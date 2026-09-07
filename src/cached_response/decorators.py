"""Exact function caching and opt-in LLM caching share one cache engine."""

import asyncio
import functools
import hashlib
import inspect
import logging
import marshal
import os
import random
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from . import adapters, learning
from .config import refresh_probability, settings
from .normalize import Normalized, digest, dumps, normalize, rebind, word_count
from .storage import get_store

LOGGER = logging.getLogger("cached_response")
POLL_INTERVAL = 0.05
FORMAT_VERSION = 4
MARSHAL_VERSION = (
    2  # Avoid reference-sharing flags changing after introspection.
)
_stats = Counter()
_stats_lock = threading.Lock()


def cache_stats() -> dict[str, int | float]:
    """Return process-wide request counts and cache_hit_percent for enabled calls.

    Counts combine all decorated functions. Disabled LLM calls and use_cache=False
    bypasses are excluded. Hit percentage is not token or cost savings.
    """
    with _stats_lock:
        result = dict(_stats)
    requests = result.get("requests", 0)
    result["cache_hit_percent"] = (
        100 * result.get("hit", 0) / requests if requests else 0.0
    )
    return result


def decision(kind, reason, config, key=""):
    with _stats_lock:
        _stats["requests"] += 1
        _stats[kind] += 1
    LOGGER.info(
        "Cache %s for hash: %s. reason=%s mode=%s",
        kind,
        key,
        reason,
        config.mode,
    )
    if config.report:
        stats = cache_stats()
        print(
            f"cached-response: {kind} ({reason}); {stats.get('hit', 0)}/{stats['requests']} cached ({stats['cache_hit_percent']:.1f}%)",
            file=sys.stderr,
        )


class Ticket:
    def __init__(
        self, store, key, owner, body, normalized, config, learning_scope=None
    ):
        self.store, self.key, self.owner = store, key, owner
        self.body, self.normalized, self.config = body, normalized, config
        self.learning_scope = learning_scope

    def release(self):
        try:
            self.store.release(self.key, self.owner)
        except Exception:
            LOGGER.debug("Cache lease release failed", exc_info=True)

    def save(self, result):
        try:
            payload = dumps(
                {
                    "result": result,
                    "bindings": self.normalized.bindings,
                    "input": self.body,
                }
            )
            if len(payload.encode()) <= self.config.max_entry_bytes:
                self.store.put(self.key, self.owner, payload)
                if self.learning_scope:
                    self.store.index(self.learning_scope, self.key)
        except Exception:
            LOGGER.debug("Response could not be cached", exc_info=True)
        finally:
            self.release()


def prepare(body, scope, config, llm, verifier=None):
    """Returns (ticket, cached envelope). A None ticket means bypass."""
    if llm and word_count(body) < config.min_words:
        decision("miss", "short_input", config)
        return None, None
    store = get_store(str(config.path))
    model_settings = (
        {k: v for k, v in body.items() if k != "messages"}
        if isinstance(body, dict)
        else {}
    )
    rule_scope = digest({"function": scope, "settings": model_settings})
    learned = (
        store.learned(rule_scope) if llm and config.mode == "risky" else ()
    )
    normalized = (
        normalize(body, config.mode, config.rules, learned)
        if llm
        else Normalized(body, {}, [])
    )
    learning_scope = (
        learning.scope_key(scope, body, config)
        if llm and config.mode == "testing" and config.learning
        else None
    )
    key = digest(
        {
            "format": FORMAT_VERSION,
            "scope": scope,
            "mode": config.mode if llm else "exact",
            "rules": [asdict(rule) for rule in config.rules],
            "learning": bool(learning_scope),
            "input": normalized.body,
        }
    )
    if normalized.learned:
        store.learn(rule_scope, normalized.learned)
    owner, deadline = uuid.uuid4().hex, time.monotonic() + config.wait_seconds
    random_draw = random.random()
    while True:
        entry = store.get(key)
        rejected = False
        if entry:
            created, payload = entry
            probability = refresh_probability(
                time.time() - created,
                config.refresh_start,
                config.refresh_force,
            )
            if random_draw >= probability and probability < 1:
                try:
                    if config.validator and not config.validator(
                        payload["input"], body
                    ):
                        raise ValueError("Validator rejected the candidate")
                    result = rebind(
                        payload["result"],
                        payload["bindings"],
                        normalized.bindings,
                    )
                    # Check decoding before advertising a hit.
                    adapters.unpack(result)
                except Exception:
                    rejected = True
                else:
                    decision(
                        "hit",
                        "normalized" if payload["input"] != body else "exact",
                        config,
                        key,
                    )
                    return None, result
        if store.claim(key, owner, config.lease_seconds):
            # Another producer may have committed between get() and claim().
            # Recheck under our lease before making a duplicate upstream call.
            latest = store.get(key)
            if latest != entry:
                store.release(key, owner)
                continue
            if entry is None and learning_scope and verifier:
                try:
                    result = learning.lookup(
                        store,
                        learning_scope,
                        body,
                        normalized,
                        config,
                        verifier,
                    )
                    if result is not None:
                        adapters.unpack(result)
                        # Keep the original entry's age for learned reuse: a new
                        # input must not renew an old answer's freshness window.
                        store.release(key, owner)
                        decision("hit", "verified_rule", config, key)
                        return None, result
                except BaseException:
                    store.release(key, owner)
                    raise
            decision(
                "miss",
                "rejected" if rejected else "refresh" if entry else "cold",
                config,
                key,
            )
            return Ticket(
                store, key, owner, body, normalized, config, learning_scope
            ), None
        if time.monotonic() >= deadline:
            decision("miss", "busy", config, key)
            return None, None
        time.sleep(POLL_INTERVAL)


