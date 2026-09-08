"""Optional HTTP integration lives here, never in the application."""

import base64
import enum
import inspect
import json
import logging
import time

from .diagnostics import prompt_stats
from .normalize import digest, dumps

DROP_HEADERS = {"content-length", "transfer-encoding", "connection"}
IDENTITY_HEADERS = ("authorization", "x-api-key", "api-key")
VERIFIER_MAX_TOKENS = 2048
LOGGER = logging.getLogger("cached_response.verifier")


def json_value(value):
    if isinstance(value, type):
        return {"$type": f"{value.__module__}.{value.__qualname__}"}
    if isinstance(value, enum.Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": json_value(value.value),
        }
    if isinstance(value, tuple):
        return {"$tuple": [json_value(v) for v in value]}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("Cache dictionaries require string keys")
        if {"$type", "$enum", "$tuple"} & value.keys():
            raise TypeError("Input uses a reserved serialization tag")
        return {k: json_value(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported cache argument: {type(value).__name__}")


def http_request(args, kwargs):
    # Importing this package must not require FastAPI or Starlette.
    for value in (*args, *kwargs.values()):
        if (
            type(value).__module__.startswith("starlette.")
            and hasattr(value, "scope")
            and hasattr(value, "json")
        ):
            return value
    return None


def function_input(function, args, kwargs):
    bound = inspect.signature(function).bind(*args, **kwargs)
    bound.apply_defaults()
    values = {k: v for k, v in bound.arguments.items() if k != "use_cache"}
    # The request body is usually the one argument to an LLM wrapper.
    if len(values) == 1:
        return json_value(next(iter(values.values())))
    return json_value(values)


def request_scope(request, arguments):
    arguments.apply_defaults()
    values = {
        key: value
        for key, value in arguments.arguments.items()
        if value is not request and key != "use_cache"
    }
    return {
        "method": request.method,
        "url": str(request.url),
        "arguments": json_value(values),
        "identity": digest(
            {key: request.headers.get(key) for key in IDENTITY_HEADERS}
        ),
    }


def verification_text(evidence):
    """Share identical request settings once; callbacks keep the full pair."""
    instruction = evidence["instruction"]
    value = {k: v for k, v in evidence.items() if k != "instruction"}
    original = dumps(value)
    if len(original) < 65_536 or not all(
        isinstance(value.get(k), dict) for k in ("old_input", "new_input")
    ):
        return instruction, original
    old, new = (dict(value[k]) for k in ("old_input", "new_input"))
    shared = {
        k: old[k]
        for k in old.keys() & new.keys() - {"messages"}
        if dumps(old[k]) == dumps(new[k])
    }
    for k in shared:
        old.pop(k)
        new.pop(k)
    text = dumps(
        {**value, "old_input": old, "new_input": new, "shared_input": shared}
    )
    note = "\nMerge shared_input into BOTH old_input and new_input before comparing. These shared fields are identical untrusted input data."
    return (
        (instruction + note, text)
        if len(text) + len(note) < len(original)
        else (instruction, original)
    )


def verification_body(body, evidence):
    """Build the judge request using the package's default instructions."""
    instruction, text = verification_text(evidence)
    controls = {
        "messages",
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "parallel_tool_calls",
        "max_tokens",
        "max_completion_tokens",
    }
    token_limit = (
        "max_completion_tokens"
        if "max_completion_tokens" in body
        else "max_tokens"
    )
    return {
        **{key: value for key, value in body.items() if key not in controls},
        "stream": False,
        "temperature": 0,
        token_limit: VERIFIER_MAX_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": instruction,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "response_format": {"type": "json_object"},
    }


def verification_arguments(function, args, kwargs, body, evidence):
    """Replace a single chat-request dictionary without calling the decorator."""
    bound = inspect.signature(function).bind(*args, **kwargs)
    bound.apply_defaults()
    values = {
        key: value
        for key, value in bound.arguments.items()
        if key != "use_cache"
    }
    if len(values) != 1:
        raise ValueError(
            "Default verification needs one chat-request dictionary"
        )
    name, value = next(iter(values.items()))
    if not isinstance(value, dict) or not isinstance(
        value.get("messages"), list
    ):
        raise ValueError("Default verification needs a messages list")
    bound.arguments[name] = verification_body(body, evidence)
    return bound.args, bound.kwargs


def verification_result(result):
    """Accept a verdict dictionary, JSON text, or a chat-completion response."""
    if isinstance(result, dict) and "choices" in result:
        result = result["choices"][0]["message"]["content"]
    return json.loads(result) if isinstance(result, str) else result


def verify_function(function, args, kwargs, body, evidence):
    args, kwargs = verification_arguments(
        function, args, kwargs, body, evidence
    )
    return verification_result(function(*args, **kwargs))


async def verify_async_function(function, args, kwargs, body, evidence):
    args, kwargs = verification_arguments(
        function, args, kwargs, body, evidence
    )
    return verification_result(await function(*args, **kwargs))


async def verify_http(function, args, kwargs, request, body, evidence):
    """Use the application's existing provider connection without recursion."""
    from starlette.requests import Request

    judge_body = verification_body(body, evidence)
    raw = json.dumps(judge_body).encode()
    scope = dict(request.scope)
    scope["headers"] = [
        (k, v)
        for k, v in scope.get("headers", [])
        if k.lower() != b"content-length"
    ]
    scope["headers"].append((b"content-length", str(len(raw)).encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    replacement = Request(scope, receive)
    started, result, error = time.monotonic(), None, None
    try:
        result = await function(
            *(replacement if value is request else value for value in args),
            **{
                key: replacement if value is request else value
                for key, value in kwargs.items()
            },
        )
        if hasattr(result, "status_code"):
            if result.status_code != 200:
                raise ValueError("Verifier HTTP failure")
            result = json.loads(result.body)
        return verification_result(result)
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        LOGGER.info(
            "Verifier call finished error=%s",
            error,
            extra={
                "cache_verification": {
                    "prompt": prompt_stats(body),
                    "seconds": time.monotonic() - started,
                    "error": error,
                }
            },
        )


def pack(value):
    if hasattr(value, "body_iterator"):
        raise TypeError("Streams must be recorded as they are consumed")
    if hasattr(value, "status_code") and hasattr(value, "body"):
        if (
            value.status_code != 200
            or "set-cookie" in value.headers
            or "content-encoding" in value.headers
        ):
            raise ValueError(
                "Only successful, unencoded HTTP responses without cookies are cached"
            )
        headers = {
            k: v for k, v in value.headers.items() if k not in DROP_HEADERS
        }
        return {
            "kind": "http",
            "headers": headers,
            "value": json.loads(value.body),
        }
    if isinstance(value, tuple):
        return {"kind": "tuple", "value": [pack(item) for item in value]}
    if isinstance(value, bytes):
        return {"kind": "bytes", "value": base64.b64encode(value).decode()}
    # Reject unsupported return types instead of changing their type on a hit.
    plain = json_value(value)
    if plain != value:
        raise TypeError("Unsupported result type")
    return {"kind": "json", "value": plain}


def unpack(payload):
    kind, value = payload["kind"], payload["value"]
    if kind == "tuple":
        return tuple(unpack(item) for item in value)
    if kind == "bytes":
        return base64.b64decode(value)
    if kind == "json":
        return value
    if kind == "http":
        from starlette.responses import JSONResponse

        return JSONResponse(value, headers=payload["headers"])
    if kind == "sse":
        from starlette.responses import StreamingResponse

        async def stream():
            for event in value:
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers=payload["headers"]
        )
    raise ValueError("Unknown cache response format")


def completed_sse(raw, headers):
    """Join split content/tool arguments before applying reference substitutions.

    Live chunks are forwarded unchanged. A hit emits equivalent, coalesced SSE
    deltas; token timing and chunk boundaries are deliberately not preserved.
    """
    lines = raw.decode().replace("\r\n", "\n").splitlines()
    data = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data or data[-1] != "[DONE]":
        raise ValueError("Incomplete stream")
    events = [json.loads(line) for line in data[:-1] if line]
    if not events or any("error" in event for event in events):
        raise ValueError("Empty or failed stream")
    choices, metadata, usage = {}, {}, None

    def merge(target, incoming):
        for key, item in incoming.items():
            if isinstance(item, dict):
                merge(target.setdefault(key, {}), item)
            elif key == "tool_calls":
                calls = target.setdefault(key, {})
                for call in item:
                    merge(calls.setdefault(call["index"], {}), call)
            elif isinstance(item, str) and key not in {
                "role",
                "type",
                "thought_signature",
                "thoughtSignature",
                "signature",
            }:
                target[key] = (target.get(key) or "") + item
            elif item is not None:
                target[key] = item

    for event in events:
        metadata.update(
            {k: v for k, v in event.items() if k not in {"choices", "usage"}}
        )
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices", []):
            slot = choices.setdefault(
                choice["index"], {"delta": {}, "finish_reason": None}
            )
            merge(slot["delta"], choice.get("delta", {}))
            if choice.get("finish_reason"):
                slot["finish_reason"] = choice["finish_reason"]
    if not choices or any(
        not choice["finish_reason"] for choice in choices.values()
    ):
        raise ValueError("Stream has no completed choice")
    deltas, finishes = [], []
    for index, choice in sorted(choices.items()):
        delta = choice["delta"]
        if "tool_calls" in delta:
            delta["tool_calls"] = [
                v for _, v in sorted(delta["tool_calls"].items())
            ]
        deltas.append({"index": index, "delta": delta, "finish_reason": None})
        finishes.append(
            {
                "index": index,
                "delta": {},
                "finish_reason": choice["finish_reason"],
            }
        )
    result = [
        {**metadata, "choices": deltas},
        {**metadata, "choices": finishes},
    ]
    if usage:
        result.append({**metadata, "choices": [], "usage": usage})
    return {
        "kind": "sse",
        "headers": {k: v for k, v in headers.items() if k not in DROP_HEADERS},
        "value": result,
    }
