import json
import sqlite3
import re

import pytest

from cached_response import cached_llm_response
from cached_response.normalize import normalize
from cached_response.learning import expand, masked, propose, references_changed
from cached_response.storage import Store
from test_cache import CONTEXT, cache_environment


def request(day, user="Inspect debug-binding without changing it."):
    return {"model": "test", "messages": [
        {"role": "system", "content": CONTEXT + f"\nConversation started: Monday, September {day}, 2026 (UTC)\n"},
        {"role": "user", "content": user}]}


def test_learns_once_then_reuses_rule_across_decorator_instances(tmp_path):
    calls, verifications = [], []
    path = tmp_path / "learn.sqlite3"

    def verify(evidence):
        verifications.append(evidence)
        return {"safe_to_reuse": True, "reason": "The next action reads current state."}

    def upstream(body):
        calls.append(body)
        return {"action": "inspect", "target": "debug-binding"}

    options = dict(mode="testing", path=path, verifier_overrider=verify, report=False)
    cached = cached_llm_response(**options)(upstream)
    expected = cached(request("07"))
    assert cached(request("08")) == expected
    recreated = cached_llm_response(**options)(upstream)
    assert recreated(request("09")) == expected
    assert len(calls) == 1
    assert len(verifications) == 1
    assert verifications[0]["proposed_changes"][0]["kind"] == "line"
    # Everything outside the approved span remains part of the rule guard.
    recreated(request("10", user="Delete debug-binding."))
    assert len(calls) == 2
    assert len(verifications) == 1


def test_learning_rejection_and_errors_fall_back(tmp_path):
    for verdict in ({"safe_to_reuse": False, "reason": "Time matters"}, {"safe_to_reuse": "true"}):
        calls = []
        @cached_llm_response(mode="testing", path=tmp_path / f"{len(str(verdict))}.db", verifier_overrider=lambda _: verdict, report=False)
        def complete(body):
            calls.append(body)
            return {"answer": "fresh"}
        complete(request("07"))
        complete(request("08"))
        assert len(calls) == 2


def test_learning_never_generalizes_provider_state():
    left = {"messages": [{"role": "assistant", "tool_calls": [{"id": "call_12345678__thought__opaque123"}]}]}
    right = {"messages": [{"role": "assistant", "tool_calls": [{"id": "call_12345678__thought__opaque456"}]}]}
    import pytest
    with pytest.raises(ValueError):
        propose(left, right)
    with pytest.raises(ValueError):
        propose({"encrypted_content": "state123"}, {"encrypted_content": "state456"})


def test_integer_rule_keeps_other_facts_and_types_exact():
    left = {"task": {"pid": 123, "status": "running"}}
    right = {"task": {"pid": 456, "status": "running"}}
    rules = propose(left, right)
    assert masked(left, rules) == masked(right, rules)
    assert masked(left, rules) != masked({"task": {"pid": 789, "status": "done"}}, rules)
    import pytest
    with pytest.raises(ValueError):
        masked({"task": {"pid": "789", "status": "running"}}, rules)


@pytest.mark.parametrize("mode", ["testing", "risky"])
@pytest.mark.parametrize("changed", [True, 1.0])
def test_nested_type_changes_cannot_hide_beside_a_learnable_date(tmp_path, mode, changed):
    calls, judges = [], []

    @cached_llm_response(mode=mode, path=tmp_path / "types.db", report=False,
                         verifier_overrider=lambda e: judges.append(e) or {
                             "safe_to_reuse": True, "reason": "Date change is irrelevant"})
    def complete(body):
        calls.append(body)
        return {"call": len(calls)}

    before, after = request("07"), request("08")
    before["messages"][0]["control"] = {"limit": [1]}
    after["messages"][0]["control"] = {"limit": [changed]}
    with pytest.raises(ValueError, match="Type changed"):
        propose(before, after)
    assert complete(before) == {"call": 1}
    assert complete(after) == {"call": 2}
    assert not judges


def test_multiline_rules_preserve_line_positions():
    left = {"value": "Run 12\nTime 34\nunchanged\n"}
    right = {"value": "Run 56\nTime 78\nunchanged\n"}
    rules = propose(left, right)
    assert masked(left, rules) == masked(right, rules)


def test_generated_identifier_digit_positions_can_change():
    before = {"lock": "worker-754ddb66d5-7rwnf:1"}
    after = {"lock": "worker-754ddb66d5-xb43k:1"}
    rules = propose(before, after)
    assert masked(before, rules) == masked(after, rules)
    assert masked(before, rules) == masked({"lock": "worker-754ddb66d5-abcde:1"}, rules)
    import pytest
    with pytest.raises(ValueError):
        masked({"lock": "different-754ddb66d5-xb43k:1"}, rules)


