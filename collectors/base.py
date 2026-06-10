"""Base abstractions for market data collectors."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List

from config import ExchangeSettings
from models import MarketDatum


class MarketCollector(ABC):
    """Abstract base class for exchange market data collectors."""

    def __init__(self, settings: ExchangeSettings) -> None:
        self.settings = settings
        self.last_fetch_time: float = 0
        self.last_error: Optional[str] = None

    @abstractmethod
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Pull market data for the given symbols.

        Collector implementations should normalise symbol keys according to
        ``settings.symbol_overrides`` so the rest of the system can work with a
        unified representation.
        """

    async def _gather_with_semaphore(self, coros: List[asyncio.Future], limit: int = 5):
        """Run coroutines with a concurrency limit."""

        semaphore = asyncio.Semaphore(limit)

        async def _run(coro):
            async with semaphore:
                return await coro

        return await asyncio.gather(*[_run(c) for c in coros])
