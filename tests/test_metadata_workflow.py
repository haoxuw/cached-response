"""Reviewed rules and bounded private captures work through the public API."""
import copy
import json
import logging
import os
from pathlib import Path

import pytest

from cached_response import Rule, cache_misses, capture_misses, cached_llm_response
from cached_response import cli, config, diagnostics
from cached_response.storage import get_store

OLD = "a1234567-1234-1234-1234-123456789abc"
NEW = "b1234567-1234-1234-1234-123456789abc"


def body(trace=OLD, target="blue"):
    return {"model": "local", "messages": [
        {"role": "system", "content": "Inspect current state. " * 5000},
        {"role": "user", "content": "Inspect " + target},
        {"role": "tool", "content": json.dumps({"trace": trace, "status": "ready"})},
    ]}


def setup(tmp_path, **options):
    calls = []
    def upstream(request):
        calls.append(copy.deepcopy(request))
        return "The resource is ready."
    kwargs = dict(path=tmp_path/"cache.db", mode="testing", min_words=0,
                  metadata_rules=(Rule.preset("uuid", paths=("messages.*.content.trace",)),),
                  learning=False)
    kwargs.update(options)
    return cached_llm_response(**kwargs)(upstream), calls


def test_large_reviewed_pair_needs_no_model_and_survives_interleaving(tmp_path):
    def forbidden(_):
        pytest.fail("A caller-reviewed rule must not call a verifier")
    ask, calls = setup(tmp_path, verifier_overrider=forbidden)
    expected = ask(body())
    for i in range(12):
        ask(body(target=f"other-{i}"))
    assert ask(body(NEW)) == expected
    assert len(calls) == 13  # The original candidate is older than eight entries.
    with get_store(str(tmp_path/"cache.db")).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["instruction", "fact", "target", "provider", "reference", "type", "layout", "format"])
def test_reviewed_rule_still_rejects_meaningful_changes(tmp_path, change):
    ask, calls = setup(tmp_path)
    old, new = body(), body(NEW)
    if change == "instruction": new["messages"][0]["content"] += " Delete it."
    if change == "fact": new["messages"][2]["content"] = json.dumps({"trace": NEW, "status": "failed"})
    if change == "target": new["messages"][1]["content"] = "Inspect green"
    if change == "provider":
        old["messages"][2]["provider_specific_fields"] = {"signature": OLD}
        new["messages"][2]["provider_specific_fields"] = {"signature": NEW}
    if change == "reference":
        old["messages"][1]["content"] = new["messages"][1]["content"] = "Inspect " + OLD
    if change == "type": new["messages"][2]["content"] = json.dumps({"trace": 12, "status": "ready"})
    if change == "layout": new["messages"][2]["content"] = json.dumps({"trace": NEW, "status": "ready"}, indent=2)
    if change == "format": new = body("not-a-uuid")
    ask(old); ask(new)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["conservative", "disabled"])
def test_metadata_rules_do_not_enable_other_modes(tmp_path, mode):
    ask, calls = setup(tmp_path, mode=mode)
    ask(body()); ask(body(NEW))
    assert len(calls) == 2


def test_rule_still_checks_output_and_validator(tmp_path):
    calls = []
    @cached_llm_response(mode="testing", min_words=0, path=tmp_path/"output.db",
                         metadata_rules=(Rule.preset("uuid", paths=("metadata.trace",)),))
    def ask(request):
        calls.append(1)
        return request["metadata"]["trace"]
    first = {"messages": [], "metadata": {"trace": OLD}}
    second = {"messages": [], "metadata": {"trace": NEW}}
    assert ask(first) == OLD
    assert ask(second) == NEW
    assert len(calls) == 2
    ask, calls = setup(tmp_path, validator=lambda a,b: False)
    ask(body()); ask(body(NEW))
    assert len(calls) == 2


def test_rule_age_and_policy_are_not_renewed(tmp_path):
    ask, calls = setup(tmp_path)
    ask(body()); ask(body(NEW))
    store = get_store(str(tmp_path/"cache.db"))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        db.execute("UPDATE entries SET created=0")
    ask(body(NEW))
    assert len(calls) == 2


