"""Local exact caching and opt-in response caching for LLM tests."""

import logging as _logging

from .config import Config, Rule, configure
from .decorators import cache_stats, cached_llm_response, cached_staticmethod
from .diagnostics import cache_misses, capture_misses
from .log_config import configure_logging


def cache_signatures(*, path=None, limit=20):
    """Read persistent signature request/hit/miss counters, most frequent first."""
    from .config import settings
    from .storage import get_store

    if not isinstance(limit, int) or not 0 <= limit <= 1000:
        raise ValueError("limit must be between 0 and 1000")
    return get_store(
        str(path if path is not None else settings().path)
    ).signature_stats(limit)


# Silent even when the host configures the root logger. Applications may attach
# their own handler here, or explicitly opt in through configure_logging().
_logger = _logging.getLogger("cached_response")
_logger.addHandler(_logging.NullHandler())
_logger.propagate = False

__all__ = [
    "Config",
    "Rule",
    "configure",
    "cache_stats",
    "cache_misses",
    "capture_misses",
    "cache_signatures",
    "configure_logging",
    "cached_llm_response",
    "cached_staticmethod",
]
