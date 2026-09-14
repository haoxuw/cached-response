"""Small, deterministic normalizers. Format recognition is a heuristic."""

import copy
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass

MARKER = "⟪SLC:"
UUID = r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
ISO_TIME = (
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
SYMBOL_TOKEN = r"(?<![\w/+])(?=[A-Za-z0-9_+=~-]{16,}(?![\w/+]))[A-Za-z0-9_+=~-]{16,}(?![\w/+])"
PREFIXED_HEX = re.compile(r"\b([A-Za-z][A-Za-z0-9]*[_-])([0-9a-fA-F]{8,})\b")
PROTECTED = {
    "thought_signature",
    "thoughtSignature",
    "thought_signatures",
    "thoughtSignatures",
    "signature",
    "provider_specific_fields",
    "extra_content",
    "encrypted_content",
    "reasoning_content",
    "reasoning",
    "redacted_thinking",
    "thinking",
    "thinking_blocks",
}
MIN_SYMBOL_FRACTION = 0.30
URL = re.compile(r"https?://[^\s\"<>]+")
THOUGHT_SEPARATOR = "__thought__"
SIGNED_ID = re.compile(r"\bcall_[A-Za-z0-9_-]+__thought__[A-Za-z0-9+/=_-]+")
CALL_ID_FIELDS = {"id", "tool_call_id"}


def dumps(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in PROTECTED:
                yield from strings(item)


def word_count(body):
    if isinstance(body, dict) and isinstance(body.get("messages"), list):
        text = strings([message.get("content") for message in body["messages"]])
    else:
        text = strings(body)
    return sum(len(part.split()) for part in text)


def project_test_metadata(body, rules):
    """Apply an explicit test contract only to linked tool-result numbers."""
    if (
        not rules
        or not isinstance(body, dict)
        or not isinstance(body.get("messages"), list)
    ):
        return body
    result = copy.deepcopy(body)
    calls = {}
    matched = False

    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Ambiguous JSON object")
        return value

    def walk(value, path, selected):
        nonlocal matched
        if isinstance(value, dict):
            return {
                key: item
                if key in PROTECTED or key == "*"
                else walk(item, (*path, key), selected)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item, (*path, "*"), selected) for item in value]
        for rule in selected:
            kind = int if rule.value_type == "int" else float
            if path in rule.paths and type(value) is kind:
                matched = True
                return kind(0)
        return value

    for message in result.get("messages", []):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                token = call.get("id")
                function = call.get("function")
                if isinstance(token, str) and isinstance(function, dict):
                    # Reused IDs cannot establish an unambiguous tool link.
                    calls[token] = (
                        None if token in calls else function.get("name")
                    )
        if message.get("role") != "tool":
            continue
        token = message.get("tool_call_id")
        tool = calls.get(token) if isinstance(token, str) else None
        if not tool or message.get("name", tool) != tool:
            continue
        selected = [rule for rule in rules if rule.tool == tool]
        content = message.get("content")
        matched = False
        if not selected:
            continue
        if isinstance(content, str):
            try:
                parsed = json.loads(content, object_pairs_hook=unique_object)
                dumps(parsed)  # Nonfinite numbers are not valid JSON metadata.
            except ValueError:
                continue
            projected = walk(parsed, (), selected)
            if matched:
                message["content"] = dumps(projected)
        elif isinstance(content, (dict, list)):
            message["content"] = walk(content, (), selected)
    return result


@dataclass
class Normalized:
    body: object
    bindings: dict


def normalize(body, mode, rules=()):
    """Normalize message values; model parameters and tool schemas stay exact."""
    if any(MARKER in text for text in strings(body)):
        raise ValueError("Input contains a reserved cache placeholder")
    bindings, indices = {}, {}
    common = [("UUID", re.compile(UUID)), ("TIME", re.compile(ISO_TIME))]
    custom = [(rule, re.compile(rule.pattern)) for rule in rules]

    def bind(kind, value):
        identity = (kind, dumps(value))
        if identity not in indices:
            marker = f"{MARKER}{len(indices)}:{kind}⟫"
            indices[identity] = marker
            bindings[marker] = value
        return indices[identity]

    def redact(text, path):
        # Embedded tool-result/argument JSON has real field names worth keeping.
        try:
            nested = json.loads(text)
        except (ValueError, TypeError):
            nested = None
        if isinstance(nested, (dict, list)):
            return dumps(walk(nested, path))
        matches = []
        for kind, pattern in common:
            matches.extend(
                (m.start(), m.end(), kind, m.group())
                for m in pattern.finditer(text)
            )
        if mode != "conservative":
            for match in PREFIXED_HEX.finditer(text):
                encoded = match.group(2)
                if any(c.isalpha() for c in encoded) and any(
                    c.isdigit() for c in encoded
                ):
                    # Recognize an encoding, not application-specific ID names.
                    # The prefix remains part of the kind and cannot be ignored.
                    matches.append(
                        (
                            match.start(),
                            match.end(),
                            "HEX_" + match.group(1),
                            match.group(),
                        )
                    )
        for match in re.finditer(SYMBOL_TOKEN, text):
            value = match.group()
            symbols = sum(not c.isalnum() for c in value)
            if (
                symbols / len(value) >= MIN_SYMBOL_FRACTION
                and any(c.isalpha() for c in value)
                and any(c.isdigit() for c in value)
            ):
                matches.append((match.start(), match.end(), "SYMBOL", value))
        for rule, pattern in custom:
            if any(
                fnmatch.fnmatchcase(path, allowed) for allowed in rule.paths
            ):
                matches.extend(
                    (m.start(), m.end(), rule.name, m.group())
                    for m in pattern.finditer(text)
                    if m.end() > m.start()
                )
        # Select non-overlapping spans. Longest wins when rules share a start.
        protected_spans = [
            (match.start(), match.end())
            for pattern in (URL, SIGNED_ID)
            for match in pattern.finditer(text)
        ]
        output, offset = [], 0
        for start, end, kind, value in sorted(
            matches, key=lambda m: (m[0], -(m[1] - m[0]), m[2])
        ):
            if start < offset or any(
                start < right and end > left for left, right in protected_spans
            ):
                continue
            output.extend((text[offset:start], bind(kind, value)))
            offset = end
        output.append(text[offset:])
        return "".join(output)

    def walk(value, path="", key=""):
        if key in PROTECTED:
            return value
        if isinstance(value, dict):
            result = {}
            for child_key in sorted(value):
                child_path = f"{path}.{child_key}" if path else child_key
                result[child_key] = walk(
                    value[child_key], child_path, child_key
                )
            return result
        if isinstance(value, list):
            return [
                walk(item, f"{path}.{index}")
                for index, item in enumerate(value)
            ]
        if isinstance(value, str):
            if (
                key in CALL_ID_FIELDS
                and value.startswith("call_")
                and THOUGHT_SEPARATOR in value
            ):
                prefix, signature = value.split(THOUGHT_SEPARATOR, 1)
                return (
                    (bind("CALL", prefix) + THOUGHT_SEPARATOR + signature)
                    if mode != "conservative"
                    else value
                )
            return redact(value, path)
        return value

    if isinstance(body, dict) and "messages" in body:
        canonical = dict(body)
        canonical["messages"] = walk(body["messages"], "messages")
    else:
        canonical = walk(body)
    return Normalized(canonical, bindings)


def rebind(value, previous, current):
    """Simultaneous, bounded substitutions; never cascade replacements."""
    if previous.keys() != current.keys():
        raise ValueError("Reference structure differs")
    replacements = {}
    for marker, old in previous.items():
        new = current[marker]
        if type(old) is not type(new):
            raise ValueError("Reference types differ")
        if old != new:
            token = str(old)
            if token in replacements and replacements[token] != str(new):
                raise ValueError("Ambiguous output substitution")
            replacements[token] = str(new)
    pattern = (
        re.compile(
            r"(?<!\w)(?:"
            + "|".join(
                re.escape(v)
                for v in sorted(replacements, key=len, reverse=True)
            )
            + r")(?!\w)"
        )
        if replacements
        else None
    )

    def walk(item, key=""):
        if key in PROTECTED:
            return item
        if isinstance(item, dict):
            if any(old in key for key in item for old in replacements):
                raise ValueError(
                    "A reference appears in an output dictionary key"
                )
            return {k: walk(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, str) and pattern:
            if (
                key in CALL_ID_FIELDS
                and item.startswith("call_")
                and THOUGHT_SEPARATOR in item
            ):
                prefix, signature = item.split(THOUGHT_SEPARATOR, 1)
                return (
                    replacements.get(prefix, prefix)
                    + THOUGHT_SEPARATOR
                    + signature
                )
            # Tool arguments are JSON strings. Parse them before rewriting so
            # numeric metadata cannot accidentally rewrite unrelated counts.
            try:
                nested = json.loads(item)
            except ValueError:
                nested = None
            if isinstance(nested, (dict, list)):
                return dumps(walk(nested))

            def substitute(match):
                old = match.group()
                if old.isdigit() and len(old) < 10:
                    raise ValueError(
                        "A short numeric reference appears in output text"
                    )
                return replacements[old]

            matches = list(pattern.finditer(item))
            covered = {match.span() for match in matches}
            for old in replacements:
                if any(
                    match.span() not in covered
                    for match in re.finditer(re.escape(old), item)
                ):
                    raise ValueError(
                        "A reference is embedded in an unmapped output token"
                    )
            return pattern.sub(substitute, item)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            for marker, old in previous.items():
                if (
                    type(item) is type(old)
                    and item == old
                    and old != current[marker]
                ):
                    raise ValueError(
                        "A numeric reference appears in the output"
                    )
        return item

    return walk(value)


def translate(value, mapping):
    """Translate explicit opaque test handles, preserving JSON text and signatures."""
    pairs = {a: b for a, b in mapping.items() if a != b}
    if not pairs:
        return value
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(map(re.escape, pairs)) + r")(?!\w)"
    )

    def check_nested(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if any(old in key for old in pairs):
                    raise ValueError("Test ID in a dictionary key")
                if key in PROTECTED and pattern.search(dumps(child)):
                    raise ValueError(
                        "Test ID occurs inside opaque provider state"
                    )
                check_nested(child)
        elif isinstance(item, list):
            for child in item:
                check_nested(child)

    def walk(item):
        if isinstance(item, dict):
            if any(old in key for key in item for old in pairs):
                raise ValueError("Test ID in a dictionary key")
            return {
                k: v if k in PROTECTED else walk(v) for k, v in item.items()
            }
        if isinstance(item, list):
            return [walk(v) for v in item]
        if not isinstance(item, str):
            return item
        spans = {m.span() for m in pattern.finditer(item)}
        if any(
            m.span() not in spans
            for old in pairs
            for m in re.finditer(re.escape(old), item)
        ):
            raise ValueError("Test ID is embedded in another token")
        if not spans:
            return item
        if THOUGHT_SEPARATOR in item:
            raise ValueError("Cannot translate a signed provider token")
        try:
            nested = json.loads(item)
        except ValueError:
            nested = None
        check_nested(nested)
        return pattern.sub(lambda m: pairs[m[0]], item)

    return walk(value)
