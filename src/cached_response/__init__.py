"""Local exact caching and opt-in response caching for LLM tests."""

import logging as _logging

from .config import Config, Rule, configure
from .decorators import cache_stats, cached_llm_response, cached_staticmethod
from .diagnostics import cache_misses
from .log_config import configure_logging

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
    "configure_logging",
    "cached_llm_response",
    "cached_staticmethod",
]
