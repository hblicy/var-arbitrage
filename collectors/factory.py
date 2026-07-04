from typing import Dict, Type
from collectors.base import MarketCollector
from collectors.nado import NadoCollector
from collectors.variational import VariationalCollector
from collectors.binance import BinanceCollector
from collectors.lighter import LighterCollector
from collectors.hyperliquid import HyperliquidCollector
from collectors.backpack import BackpackCollector
from collectors.grvt import GrvtCollector
from collectors.ondoperps import OndoPerpsCollector
from collectors.aster import AsterCollector
from config import ExchangeSettings

COLLECTOR_MAP: Dict[str, Type[MarketCollector]] = {
    "nado": NadoCollector,
    "variational": VariationalCollector,
    "binance": BinanceCollector,
    "lighter": LighterCollector,
    "hyperliquid": HyperliquidCollector,
    "backpack": BackpackCollector,
    "grvt": GrvtCollector,
    "ondoperps": OndoPerpsCollector,
    "aster": AsterCollector,
}

def create_collector(key: str, settings: ExchangeSettings) -> MarketCollector:
    """Factory method to create a collector instance based on exchange key."""
    collector_cls = COLLECTOR_MAP.get(key.lower())
    if not collector_cls:
        raise ValueError(f"Unknown exchange key: {key}")
    return collector_cls(settings)
