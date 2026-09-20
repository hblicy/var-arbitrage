"""Time-weighted minute history and durable signal transitions."""
from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing


class StrategyHistory:
    def __init__(self, path: str):
        self.path = path
        self.previous = {}
        self.states = {}
        self._events = []
        self._summaries = {}
        self._pruned_at = -math.inf
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS strategy_minutes (
                route TEXT NOT NULL, minute INTEGER NOT NULL, seconds REAL NOT NULL,
                spread_sum REAL NOT NULL, spread_square REAL NOT NULL,
                funding_sum REAL NOT NULL, funding_square REAL NOT NULL, positive_seconds REAL NOT NULL,
                PRIMARY KEY(route, minute))''')
            conn.execute('CREATE INDEX IF NOT EXISTS strategy_minutes_time ON strategy_minutes(minute)')
            conn.execute('''CREATE TABLE IF NOT EXISTS strategy_events (
                id INTEGER PRIMARY KEY, route TEXT NOT NULL, occurred_at REAL NOT NULL,
                status TEXT NOT NULL, reason TEXT, qualified_seconds REAL NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS strategy_events_route ON strategy_events(route, occurred_at)')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, samples: dict, now: float, *, max_gap: float, max_age: float = 0):
        completed_changed = False
        rows = []
        for route, sample in samples.items():
            old = self.previous.get(route)
            if not old or not 0 < now - old['observed_at'] <= max_gap:
                continue
            if sample['timestamp'] < old['timestamp']:
                continue
            cursor = old['observed_at']
            valid_until = min(now, old['timestamp'] + max_age) if max_age else now
            while cursor < valid_until:
                minute = math.floor(cursor / 60) * 60
                end = min(valid_until, minute + 60)
                weight = end - cursor
                spread, funding = old['spread'], old['funding']
                rows.append((route, minute, weight, spread*weight, spread*spread*weight,
                             funding*weight, funding*funding*weight, weight if funding > 0 else 0))
                completed_changed |= minute < math.floor(now / 60) * 60
                cursor = end
        if rows:
            with closing(self.connect()) as conn, conn:
                conn.executemany('''INSERT INTO strategy_minutes VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(route, minute) DO UPDATE SET
                    seconds=seconds+excluded.seconds, spread_sum=spread_sum+excluded.spread_sum,
                    spread_square=spread_square+excluded.spread_square, funding_sum=funding_sum+excluded.funding_sum,
                    funding_square=funding_square+excluded.funding_square,
                    positive_seconds=positive_seconds+excluded.positive_seconds''', rows)
        self.previous = {route: {**sample, 'observed_at': now} for route, sample in samples.items()}
        if completed_changed:
            self._summaries.clear()
        if now - self._pruned_at >= 3600:
            self.prune(now)

    def summaries(self, now: float, hours: int) -> dict:
        end = math.floor(now / 60) * 60
        key = (end, hours)
        if key in self._summaries:
            return self._summaries[key]
        start = end - hours * 3600
        with closing(self.connect()) as conn:
            rows = conn.execute('''SELECT route, SUM(seconds) AS weight,
                SUM(spread_sum) AS spread, SUM(spread_square) AS spread_square,
                SUM(funding_sum) AS funding, SUM(funding_square) AS funding_square,
                SUM(positive_seconds) AS positive FROM strategy_minutes
                WHERE minute>=? AND minute<? GROUP BY route''', (start, end)).fetchall()
        result = {}
        for row in rows:
            weight = row['weight']
            spread, funding = row['spread']/weight, row['funding']/weight
            result[row['route']] = {
                'spread_mean_bps': spread, 'spread_std_bps': math.sqrt(max(0, row['spread_square']/weight-spread**2)),
                'funding_mean_hourly_bps': funding,
                'funding_std_hourly_bps': math.sqrt(max(0, row['funding_square']/weight-funding**2)),
                'positive_funding_ratio': row['positive']/weight,
                'valid_seconds': weight, 'coverage': min(1, weight/(hours*3600)),
                'window_hours': hours, 'through_at': end,
            }
        self._summaries = {key: result}
        return result

    def series(self, route: str, now: float, hours: int) -> list:
        end = math.floor(now / 60) * 60
        with closing(self.connect()) as conn:
            rows = conn.execute('''SELECT minute AS time, seconds AS valid_seconds,
                spread_sum/seconds AS spread_bps, funding_sum/seconds AS funding_hourly_bps
                FROM strategy_minutes WHERE route=? AND minute>=? AND minute<? ORDER BY minute''',
                (route, end-hours*3600, end)).fetchall()
        return [dict(row) for row in rows]

    def advance(self, route, now, qualifies, valid, reason, *, required_seconds, max_gap, valid_until=None) -> dict:
        old = self.states.get(route, {})
        delta = now - old.get('checked_at', now)
        previous_expiry = old.get('valid_until')
        continuous = (old.get('qualifies') and 0 < delta <= max_gap
                      and (previous_expiry is None or now <= previous_expiry))
        duration = old.get('qualified_seconds', 0) + delta if qualifies and valid and continuous else 0
        if not valid:
            status = 'paused'
        elif qualifies:
            status = 'triggered' if duration >= required_seconds else 'pending'
        elif old.get('status') in ('triggered', 'pending'):
            status = 'ended'
        else:
            status = 'watch'
        state = {'status': status, 'reason': reason, 'qualified_seconds': duration,
                 'required_seconds': required_seconds, 'checked_at': now, 'qualifies': qualifies and valid,
                 'valid_until': valid_until}
        if old.get('status') != status or old.get('reason') != reason:
            self._events.append((route, now, status, reason, duration))
        self.states[route] = state
        return state

    def events(self, route=None) -> list:
        self.flush_events()
        with closing(self.connect()) as conn:
            rows = conn.execute('''SELECT route,occurred_at,status,reason,qualified_seconds FROM strategy_events
                WHERE (? IS NULL OR route=?) ORDER BY id DESC LIMIT 100''', (route, route)).fetchall()
        return [dict(row) for row in rows]

    def flush_events(self):
        if self._events:
            with closing(self.connect()) as conn, conn:
                conn.executemany('INSERT INTO strategy_events(route,occurred_at,status,reason,qualified_seconds) VALUES(?,?,?,?,?)', self._events)
            self._events.clear()

    def prune(self, now):
        self.flush_events()
        cutoff = now - 48*3600
        with closing(self.connect()) as conn, conn:
            conn.execute('DELETE FROM strategy_minutes WHERE minute<?', (cutoff,))
            conn.execute('DELETE FROM strategy_events WHERE occurred_at<?', (cutoff,))
        self.states = {key: state for key, state in self.states.items() if state['checked_at'] >= cutoff}
        self._summaries.clear()
        self._pruned_at = now
