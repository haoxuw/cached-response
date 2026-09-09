"""Observational miss diagnostics; similarity never authorizes cache reuse."""

import json
import os
import time
import unicodedata
from collections import Counter, deque
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .normalize import dumps, normalize, strings

MAX_CANDIDATES = 8
MAX_SIGNATURE_CHARS = 65_536
MAX_DIFFERENCES = 12
SCHEMA_KEYS = {
    "messages",
    "content",
    "role",
    "model",
    "tool_calls",
    "function",
    "arguments",
    "name",
    "id",
    "tool_call_id",
    "text",
    "type",
    "thought_signature",
    "thoughtSignature",
    "signature",
    "provider_specific_fields",
    "extra_content",
}
_examples = deque(maxlen=20)
_examples_lock = Lock()


def redact(text, *, offset=0, total=None):
    """Keep four edge characters and symbols; mask interior letters/numbers.

    Output is capped at 160 characters while retaining the original suffix.
    Strings of eight characters or fewer have no interior to mask.
    """
    if total is not None:
        return "".join(
            "*" if 4 <= offset + index < total - 4 and char.isalnum() else char
            for index, char in enumerate(text)
        )
    if len(text) > 160:
        text = text[:156] + text[-4:]
    return "".join(
        "*" if 4 <= index < len(text) - 4 and char.isalnum() else char
        for index, char in enumerate(text)
    )


def remember(event):
    with _examples_lock:
        _examples.append(deepcopy(event))


def cache_misses(limit=5):
    """Return recent process-local miss examples, newest first (at most 20).

    Examples contain masked text unless diagnostic_text=True was explicitly set.
    No cache payloads are retained here. Returned values are independent copies.
    """
    if not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    with _examples_lock:
        return deepcopy(list(reversed(_examples))[:limit])


def prompt_stats(body):
    """Count message content (or string arguments), excluding JSON framing."""
    if isinstance(body, dict) and isinstance(body.get("messages"), list):
        body = [message.get("content") for message in body["messages"]]
    result = dict(chars=0, bytes=0, words=0, symbols=0, digits=0, lines=0)
    for text in strings(body):
        counts = Counter(text)
        result["chars"] += len(text)
        result["bytes"] += len(text.encode())
        result["words"] += len(text.split())
        result["symbols"] += sum(
            count
            for char, count in counts.items()
            if unicodedata.category(char)[0] in "PS"
        )
        result["digits"] += sum(
            count for char, count in counts.items() if char.isdigit()
        )
        result["lines"] += len(text.splitlines())
    return result


def binding_summary(previous, current):
    """Describe identifier relationships without revealing their values."""
    return {
        "previous_reference_count": len(previous),
        "current_reference_count": len(current),
        "reference_structure_matches": previous.keys() == current.keys(),
    }


def signature(value):
    text = dumps(value)
    length = len(text)
    if length > MAX_SIGNATURE_CHARS:
        half = MAX_SIGNATURE_CHARS // 2
        text = text[:half] + text[-half:]
    return Counter(text[i : i + 3] for i in range(len(text))), length


def similarity(left, right):
    """Character-trigram Dice overlap, penalized for full-length differences."""
    a, a_length = left
    b, b_length = right
    overlap = sum((a & b).values())
    score = 2 * overlap / (sum(a.values()) + sum(b.values()))
    return score * min(a_length, b_length) / max(a_length, b_length)


@lru_cache(maxsize=32)
def _candidate_summary(text, mode, rules):
    body = json.loads(text)
    return signature(normalize(body, mode, rules).body), prompt_stats(body)