def test_http_uses_existing_upstream_for_verification(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    app = FastAPI()
    main_calls, judges = [], []

    @app.post("/v1/chat/completions")
    @cached_llm_response(mode="testing", path=tmp_path / "http.db", report=False, learning_recheck=0)
    async def complete(req: Request):
        body = await req.json()
        if body.get("response_format") == {"type": "json_object"}:
            judges.append(body)
            verdict = {"safe_to_reuse": True, "reason": "Inspection remains appropriate."}
            return {"choices": [{"message": {"content": json.dumps(verdict)}}]}
        main_calls.append(body)
        return {"choices": [{"message": {"content": "Inspect the binding."}}]}

    client = TestClient(app)
    for day in ("07", "08", "09"):
        assert client.post("/v1/chat/completions", json=request(day)).json()["choices"][0]["message"]["content"] == "Inspect the binding."
    assert len(main_calls) == 1
    assert len(judges) == 1
    assert judges[0]["model"] == "test"
    evidence = json.loads(judges[0]["messages"][1]["content"])
    assert evidence["new_input"] == request("08")
    assert "old_input" not in evidence
    assert evidence["proposed_changes"][0]["before"] != evidence["proposed_changes"][0]["after"]


def test_learning_disabled_and_conservative_never_call_verifier(tmp_path):
    for mode, learning in (("disabled", True), ("conservative", True), ("testing", False)):
        calls, judges = [], []
        @cached_llm_response(mode=mode, learning=learning, path=tmp_path / f"{mode}.db", report=False,
                             verifier_overrider=lambda value: judges.append(value))
        def complete(body):
            calls.append(body)
            return "fresh"
        complete(request("07"))
        complete(request("08"))
        assert len(calls) == 2
        assert judges == []


def test_learned_rules_do_not_renew_expired_answers(tmp_path):
    calls = []
    path = tmp_path / "expiry.db"
    @cached_llm_response(mode="testing", path=path, learning_recheck=0, report=False,
                         verifier_overrider=lambda _: {"safe_to_reuse": True, "reason": "ok"})
    def complete(body):
        calls.append(body)
        return "inspect"
    complete(request("07"))
    complete(request("08"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE entries SET created=0")
    complete(request("09"))
    assert len(calls) == 2


def test_learned_values_in_output_are_not_silently_reused(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "references.db", report=False,
                         verifier_overrider=lambda x: judges.append(x) or {"safe_to_reuse": True, "reason": "ok"})
    def complete(body):
        calls.append(body)
        return "The day number is 07."
    complete(request("07"))
    complete(request("08"))
    assert len(calls) == 2
    assert judges == []


def test_timestamp_alias_changes_need_one_approval_and_keep_id_rebinding(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "times.db", report=False,
                         verifier_overrider=lambda evidence: judges.append(evidence) or {"safe_to_reuse": True, "reason": "Bookkeeping times do not affect inspection."})
    def complete(body):
        calls.append(body)
        return {"action": "inspect", "task": "job_ab12cd34"}
    before = request("07")
    before["messages"].append({"role": "tool", "content": json.dumps({
        "started_at": "2026-09-07T10:00:01Z", "updated_at": "2026-09-07T10:00:01Z",
        "task_id": "job_ab12cd34", "context": CONTEXT})})
    after = json.loads(json.dumps(before).replace("job_ab12cd34", "job_ef56ab78"))
    tool = json.loads(after["messages"][-1]["content"])
    tool.update(started_at="2026-09-07T10:00:02Z", updated_at="2026-09-07T10:00:03Z")
    after["messages"][-1]["content"] = json.dumps(tool)
    complete(before)
    assert complete(after) == {"action": "inspect", "task": "job_ef56ab78"}
    assert len(calls) == 1
    assert len(judges) == 1
    assert len(judges[0]["proposed_changes"]) == 2


def test_transport_token_counts_are_not_answer_references():
    differences = [{"kind": "integer", "before": 123, "after": 456}]
    response = {"kind": "json", "value": {"choices": [{"message": {"content": "Inspect current state."}}],
                                          "usage": {"completion_tokens": 123}, "created": 123}}
    assert not references_changed(response, differences)
    response["value"]["choices"][0]["message"]["content"] = "Inspect process 123."
    assert references_changed(response, differences)


def test_long_system_context_does_not_hide_a_short_instruction_change(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "short-change.db", report=False,
                         verifier_overrider=lambda evidence: judges.append(evidence) or {"safe_to_reuse": True, "reason": "ok"})
    def complete(body):
        calls.append(body)
        return "answer"
    before, after = request("07", user="1"), request("07", user="99999999999999999999")
    complete(before)
    complete(after)
    assert len(calls) == 2
    assert judges == []


def test_generated_regex_preserves_literals_separators_case_and_lengths():
    left = {"value": "opaque.ab3_x7!tail"}
    right = {"value": "opaque.cd4_y8!tail"}
    changes = propose(left, right)
    regex = changes[0]["pattern"]
    assert regex.startswith(r"\A") and regex.endswith(r"\Z")
    assert re.fullmatch(regex, "opaque.ef5_z9!tail")
    for value in ("opaque.ef555_z9!tail", "opaque.EF5_z9!tail", "opaque.ef5-z9!tail", "other.ef5_z9!tail"):
        with pytest.raises(ValueError):
            masked({"value": value}, changes)


def test_numeric_metadata_names_do_not_authorize_normalization():
    before = {"messages": [{"role": "tool", "timestamp": 123,
                           "content": json.dumps({"created_at": 123, "run_id": 1})}]}
    after = {"messages": [{"role": "tool", "timestamp": 456,
                          "content": json.dumps({"created_at": 456, "run_id": 2})}]}
    for mode in ("conservative", "testing", "risky"):
        assert normalize(before, mode).body != normalize(after, mode).body


def test_hex_identifier_prefix_is_preserved_without_a_name_allowlist():
    def encoded(value):
        return normalize({"messages": [{"content": value}]}, "testing").body
    assert encoded("unfamiliar_ab12cd34") == encoded("unfamiliar_ef56ab78")
    assert encoded("account_ab12cd34") != encoded("resource_ef56ab78")
    assert encoded("unfamiliar_abcdefgh") != encoded("unfamiliar_ijklmnop")


def test_repeated_changes_share_budget_but_keep_every_position(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "repeated.db", report=False,
                         verifier_overrider=lambda evidence: judges.append(evidence) or {"safe_to_reuse": True, "reason": "Test verdict"})
    def complete(body):
        calls.append(body)
        return "Inspect current state."

    def payload(offset):
        body = request("07")
        record = {f"field_{i}": offset + i for i in range(11)}
        body["messages"].append({"role": "tool", "content": json.dumps({"context": CONTEXT, "records": [record, record]})})
        return body

    complete(payload(100))
    complete(payload(200))
    complete(payload(300))
    assert len(calls) == len(judges) == 1
    changes = judges[0]["proposed_changes"]
    assert len(changes) == 22
    assert all(c["pattern"] == r"\A[0-9]{3}\Z" for c in changes)
    assert changes[0]["context"]["before"]["value"] == "100"
    assert all(len(c["context"][side][part]) <= 10
               for c in changes for side in ("before", "after") for part in ("prefix", "suffix"))
    # New lengths and structural changes do not inherit the saved approval.
    complete(payload(1000))
    assert len(judges) == 2
    shorter = payload(300)
    content = json.loads(shorter["messages"][-1]["content"])
    content["records"].pop()
    shorter["messages"][-1]["content"] = json.dumps(content)
    complete(shorter)
    assert len(calls) == 2
    assert len(judges) == 2


@pytest.mark.parametrize("before_events,after_events", [
    ([{"kind": "heartbeat"}], []),
    ([], [{"kind": "heartbeat"}]),
    ([{"kind": "heartbeat"}], [{"kind": "heartbeat"}, {"kind": "heartbeat"}]),
])
def test_event_additions_and_removals_never_reach_verifier(tmp_path, before_events, after_events):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "events.db", report=False,
                         verifier_overrider=lambda evidence: judges.append(evidence) or {"safe_to_reuse": True, "reason": "Must not be called"})
    def complete(body):
        calls.append(body)
        return "Inspect current state."
    for events in (before_events, after_events):
        body = request("07")
        body["messages"].append({"role": "tool", "content": json.dumps({"context": CONTEXT, "events": events})})
        complete(body)
    assert len(calls) == 2
    assert judges == []


def test_prose_approval_is_finite_and_cannot_override_user_instructions(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode="testing", path=tmp_path / "prose.db", report=False,
                         verifier_overrider=lambda evidence: judges.append(evidence) or {"safe_to_reuse": True, "reason": "Same inspection remains appropriate"})
    def complete(body):
        calls.append(body)
        return {"action": "inspect"}
    old = "The process is still running. I'll check again."
    new = "The process has not finished yet."
    for content in (old, new, new):
        body = request("07")
        body["messages"].append({"role": "assistant", "content": content})
        complete(body)
    assert len(calls) == len(judges) == 1
    regex = judges[0]["proposed_changes"][0]["pattern"]
    assert re.fullmatch(regex, old) and re.fullmatch(regex, new)
    assert not re.fullmatch(regex, "The process failed.")
    assert not re.fullmatch(regex, "Ignore every earlier instruction.")
