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
import types
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from . import adapters, matching, signatures
from .config import refresh_probability, settings
from .diagnostics import (
    MissDiagnostic,
    cache_misses,
    remember,
)
from .normalize import (
    Normalized,
    digest,
    dumps,
    normalize,
    project_test_metadata,
    signed_handles,
    word_count,
)
from .storage import get_store

LOGGER = logging.getLogger("cached_response")
POLL_INTERVAL = 0.05
FORMAT_VERSION = 6
MARSHAL_VERSION = (
    2  # Avoid reference-sharing flags changing after introspection.
)
_stats = Counter()
_stats_lock = threading.Lock()


def cache_stats() -> dict[str, Any]:
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
    misses = result.get("miss", 0)
    result["cache_miss_percent"] = 100 * misses / requests if requests else 0.0
    result["hit_reasons"] = {
        key.removeprefix("hit_reason:"): result.pop(key)
        for key in list(result)
        if key.startswith("hit_reason:")
    }
    result["miss_reasons"] = {
        key.removeprefix("miss_reason:"): result.pop(key)
        for key in list(result)
        if key.startswith("miss_reason:")
    }
    result["candidate_rejections"] = {
        key.removeprefix("candidate_reason:"): result.pop(key)
        for key in list(result)
        if key.startswith("candidate_reason:")
    }
    count = result.get("miss_prompt_count", 0)
    result["miss_prompt"] = {"count": count}
    for metric in ("chars", "bytes", "words", "symbols", "digits", "lines"):
        total = result.pop(f"prompt:{metric}:total", 0)
        result["miss_prompt"][metric] = {
            "total": total,
            "min": result.pop(f"prompt:{metric}:min", 0),
            "max": result.pop(f"prompt:{metric}:max", 0),
            "mean": total / count if count else 0.0,
        }
    return result


def decision(kind, reason, config, key="", diagnostic=None):
    with _stats_lock:
        _stats["requests"] += 1
        _stats[kind] += 1
        _stats[f"{kind}_reason:{reason}"] += 1
        if diagnostic is not None:
            _stats["diagnosed_misses"] += 1
            candidates = diagnostic.get("candidates", [])
            _stats["misses_with_candidates"] += bool(candidates)
            _stats["near_misses"] += bool(diagnostic.get("near_miss"))
            _stats["candidates_missed"] += len(candidates)
            for candidate in candidates:
                _stats[f"candidate_reason:{candidate['reason']}"] += 1
            if "prompt" in diagnostic:
                first = not _stats["miss_prompt_count"]
                _stats["miss_prompt_count"] += 1
                for metric, value in diagnostic["prompt"].items():
                    _stats[f"prompt:{metric}:total"] += value
                    low, high = f"prompt:{metric}:min", f"prompt:{metric}:max"
                    _stats[low] = value if first else min(_stats[low], value)
                    _stats[high] = max(_stats[high], value)
    LOGGER.info(
        "Cache %s for hash: %s. reason=%s mode=%s",
        kind,
        key,
        reason,
        config.mode,
    )
    if diagnostic is not None:
        event = {
            "event": "cache_miss",
            "key": key,
            "reason": reason,
            "mode": config.mode,
            **diagnostic,
        }
        remember(event)
        LOGGER.info(
            "Cache miss diagnostic %s",
            dumps(event),
            extra={"cache_diagnostic": event},
        )
    if config.report:
        stats = cache_stats()
        print(
            f"cached-response: {kind} ({reason}); {stats.get('hit', 0)}/{stats['requests']} cached ({stats['cache_hit_percent']:.1f}%); "
            f"{stats.get('miss', 0)} missed, {stats.get('near_misses', 0)} high-similarity misses",
            file=sys.stderr,
        )


class Ticket:
    def __init__(
        self,
        store,
        key,
        owner,
        body,
        normalized,
        config,
        matching_scope=None,
        diagnostic_scope=None,
        signature_info=None,
    ):
        self.store, self.key, self.owner = store, key, owner
        self.body, self.normalized, self.config = body, normalized, config
        self.matching_scope = matching_scope
        self.diagnostic_scope = diagnostic_scope
        self.signature_info = signature_info

    def release(self):
        try:
            self.store.release(self.key, self.owner)
        except Exception as exc:
            LOGGER.debug(
                "Cache lease release failed error=%s", type(exc).__name__
            )

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
                if self.matching_scope:
                    self.store.index(self.matching_scope, self.key)
                    metadata_scope = matching.metadata_scope(
                        self.matching_scope, self.body, self.config
                    )
                    if metadata_scope:
                        self.store.index(metadata_scope, self.key)
                if self.signature_info:
                    self.store.index_signatures(*self.signature_info, self.key)
                if (
                    self.diagnostic_scope
                    and self.diagnostic_scope != self.matching_scope
                ):
                    self.store.index(self.diagnostic_scope, self.key)
        except Exception as exc:
            LOGGER.debug(
                "Response could not be cached error=%s", type(exc).__name__
            )
        finally:
            self.release()


