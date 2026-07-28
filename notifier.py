"""Notification helpers for WeChat webhooks."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from config import NotificationSettings
from models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class WeChatNotifier:
    def __init__(self, settings: NotificationSettings) -> None:
        self.settings = settings
        self._global_webhook = settings.wechat_webhook
        self._state_file = "notification_state.json"
        self._last_sent: Dict[str, float] = self._load_state()

    def _load_state(self) -> Dict[str, float]:
        if not os.path.exists(self._state_file):
            return {}
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Error loading notification state: %s", e)
            return {}

    def _save_state(self) -> None:
        try:
            cutoff_time = time.time() - (24 * 3600)
            self._last_sent = {
                key: value
                for key, value in self._last_sent.items()
                if value >= cutoff_time
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._last_sent, f)
        except Exception as e:
            logger.error("Error saving notification state: %s", e)

    def _is_in_notify_window(self) -> bool:
        offset = getattr(self.settings, "notify_minute_offset", 0)
        if offset == 0:
            return True
        return datetime.now().minute >= offset

    def _resolve_webhook(self, owner_webhook: Optional[str]) -> Optional[str]:
        return owner_webhook or self._global_webhook or None

    async def _post(self, webhook: str, content: str) -> bool:
        from curl_cffi import requests

        async with requests.AsyncSession(timeout=10.0, impersonate="chrome") as client:
            try:
                response = await client.post(
                    webhook,
                    json={"msgtype": "text", "text": {"content": content}},
                )
                response.raise_for_status()
                return True
            except Exception as exc:
                logger.exception("发送企业微信通知失败: %s", exc)
                return False

    async def send(self, opportunities: Iterable[ArbitrageOpportunity]) -> None:
        webhook = self._global_webhook
        if not webhook:
            logger.info("未配置企业微信 Webhook，跳过套利通知。")
            return

        if not self._is_in_notify_window():
            offset = getattr(self.settings, "notify_minute_offset", 10)
            logger.debug("Skip arbitrage alert outside notify window (offset=%s)", offset)
            return

        items = [
            opp for opp in opportunities
            if opp.details.get("entry_check_supported") is True
        ]
        if not items:
            logger.debug("Skip arbitrage alert because no route supports two-sided depth checks.")
            return

        whitelist = self.settings.notification_exchanges
        if whitelist:
            items = [
                opp for opp in items
                if opp.details.get("buy_exchange", "").lower() in whitelist
                and opp.details.get("sell_exchange", "").lower() in whitelist
            ]
            if not items:
                return

        cooldown = self.settings.cooldown_seconds
        now = time.time()
        final_items = []
        for opp in items:
            key = f"{opp.symbol}_{opp.direction}"
            last_time = self._last_sent.get(key, 0)
            item_cooldown = cooldown
            total_apr = getattr(opp, "total_apr", 0) or 0
            if total_apr > 100:
                item_cooldown = min(cooldown, 600)

            if now - last_time < item_cooldown:
                logger.debug(
                    "Skip notification for %s (cooldown %.1fs)",
                    key,
                    item_cooldown - (now - last_time),
                )
                continue

            final_items.append(opp)

        if not final_items:
            return

        if await self._post(webhook, _format_text(final_items)):
            for opp in final_items:
                key = f"{opp.symbol}_{opp.direction}"
                self._last_sent[key] = now
            self._save_state()
            logger.info("已发送 %d 条套利通知", len(final_items))

    async def send_exit_alerts(self, exit_signals: List[Dict]) -> None:
        """Send reversal alerts to each position owner's webhook."""
        if not exit_signals:
            return

        if not self._is_in_notify_window():
            offset = getattr(self.settings, "notify_minute_offset", 10)
            logger.debug("Skip exit alert outside notify window (offset=%s)", offset)
            return

        owner_webhooks: Dict[str, Optional[str]] = {}
        try:
            from db import get_all_users, init_db

            init_db()
            for user in get_all_users(include_disabled=True):
                owner_webhooks[user["id"]] = user.get("wecom_webhook") or None
        except Exception as e:
            logger.warning("加载用户 Webhook 失败，使用全局 Webhook: %s", e)

        cooldown = self.settings.cooldown_seconds
        now = time.time()
        for signal in exit_signals:
            owner_id = signal.get("owner_id", "")
            position_id = signal.get("position_id", "")
            webhook = self._resolve_webhook(owner_webhooks.get(owner_id))
            if not webhook:
                logger.info(
                    "未配置平仓提醒 Webhook: owner=%s symbol=%s",
                    owner_id,
                    signal.get("symbol"),
                )
                continue

            cooldown_key = f"EXIT_{owner_id}_{position_id}"
            try:
                from db import get_last_alert_time

                last_sent = get_last_alert_time(cooldown_key) or 0
            except Exception as e:
                logger.warning("读取提醒冷却失败，使用内存冷却: %s", e)
                last_sent = self._last_sent.get(cooldown_key, 0)

            if now - last_sent < cooldown:
                continue

            content = _format_exit_alerts([signal])
            if await self._post(webhook, content):
                self._last_sent[cooldown_key] = now
                self._save_state()
                try:
                    from db import log_alert, mark_position_notified

                    log_alert(
                        position_id=position_id,
                        owner_id=owner_id,
                        trigger_type="reversal",
                        message=content[:500],
                        cooldown_key=cooldown_key,
                    )
                    mark_position_notified(position_id)
                except Exception as e:
                    logger.warning("平仓提醒已发送，但写入 alert_log 失败: %s", e)
                logger.info("已发送平仓提醒: %s owner=%s", signal.get("symbol"), owner_id)


