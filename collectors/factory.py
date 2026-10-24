from typing import Dict, Type
from collectors.base import MarketCollector
from collectors.variational import VariationalCollector
from collectors.binance import BinanceCollector
from collectors.lighter import LighterCollector
from collectors.hyperliquid import HyperliquidCollector
from collectors.aster import AsterCollector
from collectors.arcus import ArcusCollector
from collectors.risex import RisexCollector
from collectors.txflow import TxflowCollector
from collectors.qfex import QfexCollector
from collectors.popdex import PopdexCollector
from config import ExchangeSettings

COLLECTOR_MAP: Dict[str, Type[MarketCollector]] = {
    "variational": VariationalCollector,
    "binance": BinanceCollector,
    "lighter": LighterCollector,
    "hyperliquid": HyperliquidCollector,
    "aster": AsterCollector,
    "arcus": ArcusCollector,
    "risex": RisexCollector,
    "txflow": TxflowCollector,
    "qfex": QfexCollector,
    "popdex": PopdexCollector,
}

def create_collector(key: str, settings: ExchangeSettings) -> MarketCollector:
    """Factory method to create a collector instance based on exchange key."""
    collector_cls = COLLECTOR_MAP.get(key.lower())
    if not collector_cls:
        raise ValueError(f"Unknown exchange key: {key}")
    return collector_cls(settings)
