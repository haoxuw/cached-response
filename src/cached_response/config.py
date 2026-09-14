"""Shared cache options and one LLM mode: disabled, conservative, testing, risky."""

import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

STATE_VARIABLE = "CACHED_RESPONSE_MODE"
MODES = ("disabled", "conservative", "testing", "risky")
DAY = 86400
CACHE_DIRECTORY = (
    f"cached-response-{os.getuid()}"
    if hasattr(os, "getuid")
    else "cached-response"
)
DEFAULT_PATH = Path(tempfile.gettempdir()) / CACHE_DIRECTORY / "cache.sqlite3"
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": DAY}


def seconds(value):
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", value.strip())
        if not match:
            raise ValueError("Use a duration such as '30m', '12h', or '7d'")
        return float(match[1]) * DURATION_UNITS[match[2]]
    return float(value)


@dataclass(frozen=True)
class Rule:
    """A regex applied only to string values at matching dotted paths.

    Patterns must consume a whole value or have appropriate token boundaries.
    Paths use fnmatch wildcards, e.g. messages.*.content. One rule name defines
    one reference type, so repeated values receive the same placeholder.
    """

    name: str
    pattern: str
    paths: tuple[str, ...] = ("messages.*.content",)

    @classmethod
    def preset(cls, name, *, paths):
        """Recognize a value format; the caller must establish its irrelevance."""
        from .normalize import ISO_TIME, UUID

        patterns = {
            "uuid": UUID,
            "iso_time": ISO_TIME,
            "hex_id": r"[A-Fa-f0-9]{8,128}",
            "digits": r"[0-9]{1,32}",
        }
        return cls(name, patterns[name], tuple(paths))


@dataclass(frozen=True)
class TestMetadata:
    """Replace declared irrelevant numeric tool-result fields with typed zero.

    Paths are tuples relative to the tool result; '*' selects a list element.
    This changes the test input sent to the model, not just the cache key.
    """

    tool: str
    paths: tuple[tuple[str, ...], ...]
    value_type: str = "int"

    def __post_init__(self):
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError("TestMetadata requires an exact tool name")
        if self.value_type not in ("int", "float"):
            raise ValueError("TestMetadata value_type must be int or float")
        if (
            not isinstance(self.paths, tuple)
            or not self.paths
            or any(
                not isinstance(path, tuple)
                or not path
                or any(not isinstance(p, str) or not p for p in path)
                for path in self.paths
            )
        ):
            raise ValueError("TestMetadata requires nonempty tuple paths")


@dataclass(frozen=True)
class Config:
    """Immutable defaults accepted as decorator or configure keyword arguments.

    Durations accept seconds, timedelta objects, or strings such as "1d".
    LLM mode is disabled unless explicitly enabled; exact caching ignores it.
    See docs/reference.md for matching rules and the override callback contract.
    """

    path: Path = DEFAULT_PATH
    mode: str = "disabled"
    min_words: int = 100
    refresh_start: float = DAY
    refresh_force: float = 7 * DAY
    max_entry_bytes: int = 16 * 1024 * 1024
    lease_seconds: float = 300
    wait_seconds: float = 300
    report: bool = False
    diagnostics: bool = True
    signature_matching: bool = False
    structural_matching: bool = False
    diagnostic_text: bool = False
    diagnostic_raw_inputs: bool = False
    near_miss_threshold: float = 0.90
    rules: tuple[Rule, ...] = ()
    validator: Callable | None = field(default=None, compare=False, repr=False)
    learning: bool = True
    verifier_overrider: Callable | None = field(
        default=None, compare=False, repr=False
    )
    metadata_paths: tuple[str, ...] = ()
    metadata_rules: tuple[Rule, ...] = ()
    diagnostic_capture: Callable | None = field(
        default=None, compare=False, repr=False
    )
    verifier_model: str | None = None
    verifier_options: dict = field(default_factory=dict)
    verifier_version: str = "1"
    verifier_timeout: float = 10
    test_aliases: Callable | None = field(
        default=None, compare=False, repr=False
    )
    alias_version: str = "1"
    test_metadata: tuple[TestMetadata, ...] = ()

    def __post_init__(self):
        if not isinstance(self.test_metadata, tuple) or any(
            not isinstance(rule, TestMetadata) for rule in self.test_metadata
        ):
            raise ValueError(
                "test_metadata must be a tuple of TestMetadata rules"
            )
        if self.signature_matching and self.mode != "disabled":
            from .signatures import vocabulary

            vocabulary()
        if not 0 <= self.near_miss_threshold <= 1:
            raise ValueError("near_miss_threshold must be between 0 and 1")
        for name in (
            "refresh_start",
            "refresh_force",
            "lease_seconds",
            "wait_seconds",
            "verifier_timeout",
        ):
            object.__setattr__(self, name, seconds(getattr(self, name)))
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if (
            self.min_words < 0
            or not 0 <= self.refresh_start < self.refresh_force
        ):
            raise ValueError(
                "Require min_words >= 0 and 0 <= refresh_start < refresh_force"
            )
        if (
            min(self.max_entry_bytes, self.lease_seconds, self.wait_seconds)
            <= 0
        ):
            raise ValueError("Size and timeout limits must be positive")
        if any(not isinstance(p, str) or not p for p in self.metadata_paths):
            raise ValueError(
                "metadata_paths must contain nonempty dotted paths"
            )
        if not isinstance(self.verifier_options, dict):
            raise ValueError("verifier_options must be a dictionary")
        if self.verifier_timeout <= 0:
            raise ValueError("Require verifier_timeout > 0")
        if self.test_aliases is not None and not callable(self.test_aliases):
            raise ValueError("test_aliases must be a callable test contract")
        for rule in self.metadata_rules:
            re.compile(rule.pattern)
            if not rule.paths or any(not p for p in rule.paths):
                raise ValueError("metadata_rules require explicit paths")


_config = Config()


def configure(**options: Any) -> Config:
    """Set process defaults and return the resulting immutable configuration.

    Args:
        **options: Config fields, for example mode="testing", path="cache.db",
            or refresh_start="1d". Decorator options override these defaults;
            CACHED_RESPONSE_MODE also overrides the default mode.

    Raises:
        TypeError: An option name or value type is unsupported.
        ValueError: A mode, duration, or limit is invalid.
    """
    global _config
    _config = replace(_config, **options)
    return _config


def settings(**overrides):
    values = {k: v for k, v in overrides.items() if v is not None}
    if "mode" not in values:
        mode = os.environ.get(STATE_VARIABLE, _config.mode)
        values["mode"] = mode if mode in MODES else "disabled"
    return replace(_config, **values)


def refresh_probability(age, start, force):
    return max(0.0, min(1.0, (age - start) / (force - start)))