def _format_exit_alerts(signals: List[Dict]) -> str:
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"⚠️ 平仓提醒 [{current_time_str}]"]

    for sig in signals:
        long_rate = sig["current_funding"]["long"] * 100
        short_rate = sig["current_funding"]["short"] * 100
        net_bps = sig.get("net_funding_bps", 0)
        base_h = sig.get("base_interval", 8)
        intervals = sig.get("native_intervals", {})
        long_h = intervals.get("buy", "?")
        short_h = intervals.get("sell", "?")
        long_ex = sig.get("long_exchange", "")
        short_ex = sig.get("short_exchange", "")

        settlement_line = _format_next_settlement(short_h)
        pnl = sig.get("pnl") or {}
        pnl_line = ""
        if pnl:
            pnl_line = (
                f"\nPnL：资金费 ${pnl.get('net_pnl', 0):.2f} | "
                f"价差 ${pnl.get('mtm_pnl', 0):.2f} | "
                f"合计 ${pnl.get('total_pnl', pnl.get('net_pnl', 0)):.2f}"
            )

        price_line = ""
        if sig.get("current_price_long") is not None or sig.get("current_price_short") is not None:
            price_line = (
                f"\n价格：Long {_format_price(sig.get('current_price_long'))} | "
                f"Short {_format_price(sig.get('current_price_short'))}"
            )

        hl_note = ""
        if "hyperliquid" in long_ex.lower() or "hyperliquid" in short_ex.lower():
            hl_note = "\nℹ️ Hyperliquid 持仓期间按小时实时结算资金费"

        lines.append(
            (
                f"\n{sig.get('symbol', '-')}\n"
                f"方向：{long_ex} long | {short_ex} short\n"
                f"当前资金费：Long {long_rate:.4f}%/{long_h}h | Short {short_rate:.4f}%/{short_h}h\n"
                f"净资金费：{net_bps / 100:.4f}% / {base_h}h\n"
                f"不利持续：{sig.get('unfavorable_duration_minutes', 0):.1f} 分钟"
                f"{price_line}"
                f"{pnl_line}"
                f"{settlement_line}"
                f"{hl_note}"
            )
        )

    return "\n".join(lines)


