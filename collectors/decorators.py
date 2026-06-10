"""Unified error handling utilities for collectors."""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import TypeVar, Callable, Any

logger = logging.getLogger(__name__)

T = TypeVar('T')


def with_retry(
    max_retries: int = 3,
    backoff_base: float = 2.0,
    exceptions: tuple = (Exception,),
    default_return: Any = None,
):
    """
    Async retry decorator with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts.
        backoff_base: Base for exponential backoff (delay = backoff_base ** attempt).
        exceptions: Tuple of exception types to catch and retry.
        default_return: Value to return if all retries fail. If None, re-raises the last exception.
    
    Usage:
        @with_retry(max_retries=3, backoff_base=2)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = backoff_base ** attempt
                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_retries} attempts: {e}"
                        )
            
            if default_return is not None:
                return default_return
            raise last_exception
        return wrapper
    return decorator


def safe_fetch(default_return: Any = None):
    """
    Simpler decorator that catches all exceptions and returns a default value.
    Logs the error but doesn't retry.
    
    Usage:
        @safe_fetch(default_return={})
        async def fetch_markets(self, symbols):
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"{func.__name__} error: {e}")
                return default_return
        return wrapper
    return decorator
