"""Explicit metadata review and persistent concrete approvals."""

import copy
import json
import sqlite3

import pytest
from test_cache import CONTEXT

from cached_response import cached_llm_response
from cached_response.normalize import normalize


def request(day, user="Inspect debug-binding without changing it."):
    return {
        "model": "test",
        "messages": [
            {"role": "system", "content": CONTEXT},
            {"role": "user", "content": user},
            {
                "role": "tool",
                "content": f"Conversation started: Monday, September {day}, 2026 (UTC)\n",
            },
        ],
    }


def test_approvals_persist_across_decorator_instances(tmp_path):
    calls, judges = [], []

    def upstream(body):
        calls.append(body)
        return "inspect"

    options = dict(
        metadata_paths=("messages.*.content",),
        mode="testing",
        path=tmp_path / "cache.db",
        verifier_overrider=lambda e: (
            judges.append(e)
            or {"safe_to_reuse": True, "segments": [0], "reason": "Test fixture"}
        ),
    )
    cached = cached_llm_response(**options)(upstream)
    cached(request("07"))
    cached(request("08"))
    recreated = cached_llm_response(**options)(upstream)
    recreated(request("09"))
    recreated(request("09"))
    assert len(calls) == 1 and len(judges) == 2
    assert all(e["old_input"] == request("07") for e in judges)
    recreated(request("10", user="Delete debug-binding."))
    assert len(calls) == 2 and len(judges) == 2
    with sqlite3.connect(tmp_path / "cache.db") as db:
        db.execute("UPDATE entries SET created=0")
    recreated(request("11"))
    assert len(calls) == 3 and len(judges) == 2


@pytest.mark.parametrize("role", ["system", "developer", "user"])
def test_changing_instruction_dates_cannot_be_approved(tmp_path, role):
    calls, judges = [], []

    @cached_llm_response(
        metadata_paths=("messages.*.content",),
        mode="testing",
        min_words=0,
        path=tmp_path / "cache.db",
        verifier_overrider=lambda e: (
            judges.append(e)
            or {"safe_to_reuse": True, "segments": [0], "reason": "must not override"}
        ),
    )
    def ask(body):
        calls.append(body)
        return "answer"

    a, b = request("07"), request("07")
    a["messages"][1] = {"role": role, "content": "Report for September 07."}
    b["messages"][1] = {"role": role, "content": "Report for September 08."}
    ask(a)
    ask(b)
    assert len(calls) == 2 and not judges


def test_changed_targets_and_timestamps_are_not_rebound(tmp_path):
    calls, judges = [], []

    @cached_llm_response(
        metadata_paths=("messages.*.content",),
        mode="testing",
        path=tmp_path / "cache.db",
        verifier_overrider=lambda e: (
            judges.append(e)
            or {"safe_to_reuse": True, "segments": [0], "reason": "Test fixture"}
        ),
    )
    def ask(body):
        calls.append(body)
        return {"target": "job_ab12cd34"}

    a = request("07")
    a["messages"][-1]["content"] = json.dumps(
        {
            "target": "job_ab12cd34",
            "start": "2026-09-07T10:00:01Z",
            "end": "2026-09-07T10:00:01Z",
        }
    )
    b = copy.deepcopy(a)
    b["messages"][-1]["content"] = json.dumps(
        {
            "target": "job_ef56ab78",
            "start": "2026-09-07T10:00:02Z",
            "end": "2026-09-07T10:00:03Z",
        }
    )
    ask(a)
    assert ask(b) == {"target": "job_ab12cd34"}
    assert len(calls) == 2 and not judges


@pytest.mark.parametrize(
    "field", ["reasoning_content", "encrypted_content", "redacted_thinking"]
)
def test_opaque_identifiers_are_not_normalized(field):
    a = request("07")
    b = copy.deepcopy(a)
    a["messages"][-1][field] = "job_ab12cd34"
    b["messages"][-1][field] = "job_ef56ab78"
    assert normalize(a, "testing").body != normalize(b, "testing").body