def _format_text(items: Iterable[ArbitrageOpportunity]) -> str:
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🔎 套利观察信号 [{current_time_str}]",
        "以下不是开仓指令；请先在面板完成 1000 USDT/腿的实时深度复核。",
    ]

    for opp in items:
        details = opp.details
        intervals = details.get("native_intervals", {})
        buy_interval = intervals.get("buy", "?")
        sell_interval = intervals.get("sell", "?")
        buy_ex = details.get("buy_exchange", "long")
        sell_ex = details.get("sell_exchange", "short")

        funding_daily_bps = _coalesce(
            details.get("funding_daily_bps"),
            details.get("funding_diff_24h_bps"),
            0,
        )
        funding_hourly_bps = _coalesce(
            details.get("funding_hourly_bps"),
            funding_daily_bps / 24 if funding_daily_bps is not None else 0,
        )
        projected_24h_bps = _coalesce(
            details.get("projected_24h_bps"),
            opp.net_spread_bps + funding_daily_bps,
        )
        cover_hours = details.get("cover_hours")
        opportunity_type = details.get("opportunity_type", "watch")

        buy_price = details.get("buy_price")
        sell_price = details.get("sell_price")
        long_rate = details.get("funding_long_native")
        short_rate = details.get("funding_short_native")

        price_note = "入场价差有利" if opp.net_spread_bps >= 0 else "入场价差不利"
        cover_text = _format_cover_hours(cover_hours, opp.net_spread_bps)
        interval_display = (
            f"{_exchange_name(buy_ex)} {buy_interval}h | "
            f"{_exchange_name(sell_ex)} {sell_interval}h"
        )

        hl_note = ""
        if "hyperliquid" in buy_ex.lower() or "hyperliquid" in sell_ex.lower():
            hl_note = "\nℹ️ Hyperliquid 特性：持仓期间1小时结算资金费"

        limit_note = ""
        if details.get("suggest_limit_order"):
            limit_note = "\n⚡ 建议使用限价单（滑点风险高）"

        lines.append(
            (
                f"\n{opp.symbol}\n"
                f"类型：{_opportunity_type_label(opportunity_type)}\n"
                f"方向：{_exchange_name(buy_ex)} 做多 / {_exchange_name(sell_ex)} 做空\n"
                f"价格：Long {_format_price(buy_price)} | Short {_format_price(sell_price)}\n"
                f"价差：{_format_signed_bps(opp.net_spread_bps)} "
                f"({opp.net_spread_bps / 100:.2f}%，{price_note})\n"
                f"资金费：Long {_format_rate(long_rate)}/{buy_interval}h | "
                f"Short {_format_rate(short_rate)}/{sell_interval}h\n"
                f"资金费优势：{_format_signed_percent(funding_hourly_bps / 100)}/h | "
                f"{_format_signed_percent(funding_daily_bps / 100)}/day\n"
                f"覆盖时间：{cover_text}\n"
                f"24h假设收敛估算：{_format_signed_percent(projected_24h_bps / 100)}（价差+资金费，非已实现收益）\n"
                f"资金费时差：{interval_display}"
                f"{hl_note}"
                f"{limit_note}"
            )
        )

    return "\n".join(lines)


def _format_next_settlement(short_h) -> str:
    try:
        interval_hours = int(short_h)
    except (TypeError, ValueError):
        return ""
    if interval_hours <= 0:
        return ""

    now_dt = datetime.now()
    settlement_hours = list(range(0, 24, interval_hours))
    current_fractional = now_dt.hour + now_dt.minute / 60.0
    next_settle_hour = next((h for h in settlement_hours if h > current_fractional), None)
    if next_settle_hour is None:
        next_settle_hour = settlement_hours[0] + 24

    hours_until = next_settle_hour - current_fractional
    minutes_until = hours_until * 60
    next_hour_display = next_settle_hour % 24
    return f"\n下次 Short 结算：约 {next_hour_display:02d}:00（剩余 {minutes_until:.0f} 分钟）"


def _coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _exchange_name(name) -> str:
    if not name:
        return "-"
    return str(name).capitalize()


def _opportunity_type_label(value: str) -> str:
    labels = {
        "aligned": "价差+资金费同向",
        "funding_cover": "资金费覆盖价差",
        "price_only": "价差机会",
        "watch": "观察",
    }
    return labels.get(value, "观察")


def _format_price(value) -> str:
    if value is None:
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num >= 100:
        return f"{num:.2f}"
    if num >= 1:
        return f"{num:.4f}".rstrip("0").rstrip(".")
    return f"{num:.8f}".rstrip("0").rstrip(".")


def _format_signed_bps(value) -> str:
    try:
        return f"{float(value):+.2f} bps"
    except (TypeError, ValueError):
        return "-"


def _format_signed_percent(value) -> str:
    try:
        return f"{float(value):+.4f}%"
    except (TypeError, ValueError):
        return "-"


def _format_rate(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:+.4f}%"
    except (TypeError, ValueError):
        return "-"


def _format_cover_hours(cover_hours, spread_bps: float) -> str:
    if spread_bps >= 0:
        return "无需覆盖"
    if cover_hours is None:
        return "资金费暂时无法覆盖"
    if cover_hours < 1:
        return f"约 {cover_hours * 60:.0f} 分钟"
    return f"约 {cover_hours:.1f} 小时"


def _format_percent(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.4f}%"
    except (TypeError, ValueError):
        return "-"