@contextmanager
def capture_misses(
    path, *, include_text=False, limit=10, seconds=300, max_bytes=8_000_000
):
    """Temporarily capture misses in this process to a new private JSONL file.

    Raw full pairs require include_text=True. Limits bound records, duration and
    file bytes; oversized records are skipped, never silently truncated. Install
    this in the process serving calls, with caching enabled. Defaults stay silent.
    """
    from . import config

    if min(limit, seconds, max_bytes) <= 0:
        raise ValueError("Capture limits must be positive")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = {"written": 0, "skipped": 0, "bytes": 0}
    lock, deadline = Lock(), time.monotonic() + seconds
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:

        def capture(diagnostic, details):
            with lock:
                if (
                    output.closed
                    or status["written"] >= limit
                    or time.monotonic() >= deadline
                ):
                    return
                event = {
                    "event": "cache_miss",
                    "redacted": not include_text,
                    "prompt": details.get("prompt"),
                    "candidates": [],
                }
                for item in details.get("candidates", [])[:1]:
                    payload = diagnostic.candidates[item["key"]]["payload"]
                    changes, truncated = differences(
                        payload["input"], diagnostic.body, include_text
                    )
                    event["candidates"].append(
                        {
                            "key": item["key"],
                            "reason": item["reason"],
                            "differences": changes,
                            "differences_truncated": truncated,
                            **(
                                {
                                    "input": payload["input"],
                                    "response": payload["result"],
                                }
                                if include_text
                                else {}
                            ),
                        }
                    )
                if include_text:
                    event["input"] = diagnostic.body
                line = dumps(event) + "\n"
                size = len(line.encode())
                if status["bytes"] + size > max_bytes:
                    status["skipped"] += 1
                    return
                output.write(line)
                output.flush()
                status["written"] += 1
                status["bytes"] += size

        previous = config.settings().diagnostic_capture
        config.configure(diagnostic_capture=capture)
        try:
            yield status
        finally:
            if config.settings().diagnostic_capture is capture:
                config.configure(diagnostic_capture=previous)
            with lock:
                output.close()


def differences(before, after, include_text=False):
    """Bounded structural differences with optional excerpts around edits."""
    changes = []
    truncated = False

    def walk(old, new, path):
        nonlocal truncated
        if old == new and type(old) is type(new):
            return
        if len(changes) >= MAX_DIFFERENCES:
            truncated = True
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                if key in old and key in new:
                    walk(old[key], new[key], [*path, key])
                else:
                    add(
                        old.get(key),
                        new.get(key),
                        [*path, key],
                        "added" if key in new else "removed",
                    )
            return
        if isinstance(old, list) and isinstance(new, list):
            for index in range(max(len(old), len(new))):
                if index < min(len(old), len(new)):
                    walk(old[index], new[index], [*path, index])
                else:
                    add(
                        old[index] if index < len(old) else None,
                        new[index] if index < len(new) else None,
                        [*path, index],
                        "added" if index >= len(old) else "removed",
                    )
            return
        add(old, new, path, "changed")

    def add(old, new, path, kind):
        nonlocal truncated
        if len(changes) >= MAX_DIFFERENCES:
            truncated = True
            return
        left = old if isinstance(old, str) else dumps(old)
        right = new if isinstance(new, str) else dumps(new)
        change = {
            "path": [
                part
                if include_text or isinstance(part, int) or part in SCHEMA_KEYS
                else redact(part)
                for part in path
            ],
            "kind": kind,
            "before_chars": len(left),
            "after_chars": len(right),
        }
        # String comparisons run in C; avoid a Python loop over long boilerplate.
        start, end = 0, min(len(left), len(right))
        while start < end:
            middle = (start + end + 1) // 2
            if left[:middle] == right[:middle]:
                start = middle
            else:
                end = middle - 1
        start = max(0, start - 30)
        left_excerpt, right_excerpt = (
            left[start : start + 160],
            right[start : start + 160],
        )
        change.update(
            before=left_excerpt
            if include_text
            else redact(left_excerpt, offset=start, total=len(left)),
            after=right_excerpt
            if include_text
            else redact(right_excerpt, offset=start, total=len(right)),
            offset=start,
            redacted=not include_text,
        )
        changes.append(change)

    walk(before, after, [])
    return changes, truncated


