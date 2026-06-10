"""Collector for Variational exchange data.

The collector tries the JSON endpoint first and can optionally fall back to
HTML scraping when ``VARIATIONAL_SCRAPE_MODE=scrape``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, Iterable, List, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests

from collectors.base import MarketCollector
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)


class VariationalCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._session: requests.AsyncSession | None = None
        self._lock = asyncio.Lock()
        self._last_error_log_at: Dict[str, float] = {}

    async def _get_session(self) -> requests.AsyncSession:
        async with self._lock:
            if self._session is None:
                self._session = requests.AsyncSession(
                    base_url=self.settings.base_url,
                    timeout=self.settings.timeout,
                    headers=self.settings.extra_headers,
                    impersonate="chrome",
                )
            return self._session

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Convert exchange symbol to unified symbol, e.g. BTC-PERP -> BTCUSDT."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]

        if self.settings.symbol_pattern:
            raw_clean = raw_symbol.strip()
            match = re.match(self.settings.symbol_pattern, raw_clean)
            if match:
                asset = match.group(1).upper()
                return f"{asset}USDT"

        return None

    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        symbol_list = list(symbols)

        session = await self._get_session()
        data_from_api = await self._try_api(session)
        is_api = True

        if data_from_api is None and (self.settings.scrape_mode or "").lower() == "scrape":
            logger.debug("Variational API returned no usable data; trying scrape fallback.")
            # _try_api resets the session on network errors. Fetch the current
            # session again so scrape never reuses a closed curl_cffi session.
            session = await self._get_session()
            data = await self._scrape_markets(session)
            is_api = False
        else:
            data = data_from_api

        markets: Dict[str, MarketDatum] = {}
        if not data:
            logger.warning("Variational returned no market data.")
            return markets

        for item in data:
            raw_symbol = item.get(self.settings.price_endpoint.symbol_key)
            if not raw_symbol:
                continue

            normalised_symbol = self._normalise_symbol(raw_symbol)
            if not normalised_symbol:
                continue

            if symbol_list and normalised_symbol not in symbol_list:
                continue

            price = _safe_float(item.get(self.settings.price_endpoint.price_key))
            funding = _safe_percent(item.get(self.settings.price_endpoint.funding_key))
            volume = _safe_float(item.get(self.settings.price_endpoint.volume_key))

            interval_s = _safe_float(item.get("funding_interval_s")) or 28800
            native_interval = int(interval_s / 3600) if interval_s > 0 else 8

            if funding is not None and is_api:
                # Variational API returns annualized funding. Convert it to the
                # native funding interval used by the rest of the analyzer.
                funding = funding / (365.0 * 24.0 / native_interval)

            if price is None:
                continue

            if normalised_symbol not in markets:
                markets[normalised_symbol] = MarketDatum(
                    symbol=normalised_symbol,
                    price=price,
                    funding_rate=funding,
                    volume_24h=volume,
                    exchange=self.settings.name,
                    native_interval_hours=native_interval,
                    best_bid=_safe_float(item.get("best_bid")),
                    best_ask=_safe_float(item.get("best_ask")),
                )

        return markets

    async def _try_api(self, session: requests.AsyncSession) -> Optional[list]:
        endpoint = self.settings.price_endpoint
        try:
            response = await session.request(
                method=endpoint.method,
                url=endpoint.path,
                params=endpoint.params,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self._record_fetch_error("API", exc)
            await self._reset_session()
            return None

        data = payload
        try:
            for key in endpoint.response_path:
                data = data[key]
        except (KeyError, TypeError) as exc:
            logger.debug("Variational API response missing expected field: %s", exc)
            return None

        raw_list = []
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            for val in data.values():
                if isinstance(val, list):
                    raw_list.extend(val)

        if not raw_list:
            logger.debug("Variational API returned empty or unexpected payload.")
            return None

        logger.debug("Variational API returned %d records.", len(raw_list))
        return raw_list

    async def _scrape_markets(self, session: requests.AsyncSession) -> Optional[list]:
        try:
            response = await session.get("/markets")
            response.raise_for_status()
        except Exception as exc:
            self._record_fetch_error("Scrape", exc)
            await self._reset_session()
            return None

        return self._parse_html(response.text)

    async def aclose(self) -> None:
        await self._reset_session()

    async def _reset_session(self) -> None:
        async with self._lock:
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception as exc:
                    logger.debug("Ignoring Variational session close error: %s", exc)
                self._session = None

    def _record_fetch_error(self, stage: str, exc: Exception) -> None:
        """Log recurring network failures without flooding disk every scan."""
        error_type = type(exc).__name__
        message = str(exc)
        logger.warning("Variational %s fetch failed: %s: %s", stage, error_type, message)

        key = f"{stage}:{error_type}:{message[:120]}"
        now = time.monotonic()
        if now - self._last_error_log_at.get(key, 0.0) < 60:
            return
        self._last_error_log_at[key] = now

        try:
            with open("logs/variational_error.log", "a", encoding="utf-8") as f:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{timestamp} {stage} Error: {error_type}: {message}\n")
        except Exception:
            pass

    def _parse_html(self, html: str) -> Optional[List[Dict[str, str]]]:
        soup = BeautifulSoup(html, "lxml")
        row_selectors = self.settings.scrape_selectors.get("row", [])
        rows = _select_all(soup, row_selectors)

        results: List[Dict[str, str]] = []
        key_map = {
            "symbol": self.settings.price_endpoint.symbol_key,
            "price": self.settings.price_endpoint.price_key,
            "funding": self.settings.price_endpoint.funding_key,
            "volume": self.settings.price_endpoint.volume_key,
        }

        if rows:
            for row in rows:
                item: Dict[str, str] = {}
                for key in ("symbol", "price", "funding", "volume"):
                    selectors = self.settings.scrape_selectors.get(key, [])
                    text = _extract_text(row, selectors)
                    if text is not None:
                        item[key_map.get(key) or key] = text
                if item:
                    results.append(item)
        else:
            # Weak fallback for static data embedded in the page.
            for symbol in ("BTC-PERP", "ETH-PERP", "BNB-PERP", "SOL-PERP"):
                pattern = f'"{symbol}".*?"lastPrice":"?([\\d\\.]+)"?'
                match = re.search(pattern, html)
                if match:
                    results.append({
                        key_map["symbol"]: symbol,
                        key_map["price"]: match.group(1),
                    })

        logger.debug("Variational HTML parser returned %d records.", len(results))
        return results or None


def _safe_float(value) -> Optional[float]:
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_percent(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str) and value.endswith("%"):
        try:
            return float(value.rstrip("%")) / 100.0
        except ValueError:
            return None

    return _safe_float(value)


def _select_all(soup: BeautifulSoup, selectors):
    for selector in _ensure_list(selectors):
        nodes = soup.select(selector)
        if nodes:
            return nodes
    return []


def _extract_text(node, selectors) -> Optional[str]:
    for selector in _ensure_list(selectors):
        found = node.select_one(selector)
        if found is not None:
            value = found.get_text(strip=True)
            if value:
                return value
    return None


def _ensure_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)