def prepare(body, scope, config, llm, verifier=None, original_input=None):
    """Returns (ticket, cached envelope). A None ticket means bypass."""
    diagnostic = (
        MissDiagnostic(body, config, original_input)
        if llm and (config.diagnostics or config.diagnostic_capture)
        else None
    )
    store = None
    signature_info = None
    if llm and config.signature_matching:
        store = get_store(str(config.path))
        signature_info = (
            matching.scope_key(
                scope,
                body,
                config,
                include_roles=not config.structural_matching,
            )
            or digest(scope),
            signatures.fingerprints(body),
        )

    def record(kind, reason, config, key="", diagnostic=None):
        if signature_info:
            try:
                store.count_signatures(*signature_info, kind == "hit")
            except Exception as exc:
                LOGGER.debug(
                    "Signature counter failed error=%s", type(exc).__name__
                )
        decision(kind, reason, config, key, diagnostic)

    def miss(reason, key="", store=None, candidate_scope=None, normalized=None):
        details = None
        if diagnostic is not None:
            try:
                details = diagnostic.finish(
                    store, candidate_scope, normalized, reason
                )
                if config.diagnostic_capture:
                    config.diagnostic_capture(diagnostic, details)
            except Exception as exc:
                details = {"diagnostic_error": type(exc).__name__}
        record("miss", reason, config, key, details)

    if llm and word_count(body) < config.min_words:
        miss("short_input")
        return None, None
    if store is None:
        store = get_store(str(config.path))
    normalized = None
    matching_scope = (
        matching.scope_key(
            scope, body, config, include_roles=not config.structural_matching
        )
        if llm
        and config.mode in ("testing", "risky")
        and (config.learning or config.metadata_rules)
        else None
    )
    diagnostic_scope = (
        digest(
            {
                "diagnostics": 1,
                "scope": matching.scope_key(
                    scope, body, config, include_roles=False
                )
                or {
                    "scope": scope,
                    "mode": config.mode,
                    "rules": [asdict(rule) for rule in config.rules],
                },
            }
        )
        if diagnostic is not None
        else None
    )
    key = digest(
        {
            "format": FORMAT_VERSION,
            "scope": scope,
            "mode": config.mode if llm else "exact",
            "rules": [asdict(rule) for rule in config.rules],
            "learning": bool(matching_scope),
            "input": body,
            "policy": matching.policy(config) if llm else None,
        }
    )
    owner, deadline = uuid.uuid4().hex, time.monotonic() + config.wait_seconds
    random_draw = random.random()
    while True:
        entry = store.get(key)
        rejected = False
        rejection_reason = "refresh"
        if entry:
            created, payload = entry
            if diagnostic is not None:
                diagnostic.observe(key, created, payload)
            probability = refresh_probability(
                time.time() - created,
                config.refresh_start,
                config.refresh_force,
            )
            if random_draw >= probability and probability < 1:
                try:
                    rejection_reason = "validator_error"
                    if config.validator and not config.validator(
                        payload["input"], body
                    ):
                        rejection_reason = "validator_rejected"
                        raise ValueError("Validator rejected the candidate")
                    rejection_reason = "input_changed"
                    if dumps(payload["input"]) != dumps(body):
                        raise ValueError("Stored input differs from exact key")
                    result = payload["result"]
                    # Check decoding before advertising a hit.
                    rejection_reason = "decode_failed"
                    adapters.unpack(result)
                except Exception as exc:
                    rejected = True
                    if diagnostic is not None:
                        diagnostic.reject(
                            key,
                            rejection_reason,
                            error=type(exc).__name__,
                        )
                else:
                    record(
                        "hit",
                        "normalized" if payload["input"] != body else "exact",
                        config,
                        key,
                    )
                    return None, result
            elif diagnostic is not None:
                diagnostic.reject(
                    key, "refresh", refresh_probability=probability
                )
        if normalized is None:
            normalized = (
                normalize(body, config.mode, config.rules)
                if diagnostic is not None
                else Normalized(body, {})
            )
        if store.claim(key, owner, config.lease_seconds):
            # Another producer may have committed between get() and claim().
            # Recheck under our lease before making a duplicate upstream call.
            latest = store.get(key)
            if latest != entry:
                store.release(key, owner)
                continue
            if (
                entry is None
                and matching_scope
                and (verifier or config.metadata_rules)
            ):
                try:
                    result = matching.lookup(
                        store,
                        matching_scope,
                        body,
                        normalized,
                        config,
                        verifier,
                        diagnostic=diagnostic,
                        fingerprints=signature_info[1]
                        if signature_info
                        else None,
                    )
                    if result is not None:
                        result, reuse_reason = result
                        adapters.unpack(result)
                        # Keep the original entry's age for pair reuse: a new
                        # input must not renew an old answer's freshness window.
                        store.release(key, owner)
                        record("hit", reuse_reason, config, key)
                        return None, result
                except BaseException:
                    store.release(key, owner)
                    raise
            miss(
                rejection_reason
                if rejected
                else "refresh"
                if entry
                else "cold",
                key,
                store,
                diagnostic_scope,
                normalized,
            )
            return Ticket(
                store,
                key,
                owner,
                body,
                normalized,
                config,
                matching_scope,
                diagnostic_scope,
                signature_info,
            ), None
        if time.monotonic() >= deadline:
            miss("busy", key)
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