@pytest.mark.parametrize("name,old,new", [
    ("uuid", OLD, NEW), ("iso_time", "2031-03-02T12:10:00Z", "2042-08-09T02:05:00+04:00"),
    ("hex_id", "abcdef12", "9876aabc"), ("digits", "12345678", "87654321"),
])
def test_presets_on_declared_transport_metadata(tmp_path, name, old, new):
    rule = Rule.preset(name, paths=("metadata.trace",))
    ask, calls = setup(tmp_path, metadata_rules=(rule,))
    first, second = body("constant-trace"), body("constant-trace")
    first["metadata"], second["metadata"] = {"trace": old}, {"trace": new}
    ask(first); ask(second)
    assert len(calls) == 1


@pytest.mark.parametrize("include_text", [False, True])
def test_capture_is_bounded_private_and_restores_settings(tmp_path, include_text, monkeypatch):
    monkeypatch.setattr(config, "_config", config.Config())
    ask, _ = setup(tmp_path, metadata_rules=(), diagnostic_text=True)
    first, second = body(), body(NEW)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    ask(first)
    path = tmp_path/"private.jsonl"
    with capture_misses(path, include_text=include_text, limit=1) as status:
        ask(second)
        ask(body("c1234567-1234-1234-1234-123456789abc"))
    assert config.settings().diagnostic_capture is None
    assert root.handlers == handlers and root.level == level
    assert status["written"] == 1
    text = path.read_text()
    record = json.loads(text)
    assert len(record["candidates"]) == 1
    if include_text:
        assert record["input"] == second
        assert record["candidates"][0]["input"] == first
        assert "response" in record["candidates"][0]
    else:
        assert OLD not in text and NEW not in text
        assert "input" not in record
    if os.name == "posix": assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        with capture_misses(path): pass


def test_capture_budget_deadline_and_exception_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_config", config.Config())
    ask, _ = setup(tmp_path, metadata_rules=(), diagnostics=False)
    ask(body())
    with capture_misses(tmp_path/"small.jsonl", include_text=True, max_bytes=20) as status:
        ask(body(NEW))
    assert status == {"written": 0, "skipped": 1, "bytes": 0}
    now = [100.0]
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: now[0])
    with pytest.raises(RuntimeError):
        with capture_misses(tmp_path/"expired.jsonl", seconds=1) as status:
            now[0] += 2
            ask(body("another-trace"))
            raise RuntimeError("test exception")
    assert status["written"] == 0
    assert config.settings().diagnostic_capture is None


def test_skill_installs_without_database_and_never_overwrites(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cached-response", "--install-skill", str(tmp_path)])
    cli.main()
    target = Path(capsys.readouterr().out.strip())
    text = target.read_text()
    assert text.startswith("---\nname: configure-cached-response\n")
    target.write_text("User customization")
    with pytest.raises(FileExistsError): cli.main()
    assert target.read_text() == "User customization"
    monkeypatch.setattr("sys.argv", ["cached-response", "--skill"])
    cli.main()
    assert capsys.readouterr().out.strip() == text.strip()


def test_near_miss_points_to_skill_without_activating_capture(tmp_path):
    ask, _ = setup(tmp_path, metadata_rules=())
    ask(body()); ask(body(NEW))
    event = cache_misses(1)[0]
    assert event["near_miss"]
    assert event["next_step"]["read"] == "cached-response --skill"
    assert config.settings().diagnostic_capture is None


def test_normalization_failure_does_not_leave_a_lease(tmp_path):
    ask, calls = setup(tmp_path)
    request = body("⟪SLC:reserved-placeholder")
    ask(request)
    with get_store(str(tmp_path/"cache.db")).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    assert len(calls) == 1


def test_async_http_rule_preserves_response_and_caller_isolation(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    app, calls = FastAPI(), []
    @app.post("/chat")
    @cached_llm_response(mode="testing", min_words=0, path=tmp_path/"http.db",
                         learning=False, metadata_rules=(Rule.preset("uuid", paths=("metadata.trace",)),))
    async def chat(request: Request):
        calls.append(await request.json())
        async def chunks():
            yield 'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"ready"},"finish_reason":null}]}\n\n'
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(chunks(), media_type="text/event-stream")
    first = {"messages": [{"role": "user", "content": "Read current status."}], "metadata": {"trace": OLD}}
    second = {**first, "metadata": {"trace": NEW}}
    with TestClient(app) as client:
        original = client.post("/chat", json=first, headers={"authorization": "Bearer caller-one"})
        cached = client.post("/chat", json=second, headers={"authorization": "Bearer caller-one"})
        def events(response):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.text.endswith("data: [DONE]\n\n")
            return [json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"]
        assert events(cached) == events(original)
        assert len(calls) == 1
        client.post("/chat", json=second, headers={"authorization": "Bearer caller-two"})
        assert len(calls) == 2