class MissDiagnostic:
    def __init__(self, body, config):
        self.body = body
        self.config = config
        self.candidates = {}

    def observe(self, key, created, payload):
        self.candidates[key] = {
            "created": created,
            "payload": payload,
            "checks": [],
        }

    def reject(self, key, reason, **details):
        checks = self.candidates[key]["checks"]
        # Keep a bounded trace while retaining the final rejection reason.
        if len(checks) == 16:
            checks.pop()
            self.candidates[key]["checks_truncated"] = True
        checks.append({"reason": reason, **details})

    def finish(self, store=None, scope=None, normalized=None, reason="cold"):
        result = {"prompt": prompt_stats(self.body), "candidates": []}
        if store is not None and scope and not self.candidates:
            for key, created, payload in store.candidates(
                scope, MAX_CANDIDATES
            ):
                self.observe(key, created, payload)

                def roles(value):
                    if not isinstance(value, dict):
                        return None
                    messages = value.get("messages")
                    if not isinstance(messages, list) or not all(
                        isinstance(message, dict) for message in messages
                    ):
                        return None
                    return [message.get("role") for message in messages]

                old_roles = roles(payload.get("input"))
                new_roles = roles(self.body)
                if old_roles != new_roles:
                    self.reject(
                        key,
                        "message_role_sequence_changed",
                        previous_message_count=len(old_roles),
                        current_message_count=len(new_roles),
                    )
                    continue
                self.reject(
                    key,
                    "verification_disabled"
                    if (
                        self.config.mode not in ("testing", "risky")
                        or not self.config.learning
                    )
                    else "verification_unavailable",
                )
        if normalized is None:
            return result
        current_signature = signature(normalized.body)
        for key, candidate in self.candidates.items():
            checks = candidate["checks"]
            record = {
                "key": key,
                "age_seconds": round(
                    max(0, time.time() - candidate["created"]), 3
                ),
                "reason": checks[-1]["reason"] if checks else reason,
                "checks": checks,
                "checks_truncated": candidate.get("checks_truncated", False),
                **candidate.get("retrieval", {}),
            }
            try:
                old = candidate["payload"]["input"]
                text = dumps(old)
                # Retain at most 32 strings of 262,144 characters.
                summarize = (
                    _candidate_summary
                    if len(text) <= 262_144
                    else _candidate_summary.__wrapped__
                )
                old_signature, stats = summarize(
                    text, self.config.mode, tuple(self.config.rules)
                )
                score = similarity(current_signature, old_signature)
                changes, truncated = differences(
                    old, self.body, self.config.diagnostic_text
                )
                record.update(
                    signature_similarity=round(score, 4),
                    signature_sampled=max(
                        current_signature[1], old_signature[1]
                    )
                    > MAX_SIGNATURE_CHARS,
                    high_similarity=score >= self.config.near_miss_threshold,
                    prompt=stats,
                    differences=changes,
                    differences_truncated=truncated,
                )
            except Exception as exc:
                record["diagnostic_error"] = type(exc).__name__
            result["candidates"].append(record)
        result["candidates"].sort(
            key=lambda item: item.get("signature_similarity", -1), reverse=True
        )
        result["near_miss"] = any(
            item.get("high_similarity", False) for item in result["candidates"]
        )
        result["candidate_limit"] = MAX_CANDIDATES * (
            (3 if self.config.signature_matching else 1)
            + bool(self.config.metadata_paths or self.config.metadata_rules)
        )
        result["near_miss_threshold"] = self.config.near_miss_threshold
        if result["near_miss"]:
            result["next_step"] = {
                "skill": "configure-cached-response",
                "read": "cached-response --skill",
                "capture": "capture_misses(path, include_text=True, limit=10)",
                "requires": "authorized test traffic in the serving process",
            }
        return result
