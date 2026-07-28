#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nado ↔ Variational arbitrage monitor."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
import sys
import time

from dotenv import load_dotenv

from analyzer import analyse_markets
from collectors.nado import NadoCollector
from collectors.variational import VariationalCollector
from collectors.binance import BinanceCollector
from collectors.lighter import LighterCollector
from collectors.hyperliquid import HyperliquidCollector
from collectors.backpack import BackpackCollector
from config import Settings, settings
from entry_check import annotate_entry_check_support
from notifier import WeChatNotifier
from market_state import stamp_market_timestamps
from position_tracker import accumulate_funding, update_pre_settlement_rates

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_cycle(
    cfg: Settings,
    collectors: Dict[str, MarketCollector],
    notifier: WeChatNotifier,
) -> None:
    symbols = cfg.tracked_symbols

    # Fetch data from all collectors concurrently
    fetch_tasks = {
        name: asyncio.create_task(_safe_fetch(collector, symbols, name))
        for name, collector in collectors.items()
    }
    
    exchanges_data = {}
    for name, task in fetch_tasks.items():
        exchanges_data[name] = await task

    # Persist the freshest funding rates so accumulate_funding loop
    # can charge the PRE-settlement rate when crossing a boundary
    update_pre_settlement_rates(exchanges_data)

    # Dyamically discover common symbols if none tracked
    if not symbols:
        # Find intersection of all non-empty market data keys
        market_sets = [set(data.keys()) for data in exchanges_data.values() if data]
        if market_sets:
            common_symbols = set.intersection(*market_sets)
            if common_symbols:
                logger.info("自动发现 %d 个共有交易对", len(common_symbols))
                symbols_to_analyze = sorted(list(common_symbols))
            else:
                logger.warning("未自发现共同交易对。")
                return
        else:
            logger.warning("所有交易所数据均为空。")
            return
    else:
        symbols_to_analyze = symbols

    opportunities, _, _ = analyse_markets(symbols_to_analyze, exchanges_data, cfg)
    annotate_entry_check_support(opportunities, collectors)
    if opportunities:
        logger.info("发现 %d 个套利机会", len(opportunities))
        for opp in opportunities:
            logger.info(
                "%s | %s | 净价差 %.2f bps",
                opp.symbol,
                opp.direction,
                opp.net_spread_bps,
            )
        await notifier.send(opportunities)
    else:
        logger.info("暂无满足条件的套利机会。")


async def funding_settlement_loop(
    cfg: Settings,
    collectors: Dict[str, "MarketCollector"],
) -> None:
    """独立任务: 每 50 分钟检查并记录资金费结算。
    
    与主扫描循环并行运行, 专门负责更新持仓的真实累计资金费。
    """
    INTERVAL = 3000  # 50 minutes
    while True:
        try:
            # 复用 collector fetch
            fetch_tasks = {
                name: asyncio.create_task(_safe_fetch(collector, [], name))
                for name, collector in collectors.items()
            }
            exchanges_data = {}
            for name, task in fetch_tasks.items():
                exchanges_data[name] = await task
            
            n = accumulate_funding(exchanges_data)
            if n:
                logger.info("资金费结算循环: 本轮记录 %d 笔结算", n)
        except Exception as e:
            logger.error("资金费结算循环异常: %s", e)
        
        await asyncio.sleep(INTERVAL)


async def _safe_fetch(collector, symbols, label: str):
    try:
        data = await collector.fetch_markets(symbols)
        stamp_market_timestamps(data, time.time())
        return data
    except Exception as exc:
        logger.exception("获取 %s 行情失败: %s", label, exc)
        return {}


async def main() -> None:
    load_dotenv()
    cfg = settings

    collectors = {}
    from collectors.factory import create_collector

    for key, ex_cfg in cfg.exchanges.items():
        if not ex_cfg.enabled:
            logger.info(f"交易所 {key} 已禁用，跳过初始化。")
            continue
            
        try:
            collectors[key] = create_collector(key, ex_cfg)
        except ValueError as e:
            logger.warning(str(e))
        except Exception as e:
            logger.error(f"初始化交易所 {key} 失败: {e}")

    notifier = WeChatNotifier(cfg.notifications)

    try:
        # 并行启动: 主扫描循环 + 资金费结算循环
        funding_task = asyncio.create_task(
            funding_settlement_loop(cfg, collectors)
        )
        
        while True:
            await run_cycle(cfg, collectors, notifier)
            await asyncio.sleep(cfg.schedule.interval_seconds)
    finally:
        funding_task.cancel()
        for collector in collectors.values():
            await _safe_close(collector)


async def _safe_close(collector) -> None:
    close = getattr(collector, "aclose", None)
    if close:
        with suppress(Exception):
            await close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断，退出监控。")
