from typing import Dict, Type
from collectors.base import MarketCollector
from collectors.variational import VariationalCollector
from collectors.binance import BinanceCollector
from collectors.lighter import LighterCollector
from collectors.hyperliquid import HyperliquidCollector
from collectors.aster import AsterCollector
from collectors.arcus import ArcusCollector
from collectors.bulk import BulkCollector
from collectors.risex import RisexCollector
from config import ExchangeSettings

COLLECTOR_MAP: Dict[str, Type[MarketCollector]] = {
    "variational": VariationalCollector,
    "binance": BinanceCollector,
    "lighter": LighterCollector,
    "hyperliquid": HyperliquidCollector,
    "aster": AsterCollector,
    "arcus": ArcusCollector,
    "bulk": BulkCollector,
    "risex": RisexCollector,
}

def create_collector(key: str, settings: ExchangeSettings) -> MarketCollector:
    """Factory method to create a collector instance based on exchange key."""
    collector_cls = COLLECTOR_MAP.get(key.lower())
    if not collector_cls:
        raise ValueError(f"Unknown exchange key: {key}")
    return collector_cls(settings)
