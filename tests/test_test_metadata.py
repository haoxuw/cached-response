"""Explicit numeric test contracts affect lookup and inference together."""

import asyncio
import copy
import json

import pytest

from cached_response import TestMetadata as Metadata, cached_llm_response
from cached_response.normalize import project_test_metadata


RULE = Metadata("inspect", (("created_at",), ("events", "*", "pid")))


def request(stamp=123, pid=456):
    return {"model": "local", "messages": [
        {"role": "system", "content": "Report status; never change resources."},
        {"role": "assistant", "tool_calls": [{"id": "call_a", "type": "function",
          "function": {"name": "inspect", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": json.dumps({
            "created_at": stamp, "events": [{"pid": pid}], "replicas": 2,
            "status": "running", "expires": 789,
        })},
    ]}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_projection_hits_and_reaches_provider_on_bypass(tmp_path, asynchronous):
    calls = []
    def provider(body):
        calls.append(copy.deepcopy(body))
        return {"answer": json.loads(body["messages"][-1]["content"])}
    async def async_provider(body):
        return provider(body)
    ask = cached_llm_response(mode="testing", min_words=0, learning=False,
        path=tmp_path / "cache.db", test_metadata=(RULE,))(
            async_provider if asynchronous else provider)
    def run(body, **kwargs):
        value = ask(body, **kwargs)
        return asyncio.run(value) if asynchronous else value
    original = request()
    snapshot = copy.deepcopy(original)
    first = run(original)
    assert run(request(321, 654)) == first
    assert len(calls) == 1
    assert first["answer"]["created_at"] == 0
    assert first["answer"]["events"][0]["pid"] == 0
    assert original == snapshot
    run(request(222, 333), use_cache=False)
    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.parametrize("change", ["status", "replicas", "expires", "system", "tool", "type", "error"])
def test_meaningful_changes_miss(tmp_path, change):
    calls = []
    @cached_llm_response(mode="testing", min_words=0, learning=False,
        path=tmp_path / "cache.db", test_metadata=(RULE,))
    def ask(body):
        calls.append(body)
        return {"answer": len(calls)}
    ask(request())
    changed = request(321, 654)
    content = json.loads(changed["messages"][-1]["content"])
    if change == "system": changed["messages"][0]["content"] = "Delete resources."
    elif change == "tool": changed["messages"][1]["tool_calls"][0]["function"]["name"] = "delete"
    elif change == "type": content["created_at"] = True
    elif change == "error": content["error"] = "Permission denied"
    else: content[change] = "changed"
    changed["messages"][-1]["content"] = json.dumps(content)
    ask(changed)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["disabled", "conservative"])
def test_not_applied_outside_test_modes(tmp_path, mode):
    @cached_llm_response(mode=mode, min_words=0, learning=False,
        path=tmp_path / "cache.db", test_metadata=(RULE,))
    def ask(body):
        return body
    assert ask(request()) == request()


def test_exact_paths_types_and_provider_state():
    body = request()
    content = {"created_at": 1.5, "events": {"other": {"pid": 789}},
               "provider_specific_fields": {"created_at": 123}, "a.b": 4}
    body["messages"][-1]["content"] = json.dumps(content)
    rules = (RULE, Metadata("inspect", (("provider_specific_fields", "created_at"), ("a", "b"))))
    assert project_test_metadata(body, rules) == body
    projected = project_test_metadata(body, (Metadata("inspect", (("created_at",),), "float"),))
    assert json.loads(projected["messages"][-1]["content"])["created_at"] == 0.0


@pytest.mark.parametrize("change", ["unlinked", "conflicting_name", "duplicate_id"])
def test_ambiguous_tool_identity_is_unchanged(change):
    body = request()
    if change == "unlinked": body["messages"][-1]["tool_call_id"] = "unknown"
    elif change == "conflicting_name": body["messages"][-1]["name"] = "other"
    else: body["messages"].insert(2, copy.deepcopy(body["messages"][1]))
    assert project_test_metadata(body, (RULE,)) == body


@pytest.mark.parametrize("kwargs", [dict(tool="", paths=(("x",),)),
    dict(tool="inspect", paths=("x",)), dict(tool="inspect", paths=(("x",),), value_type="bool")])
def test_invalid_rules_fail_early(kwargs):
    with pytest.raises(ValueError): Metadata(**kwargs)


def test_rule_policy_isolated_and_default_policy_unchanged(tmp_path):
    from cached_response.config import Config
    from cached_response.matching import policy
    assert "test_metadata" not in policy(Config())
    calls = []
    def provider(body):
        calls.append(body)
        return body
    options = dict(path=tmp_path / "cache.db", mode="testing", min_words=0, learning=False)
    plain = cached_llm_response(**options)(provider)
    projected = cached_llm_response(**options, test_metadata=(RULE,))(provider)
    assert plain(request()) == request()
    assert projected(request()) != request()
    assert len(calls) == 2


def test_raw_diagnostics_keep_original_and_projected_inputs(tmp_path):
    from cached_response import cache_misses
    @cached_llm_response(mode="testing", min_words=0, learning=False,
        diagnostic_raw_inputs=True, path=tmp_path / "cache.db", test_metadata=(RULE,))
    def ask(body):
        return {"answer": "running"}
    ask(request())
    event = cache_misses(1)[0]["raw_inputs"]
    assert event["caller_input"] == request()
    assert json.loads(event["lookup_input"]["messages"][-1]["content"])["created_at"] == 0


def test_http_projection_reaches_upstream_and_hits(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    app = FastAPI()
    calls = []
    @app.post("/chat")
    @cached_llm_response(mode="testing", min_words=0, learning=False,
        path=tmp_path / "cache.db", test_metadata=(RULE,))
    async def endpoint(request: Request):
        body = await request.json()
        calls.append(body)
        return body
    with TestClient(app) as client:
        first = client.post("/chat", json=request())
        second = client.post("/chat", json=request(321, 654))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1


def test_wildcard_never_selects_dictionary_keys():
    body = request()
    body["messages"][-1]["content"] = '{"events":{"*":{"pid":123}}}'
    assert project_test_metadata(body, (RULE,)) == body


@pytest.mark.parametrize("content", [
    '{"created_at":123,"status":"failed","status":"running"}',
    '{"created_at":123,"quantity":NaN}',
])
def test_ambiguous_or_nonfinite_json_is_not_projected(content):
    body = request()
    body["messages"][-1]["content"] = content
    assert project_test_metadata(body, (RULE,)) == body


def test_already_zero_fields_have_the_same_projected_key():
    assert project_test_metadata(request(0, 0), (RULE,)) == project_test_metadata(request(), (RULE,))