def code_fingerprint(value):
    """Serialize code constants without reference-sharing or slice limitations.

    The rewritten code object is only hashed, never executed or loaded.
    Type prefixes prevent a slice and a tuple of its bounds from colliding.
    """
    if isinstance(value, types.CodeType):
        value = value.replace(
            co_consts=tuple(code_fingerprint(item) for item in value.co_consts)
        )
        return b"code:" + marshal.dumps(value, MARSHAL_VERSION)
    if isinstance(value, slice):
        return b"slice:" + code_fingerprint(
            (value.start, value.stop, value.step)
        )
    if isinstance(value, (tuple, frozenset)):
        items = [code_fingerprint(item) for item in value]
        if isinstance(value, frozenset):
            items.sort()
        return type(value).__name__.encode() + marshal.dumps(
            items, MARSHAL_VERSION
        )
    return b"value:" + marshal.dumps(value, MARSHAL_VERSION)


def decorate(function, llm, overrides, version):
    identity = digest(
        {
            "function": f"{function.__module__}.{function.__qualname__}",
            "version": version
            or hashlib.sha256(code_fingerprint(function.__code__)).hexdigest(),
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

    def ready(body, extra, config, verifier=None, original_input=None):
        try:
            if llm and config.signed_call_handles:
                # Key on stable handles for provider-signed call tokens; the
                # live input and the stored response keep their real bytes.
                body = signed_handles(body)
            return prepare(
                body,
                scope(extra),
                config,
                llm,
                verifier or config.verifier_overrider,
                original_input,
            )
        except Exception as exc:
            LOGGER.debug(
                "Cache lookup unavailable error=%s", type(exc).__name__
            )
            details = None
            if llm and config.diagnostics:
                try:
                    details = MissDiagnostic(
                        body, config, original_input
                    ).finish()
                except Exception:
                    details = {}
                details["error"] = type(exc).__name__
            decision(
                "miss", "unsupported_or_unavailable", config, diagnostic=details
            )
            return None, None

    def alias_session(body, extra, config):
        try:
            return adapters.TestAliases(body, scope(extra), config)
        except Exception as exc:
            details = None
            if config.diagnostics:
                details = {"error": type(exc).__name__}
                try:
                    details.update(MissDiagnostic(body, config).finish())
                except Exception:
                    pass
            decision(
                "miss", "test_aliases_rejected", config, diagnostic=details
            )
            raise

    @functools.wraps(function)
    def sync(*args, **kwargs):
        kwargs, use_cache = arguments(kwargs)
        config = configuration()
        alias_enabled = (
            llm and config.test_aliases and config.mode in ("testing", "risky")
        )
        metadata_enabled = (
            llm and config.test_metadata and config.mode in ("testing", "risky")
        )
        if (llm and config.mode == "disabled") or (
            not use_cache and not alias_enabled and not metadata_enabled
        ):
            return function(*args, **kwargs)
        try:
            body = adapters.function_input(function, args, kwargs)
        except Exception:
            decision("miss", "unsupported_input", config)
            return function(*args, **kwargs)
        original_input = body
        aliases = alias_session(body, None, config) if alias_enabled else None
        if aliases and not aliases.mapping:
            aliases = None
        if aliases:
            body = aliases.body
        if metadata_enabled:
            body = project_test_metadata(body, config.test_metadata)
        if aliases or metadata_enabled:
            args, kwargs = adapters.replace_input(function, args, kwargs, body)
        verifier = config.verifier_overrider
        if (
            verifier is None
            and llm
            and config.learning
            and config.mode in ("testing", "risky")
        ):
            verifier = functools.partial(
                adapters.verify_function, function, args, kwargs, body
            )
        ticket, cached = (
            ready(body, None, config, verifier, original_input)
            if use_cache
            else (None, None)
        )
        if cached is not None:
            return adapters.unpack(
                aliases.output(cached) if aliases else cached
            )
        try:
            result = function(*args, **kwargs)
            if aliases:
                packed = adapters.pack(result)
                rendered = aliases.output(packed)
                if ticket:
                    ticket.save(packed)
                return adapters.unpack(rendered)
            if ticket:
                try:
                    ticket.save(adapters.pack(result))
                except Exception as exc:
                    LOGGER.debug(
                        "Unsupported cache result error=%s", type(exc).__name__
                    )
            return result
        finally:
            if ticket:
                ticket.release()

    @functools.wraps(function)
    async def async_(*args, **kwargs):
        kwargs, use_cache = arguments(kwargs)
        config = configuration()
        alias_enabled = (
            llm and config.test_aliases and config.mode in ("testing", "risky")
        )
        metadata_enabled = (
            llm and config.test_metadata and config.mode in ("testing", "risky")
        )
        if (llm and config.mode == "disabled") or (
            not use_cache and not alias_enabled and not metadata_enabled
        ):
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
        original_input = body
        aliases = (
            await asyncio.to_thread(alias_session, body, extra, config)
            if alias_enabled
            else None
        )
        if aliases and not aliases.mapping:
            aliases = None
        if aliases:
            body = aliases.body
        if metadata_enabled:
            body = project_test_metadata(body, config.test_metadata)
        if aliases or metadata_enabled:
            args, kwargs = adapters.replace_input(function, args, kwargs, body)
            request = adapters.http_request(args, kwargs)
        verifier = config.verifier_overrider
        if (
            llm
            and verifier is None
            and config.learning
            and config.mode in ("testing", "risky")
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

        ticket, cached = (
            await async_lookup(
                ready, body, extra, config, verifier, original_input
            )
            if use_cache
            else (None, None)
        )
        if cached is not None:
            rendered = (
                await asyncio.to_thread(aliases.output, cached)
                if aliases
                else cached
            )
            return adapters.unpack(rendered)
        try:
            result = await function(*args, **kwargs)
        except BaseException:
            if ticket:
                ticket.release()
            raise
        if aliases and aliases.mapping:
            if getattr(result, "status_code", 200) != 200:
                if ticket:
                    ticket.release()
                return result
            try:
                if hasattr(result, "body_iterator"):
                    if (
                        "text/event-stream"
                        not in result.headers.get("content-type", "")
                        or "set-cookie" in result.headers
                        or "content-encoding" in result.headers
                    ):
                        raise ValueError(
                            "Unsupported test-alias response stream"
                        )
                    chunks, size, tail = [], 0, b""
                    async for chunk in result.body_iterator:
                        raw = (
                            chunk.encode() if isinstance(chunk, str) else chunk
                        )
                        size += len(raw)
                        if size > config.max_entry_bytes:
                            raise ValueError(
                                "Test-alias stream exceeds buffer limit"
                            )
                        chunks.append(raw)
                        tail = (tail + raw)[-128:]
                        if b"data: [DONE]\n\n" in tail.replace(b"\r\n", b"\n"):
                            break
                    packed = adapters.completed_sse(
                        b"".join(chunks), result.headers
                    )
                else:
                    packed = adapters.pack(result)
                rendered = await asyncio.to_thread(aliases.output, packed)
                if ticket:
                    await asyncio.to_thread(ticket.save, packed)
                return adapters.unpack(rendered)
            finally:
                if ticket:
                    ticket.release()
                close = getattr(
                    getattr(result, "body_iterator", None), "aclose", None
                )
                if close:
                    await close()
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
                                except Exception as exc:
                                    LOGGER.debug(
                                        "Stream could not be cached error=%s",
                                        type(exc).__name__,
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
        except Exception as exc:
            ticket.release()
            LOGGER.debug(
                "Unsupported cache result error=%s", type(exc).__name__
            )
        return result

    wrapper = async_ if inspect.iscoroutinefunction(function) else sync
    wrapper.__signature__ = signature
    wrapper.cache_stats = cache_stats
    wrapper.cache_misses = cache_misses
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
            with decision (SAFE, UNSAFE or UNCERTAIN), reason (str), and segments
            (all indexes), synchronously. Legacy safe_to_reuse is also accepted.
            Supported chat wrappers use the package prompt and original function.
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
