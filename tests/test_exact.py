"""Exact caching is independent of LLM risk modes."""

import asyncio

import pytest

from cached_response import cached_staticmethod, config


@pytest.mark.parametrize("mode", [None, "disabled", "testing", "risky"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_exact_cache_ignores_llm_mode(
    tmp_path, monkeypatch, mode, asynchronous
):
    monkeypatch.setattr(
        config, "_config", config.Config(path=tmp_path / "cache.db")
    )
    if mode is None:
        monkeypatch.delenv("CACHED_RESPONSE_MODE", raising=False)
    else:
        monkeypatch.setenv("CACHED_RESPONSE_MODE", mode)
    calls = []

    def compute(value):
        calls.append(value)
        return value * 2

    async def compute_async(value):
        return compute(value)

    cached = cached_staticmethod(compute_async if asynchronous else compute)

    async def run():
        assert await cached(2) == await cached(2) == 4
        assert await cached(3) == 6
        assert await cached(2, use_cache=False) == 4

    if asynchronous:
        asyncio.run(run())
    else:
        assert cached(2) == cached(2) == 4
        assert cached(3) == 6
        assert cached(2, use_cache=False) == 4
    assert calls == [2, 3, 2]


def test_exact_cache_has_no_mode_argument():
    with pytest.raises(TypeError, match="mode"):
        cached_staticmethod(mode="testing")
