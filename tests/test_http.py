import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, StreamingResponse

from cached_response import cached_llm_response
from test_cache import CONTEXT, UUID_A, UUID_B, body, cache_environment


def application(stream=False, truncated=False, status=200):
    app = FastAPI()
    calls = []

    @app.post("/v1/chat/completions")
    @cached_llm_response(report=False)
    async def complete(request: Request):
        request_body = await request.json()
        calls.append(request_body)
        if not stream:
            return JSONResponse({"text": request_body["messages"][-1]["content"]}, status_code=status)

        async def chunks():
            identifier = request_body["messages"][-1]["content"]
            arguments = json.dumps({"task_id": identifier})
            events = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_original", "type": "function", "function": {"name": "kanban_show", "arguments": arguments[:20]}, "extra_content": {"google": {"thought_signature": "opaque-signature"}}}]}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[20:]}}]}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
            for event in events:
                yield "data: " + json.dumps(event) + "\n\n"
            if not truncated:
                yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream", status_code=status)

    return TestClient(app), calls


def test_http_json_changed_identifier_misses():
    client, calls = application()
    assert client.post("/v1/chat/completions", json=body(UUID_A)).json()["text"] == UUID_A
    assert client.post("/v1/chat/completions", json=body(UUID_B)).json()["text"] == UUID_B
    assert len(calls) == 2


def test_sse_changed_identifier_misses_then_exact_hit():
    client, calls = application(stream=True)
    client.post("/v1/chat/completions", json=body(UUID_A))
    response = client.post("/v1/chat/completions", json=body(UUID_B))
    # Repeated identical input replays the completed, coalesced stream.
    response = client.post("/v1/chat/completions", json=body(UUID_B))
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    call = events[0]["choices"][0]["delta"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"])["task_id"] == UUID_B
    assert call["extra_content"]["google"]["thought_signature"] == "opaque-signature"
    assert len(calls) == 2


def test_incomplete_stream_not_cached():
    client, calls = application(stream=True, truncated=True)
    for _ in range(2):
        assert client.post("/v1/chat/completions", json=body(UUID_A)).status_code == 200
    assert len(calls) == 2


def test_http_error_not_cached():
    client, calls = application(status=429)
    for _ in range(2):
        assert client.post("/v1/chat/completions", json=body(UUID_A)).status_code == 429
    assert len(calls) == 2


def test_http_identity_isolation():
    client, calls = application()
    for token in ("first", "second"):
        client.post("/v1/chat/completions", json=body(UUID_A), headers={"Authorization": token})
    assert len(calls) == 2


def test_http_production_bypass(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "disabled")
    client, calls = application()
    for _ in range(2):
        client.post("/v1/chat/completions", json=body(UUID_A))
    assert len(calls) == 2
    assert not (tmp_path / "private").exists()