async def async_lookup(lookup, *args):
    """Release a worker's eventual cache lease if its caller is cancelled."""
    lock = threading.Lock()
    cancelled = False
    ticket = None

    def run():
        nonlocal ticket
        result = lookup(*args)
        with lock:
            ticket = result[0]
            if cancelled and ticket is not None:
                ticket.release()
        return result

    try:
        return await asyncio.to_thread(run)
    except asyncio.CancelledError:
        with lock:
            cancelled = True
            if ticket is not None:
                ticket.release()
        raise


def decorate(function, llm, overrides, version):
    identity = digest(
        {
            "function": f"{function.__module__}.{function.__qualname__}",
            "version": version
            or hashlib.sha256(
                marshal.dumps(function.__code__, MARSHAL_VERSION)
            ).hexdigest(),
        }
    )
    try:
        # FastAPI needs resolved annotations when inspecting this wrapper.
        signature = inspect.signature(function, eval_str=True)
    except NameError:
        # TYPE_CHECKING-only imports and forward references need not resolve.
        signature = inspect.signature(function)

    def arguments(kwargs):
        kwargs = dict(kwargs)
        use_cache = kwargs.get("use_cache", True)
        if "use_cache" not in signature.parameters:
            kwargs.pop("use_cache", None)
        return kwargs, use_cache

    def scope(extra=None):
        return digest(
            {
                "function": identity,
                "scope": overrides.get("namespace"),
                "upstream": os.environ.get("INFERENCE_URL"),
                "http": extra,
            }
        )

    configuration = functools.partial(
        settings, **{k: v for k, v in overrides.items() if k != "namespace"}
    )

    def ready(body, extra, config, verifier=None):
        try:
            return prepare(
                body,
                scope(extra),
                config,
                llm,
                verifier or config.verifier_overrider,
            )
        except Exception:
            LOGGER.debug("Cache lookup unavailable", exc_info=True)
            decision("miss", "unsupported_or_unavailable", config)
            return None, None

    @functools.wraps(function)
    def sync(*args, **kwargs):
        kwargs, use_cache = arguments(kwargs)
        config = configuration()
        if (llm and config.mode == "disabled") or not use_cache:
            return function(*args, **kwargs)
        try:
            body = adapters.function_input(function, args, kwargs)
        except Exception:
            decision("miss", "unsupported_input", config)
            return function(*args, **kwargs)
        verifier = config.verifier_overrider
        if (
            verifier is None
            and llm
            and config.learning
            and config.mode == "testing"
        ):
            verifier = functools.partial(
                adapters.verify_function, function, args, kwargs, body
            )
        ticket, cached = ready(body, None, config, verifier)
        if cached is not None:
            return adapters.unpack(cached)
        try:
            result = function(*args, **kwargs)
            if ticket:
                try:
                    ticket.save(adapters.pack(result))
                except Exception:
                    LOGGER.debug("Unsupported cache result", exc_info=True)
            return result
        finally:
            if ticket:
                ticket.release()

    @functools.wraps(function)
    async def async_(*args, **kwargs):
        kwargs, use_cache = arguments(kwargs)
        config = configuration()
        if (llm and config.mode == "disabled") or not use_cache:
            return await function(*args, **kwargs)
        try:
            request = adapters.http_request(args, kwargs)
            body = (
                await request.json()
                if request is not None
                else adapters.function_input(function, args, kwargs)
            )
            extra = (
                adapters.request_scope(request, signature.bind(*args, **kwargs))
                if request is not None
                else None
            )
        except Exception:
            decision("miss", "unsupported_input", config)
            return await function(*args, **kwargs)
        verifier = config.verifier_overrider
        if (
            llm
            and verifier is None
            and config.learning
            and config.mode == "testing"
        ):
            loop = asyncio.get_running_loop()

            def verifier(evidence):
                pending = asyncio.run_coroutine_threadsafe(
                    adapters.verify_http(
                        function, args, kwargs, request, body, evidence
                    )
                    if request is not None
                    else adapters.verify_async_function(
                        function, args, kwargs, body, evidence
                    ),
                    loop,
                )
                try:
                    return pending.result(timeout=config.verifier_timeout)
                finally:
                    if not pending.done():
                        pending.cancel()

        ticket, cached = await async_lookup(
            ready, body, extra, config, verifier
        )
        if cached is not None:
            return adapters.unpack(cached)
        try:
            result = await function(*args, **kwargs)
        except BaseException:
            if ticket:
                ticket.release()
            raise
        if not ticket:
            return result
        if hasattr(result, "body_iterator"):
            if (
                result.status_code != 200
                or "text/event-stream"
                not in result.headers.get("content-type", "")
                or "set-cookie" in result.headers
            ):
                ticket.release()
                return result
            original = result.body_iterator

            async def record():
                chunks, size, saved = [], 0, False
                tail = b""
                try:
                    async for chunk in original:
                        raw = (
                            chunk.encode() if isinstance(chunk, str) else chunk
                        )
                        tail = (tail + raw)[-128:]
                        size += len(raw)
                        if size <= config.max_entry_bytes:
                            chunks.append(raw)
                            # Save before yielding DONE: clients can disconnect
                            # immediately after it, without waiting for HTTP EOF.
                            if (
                                not saved
                                and b"data: [DONE]\n\n"
                                in tail.replace(b"\r\n", b"\n")
                            ):
                                try:
                                    packed = adapters.completed_sse(
                                        b"".join(chunks), result.headers
                                    )
                                    await asyncio.to_thread(ticket.save, packed)
                                    saved = True
                                except Exception:
                                    LOGGER.debug(
                                        "Stream could not be cached",
                                        exc_info=True,
                                    )
                        yield chunk
                finally:
                    if not saved:
                        ticket.release()
                    close = getattr(original, "aclose", None)
                    if close:
                        await close()

            result.body_iterator = record()
            return result
        try:
            await asyncio.to_thread(ticket.save, adapters.pack(result))
        except Exception:
            ticket.release()
            LOGGER.debug("Unsupported cache result", exc_info=True)
        return result

    wrapper = async_ if inspect.iscoroutinefunction(function) else sync
    wrapper.__signature__ = signature
    wrapper.cache_stats = cache_stats
    return wrapper


