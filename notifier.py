"""Notification helpers (WeChat webhook)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Iterable, Dict


from config import NotificationSettings
from models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class WeChatNotifier:
    def __init__(self, settings: NotificationSettings) -> None:
        self.settings = settings
        # Track last sent time for each (symbol, direction) pair
        # Key: "SYMBOL_direction", Value: timestamp (float)
        self._state_file = "notification_state.json"
        self._last_sent: Dict[str, float] = self._load_state()

    def _load_state(self) -> Dict[str, float]:
        """Load notification state from disk."""
        import json
        import os
        if not os.path.exists(self._state_file):
            return {}
        try:
            with open(self._state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading notification state: {e}")
            return {}

    def _save_state(self):
        """Save notification state to disk."""
        try:
            # Clean up old entries to prevent file from growing indefinitely
            # Remove entries older than 24 hours
            current_time = time.time()
            cutoff_time = current_time - (24 * 3600)
            
            keys_to_remove = [k for k, v in self._last_sent.items() if v < cutoff_time]
            for k in keys_to_remove:
                del self._last_sent[k]
                
            with open(self._state_file, "w") as f:
                json.dump(self._last_sent, f)
        except Exception as e:
            logger.error(f"Error saving notification state: {e}")

    def _is_in_notify_window(self) -> bool:
        """Check if current time is in the allowed notification window.
        
        Returns True if:
        - notify_minute_offset is 0 (no restriction), OR
        - current minute >= offset
        
        Example: offset=10 allows notifications at XX:10~XX:59, blocks XX:00~XX:09
        """
        offset = getattr(self.settings, 'notify_minute_offset', 0)
        
        # offset = 0 means no time restriction
        if offset == 0:
            return True
        
        current_minute = datetime.now().minute
        
        # Allow notification if current minute >= offset
        return current_minute >= offset



    async def send(self, opportunities: Iterable[ArbitrageOpportunity]) -> None:
        webhook = self.settings.wechat_webhook
        if not webhook:
            logger.info("未配置企业微信Webhook，跳过通知。")
            return

        # Check if current time is in the allowed notification window
        if not self._is_in_notify_window():
            offset = getattr(self.settings, 'notify_minute_offset', 10)
            logger.debug(f"当前不在通知时间窗口 (需等待整点后第{offset}分钟)，跳过套利通知。")
            return

        items = list(opportunities)
        if not items:
            return


        # 1. Filter by notification whitelist if configured
        whitelist = self.settings.notification_exchanges
        if whitelist:
            filtered_items = []
            for opp in items:
                buy_ex = opp.details.get("buy_exchange", "").lower()
                sell_ex = opp.details.get("sell_exchange", "").lower()
                if buy_ex in whitelist and sell_ex in whitelist:
                    filtered_items.append(opp)
            items = filtered_items
            if not items:
                return

        # 2. Filter by Cooldown (Deduplication)
        cooldown = self.settings.cooldown_seconds
        now = time.time()
        final_items = []
        
        for opp in items:
            # Create a unique key for this opportunity type
            # e.g. "BTCUSDT_binance_long_variational_short"
            key = f"{opp.symbol}_{opp.direction}"
            last_time = self._last_sent.get(key, 0)
            
            # Smart Cooldown: If APR is exceptionally high (>100%), 
            # reduce cooldown to 10 minutes (600s) to ensure we don't miss it
            # if previous alerts were lower value.
            item_cooldown = cooldown
            total_apr = getattr(opp, "total_apr", 0) or 0
            if total_apr > 100:
                item_cooldown = min(cooldown, 600)
                logger.info(f"High yield detected ({total_apr:.1f}% APR). Using reduced cooldown (10m) for {key}")

            if now - last_time < item_cooldown:
                logger.debug(f"Skip notification for {key} (Cooldown remaining: {item_cooldown - (now - last_time):.1f}s)")
                continue
            
            # Update last sent time and add to send list
            self._last_sent[key] = now
            final_items.append(opp)
            
        if final_items:
            self._save_state()
            
        items = final_items
        if not items:
            return

        content = _format_text(items)
        from curl_cffi import requests
        async with requests.AsyncSession(timeout=10.0, impersonate="chrome") as client:
            try:
                response = await client.post(
                    webhook,
                    json={
                        "msgtype": "text",
                        "text": {"content": content},
                    },
                )
                response.raise_for_status()
                logger.info("已发送%d条套利通知", len(items))
            except Exception as exc:
                logger.exception("发送企业微信通知失败: %s", exc)

    async def send_exit_alerts(self, exit_signals: list) -> None:
        """Send exit/close position alerts when funding rates flip unfavorably."""
        webhook = self.settings.wechat_webhook
        if not webhook:
            logger.info("未配置企业微信Webhook，跳过平仓提醒。")
            return

        # Check if current time is in the allowed notification window
        if not self._is_in_notify_window():
            offset = getattr(self.settings, 'notify_minute_offset', 10)
            logger.debug(f"当前不在通知时间窗口 (需等待整点后第{offset}分钟)，跳过平仓提醒。")
            return

        if not exit_signals:
            return


        # Cooldown: 1 hour between exit alerts for same symbol
        now = time.time()
        alerts_to_send = []
        
        for signal in exit_signals:
            # Use full position identifier for cooldown to avoid suppressing different exchange pairs
            key = f"EXIT_{signal['symbol']}_{signal['long_exchange']}_{signal['short_exchange']}"
            last_time = self._last_sent.get(key, 0)
            
            # Use same cooldown setting as arbitrage alerts (default 30m)
            cooldown = self.settings.cooldown_seconds
            if now - last_time < cooldown:
                continue
            self._last_sent[key] = now
            alerts_to_send.append(signal)
            
        if alerts_to_send:
            self._save_state()
        
        if not alerts_to_send:
            return

        content = _format_exit_alerts(alerts_to_send)
        
        from curl_cffi import requests
        async with requests.AsyncSession(timeout=10.0, impersonate="chrome") as client:
            try:
                response = await client.post(
                    webhook,
                    json={
                        "msgtype": "text",
                        "text": {"content": content},
                    },
                )
                response.raise_for_status()
                logger.info("已发送%d条平仓提醒", len(alerts_to_send))
            except Exception as exc:
                logger.exception("发送平仓提醒失败: %s", exc)


def _format_exit_alerts(signals: list) -> str:
    """Format exit alert messages."""
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"⚠️ 平仓提醒 [{current_time_str}]"]
    
    for sig in signals:
        long_rate = sig['current_funding']['long'] * 100
        short_rate = sig['current_funding']['short'] * 100
        net_bps = sig.get('net_funding_bps', 0)
        base_h = sig.get('base_interval', 8)
        # Format direction: binance_long_variational_short -> binance_long | variational_short
        display_direction = sig['direction'].replace("_long_", "_long | ")
        
        # Funding intervals
        intervals = sig.get('native_intervals', {})
        long_h = intervals.get('buy', '?')
        short_h = intervals.get('sell', '?')
        long_ex = sig['long_exchange']
        short_ex = sig['short_exchange']
        interval_display = f"{long_ex} {long_h}h | {short_ex} {short_h}h"
        
        # Hyperliquid Continuous Settlement Reminder
        hl_note = ""
        if "hyperliquid" in long_ex.lower() or "hyperliquid" in short_ex.lower():
            hl_note = "\nℹ️ Hyperliquid 持仓期间按小时实时结算利息"

        # Calculate next short-side settlement time and remaining escape window
        settlement_line = ""
        try:
            if isinstance(short_h, (int, float)) and short_h > 0:
                now_dt = datetime.now()
                interval_hours = int(short_h)
                # Settlement points: 0, interval, 2*interval, ... within 24h
                # e.g. 4h -> 0,4,8,12,16,20; 1h -> 0,1,2,...,23; 8h -> 0,8,16
                settlement_hours = list(range(0, 24, interval_hours))
                current_hour = now_dt.hour
                current_min = now_dt.minute
                current_fractional = current_hour + current_min / 60.0
                
                # Find the next settlement hour
                next_settle_hour = None
                for h in settlement_hours:
                    if h > current_fractional:
                        next_settle_hour = h
                        break
                if next_settle_hour is None:
                    # Wrap to next day's first settlement
                    next_settle_hour = settlement_hours[0] + 24
                
                # Calculate remaining time
                remaining_minutes = int((next_settle_hour - current_fractional) * 60)
                remain_h = remaining_minutes // 60
                remain_m = remaining_minutes % 60
                next_settle_display = f"{next_settle_hour % 24:02d}:00"
                
                if remain_h > 0:
                    settlement_line = f"\n⏰ 空方({short_ex} {interval_hours}h)下次结算：{next_settle_display}（剩余 {remain_h}h{remain_m:02d}m）"
                else:
                    settlement_line = f"\n⏰ 空方({short_ex} {interval_hours}h)下次结算：{next_settle_display}（剩余 {remain_m}m）"
        except Exception as e:
            logger.debug(f"Failed to calculate settlement countdown: {e}")

        # P&L status line - show TOTAL PnL (funding + MTM) like dashboard
        pnl = sig.get("pnl")
        if pnl:
            # Calculate total PnL including MTM (same as dashboard)
            net_pnl = pnl.get("net_pnl", 0)  # Funding PnL
            mtm_pnl = pnl.get("mtm_pnl", 0)  # Mark-to-market PnL
            total_pnl = pnl.get("total_pnl", net_pnl + mtm_pnl)
            notional = pnl.get("notional", 0)

            # Determine if profitable based on TOTAL PnL
            is_total_profitable = total_pnl > 0

            if is_total_profitable:
                pnl_line = (
                    f"\n✅ 资金费转负，建议平仓"
                    f"\n   总盈亏: +${total_pnl:.2f} (资金费 ${net_pnl:.2f} + 价差 ${mtm_pnl:.2f})"
                    f"\n   名义本金: ${notional:.2f} | 持仓 {pnl['hours_held']:.0f}h"
                )
            else:
                pnl_line = (
                    f"\n⚠️ 资金费转负，建议平仓止损"
                    f"\n   总盈亏: -${abs(total_pnl):.2f} (资金费 ${net_pnl:.2f} + 价差 ${mtm_pnl:.2f})"
                    f"\n   名义本金: ${notional:.2f} | 持仓 {pnl['hours_held']:.0f}h"
                )
        else:
            pnl_line = "\n⚠️ 净收益已转负，建议平仓结清"

        lines.append(
            f"\n{sig['symbol']}\n"
            f"方向：{display_direction}\n"
            f"费率明细：Long {long_rate:.4f}% | Short {short_rate:.4f}%\n"
            f"资金费时差：{interval_display}\n"
            f"资金费差：{net_bps:.2f} bps ({base_h}h Basis)"
            f"{settlement_line}"
            f"{pnl_line}"
            f"{hl_note}"
        )

    
    return "\n".join(lines)



def _format_text(items: Iterable[ArbitrageOpportunity]) -> str:
    # Add current time to the header
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"💰 套利提醒 [{current_time_str}]"]
    
    for opp in items:
        # Format direction: binance_long_variational_short -> binance_long | variational_short
        display_direction = opp.direction.replace("_long_", "_long | ")
        
        # Use 24h funding diff if available in details
        funding_diff_24h_bps = opp.details.get("funding_diff_24h_bps")
        funding_display = f"{funding_diff_24h_bps/100:.4f}%" if funding_diff_24h_bps is not None else "-"
        
        # Funding intervals
        intervals = opp.details.get("native_intervals", {})
        buy_interval = intervals.get("buy", "?")
        sell_interval = intervals.get("sell", "?")
        buy_ex = opp.details.get("buy_exchange", "long")
        sell_ex = opp.details.get("sell_exchange", "short")
        interval_display = f"{buy_ex} {buy_interval}h | {sell_ex} {sell_interval}h"

        # Hyperliquid Continuous Settlement Reminder
        hl_note = ""
        if "hyperliquid" in buy_ex.lower() or "hyperliquid" in sell_ex.lower():
            hl_note = "\nℹ️ Hyperliquid 特性：持仓期间1小时结算资金费"

        # Limit order reminder
        limit_note = ""
        if opp.details.get("suggest_limit_order"):
            limit_note = "\n⚡ 建议使用限价单（滑点风险高）"
            
        # Calculate Total Net Yield and APR
        total_bps = opp.details.get("total_net_bps", 0)
        base_interval = opp.details.get("base_interval", 8)
        funding_bps = opp.details.get("funding_diff_scaled_bps", 0)
        
        funding_annual = funding_bps * (24 / base_interval) * 3.65 if base_interval > 0 else 0
        total_apr = funding_annual + (opp.net_spread_bps / 100.0)

        lines.append(
            (
                f"\n{opp.symbol}\n"
                f"方向：{display_direction}\n"
                f"净价差：{opp.net_spread_bps:.2f} bps (原始 {opp.gross_spread_bps:.2f} bps)\n"
                f"资金费差：{funding_display} (Daily)\n"
                f"资金费时差：{interval_display}\n"
                f"总收益(日化)：{total_bps:.2f} bps | APR：≈{total_apr:.1f}%"
                f"{hl_note}"
                f"{limit_note}"
            )
        )
    return "\n".join(lines)


def _format_percent(value) -> str:
    if value is None:
        return "-"
    return f"{value*100:.4f}%"