def cached_llm_response(
    function: Callable | None = None,
    *,
    mode: str | None = None,
    version: str | None = None,
    verifier_overrider: Callable | None = None,
    **options: Any,
) -> Callable:
    """Decorate a sync or async LLM function; caching is disabled by default.

    Args:
        function: The function to wrap; omitted when using @decorator(...).
        mode: disabled, conservative, testing, or risky. When omitted, use
            CACHED_RESPONSE_MODE, then configure() defaults.
        version: Explicit cache version for changes to hidden dependencies.
        verifier_overrider: Optional replacement for built-in verification.
            Receives evidence including default instructions and returns a dict
            with safe_to_reuse (bool) and reason (str), synchronously. By default,
            supported chat wrappers use the package prompt and original function.
        **options: Config fields such as path, min_words, refresh_start, and
            refresh_force; namespace additionally separates caches.

    Returns:
        A wrapped function, or a decorator if function was omitted. Inputs under
        100 words bypass caching by default. Call with use_cache=False to bypass.
        Results must be supported JSON values, tuples, bytes, or HTTP responses.

    Raises:
        TypeError: A decorator option is unsupported.
        ValueError: An explicit mode or configuration value is invalid.
    """
    apply = functools.partial(
        decorate,
        llm=True,
        overrides={
            "mode": mode,
            "verifier_overrider": verifier_overrider,
            **options,
        },
        version=version,
    )
    return apply(function) if function is not None else apply


def cached_staticmethod(
    function: Callable | None = None,
    *,
    version: str | None = None,
    **options: Any,
) -> Callable:
    """Cache a pure sync or async function by exact arguments, always enabled.

    No mode is needed; LLM mode settings do not affect this decorator. Supports
    shared options such as path, refresh_start, refresh_force, report, namespace,
    and version. No normalization, learning, or word minimum is applied.

    A class is unnecessary; inside a class put @staticmethod above this
    decorator. Call with use_cache=False to run the function without caching.
    Only cache functions whose earlier results are acceptable and whose side
    effects do not need to execute on every call.
    """
    if "mode" in options:
        raise TypeError(
            "cached_staticmethod has no mode; call with use_cache=False to bypass"
        )
    apply = functools.partial(
        decorate, llm=False, overrides=options, version=version
    )
    return apply(function) if function is not None else apply
