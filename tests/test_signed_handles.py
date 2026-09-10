"""Signed provider tokens are keyed as stable handles, never rewritten."""

import json

import pytest

from cached_response import cached_llm_response, configure
from cached_response.normalize import signed_handles

CONTEXT = "This is repeated context for an isolated signed-handle test. " * 15
SIGNED_A = "call_346990__thought__EosRCogRARFNMgG9bvw4j1yvTBrza0H"
SIGNED_B = "call_470754__thought__ErARCq0RARFNMgwlv0d7ieH8gwq3Wfc"


def body(signed):
    return {
        "model": "fixed-model",
        "messages": [
            {"role": "system", "content": CONTEXT},
            {"role": "user", "content": "Report the task status."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": signed,
                        "function": {"arguments": "{}", "name": "kanban_show"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": signed, "content": '{"status": "done"}'},
        ],
    }


@pytest.fixture(autouse=True)
def cache_environment(monkeypatch, tmp_path):
    configure(path=tmp_path / "private" / "cache.sqlite3", mode="disabled")
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "testing")


def test_signed_handles_are_positional_and_preserve_everything_else():
    canonical = signed_handles(body(SIGNED_A))
    assert json.dumps(canonical) == json.dumps(signed_handles(body(SIGNED_B)))
    assert SIGNED_A not in json.dumps(canonical)
    assert CONTEXT in canonical["messages"][0]["content"]


def test_repetitions_differing_only_in_signatures_hit_and_run_live_with_real_bytes():
    seen = []

    @cached_llm_response(signed_call_handles=True)
    def ask(request):
        seen.append(request)
        return {"answer": f"reply {len(seen)}"}

    first = ask(body(SIGNED_A))
    second = ask(body(SIGNED_B))
    assert first == second == {"answer": "reply 1"}
    # The live call received the caller's real signed bytes, not handles.
    assert seen[0]["messages"][2]["tool_calls"][0]["id"] == SIGNED_A
    assert len(seen) == 1


def test_signed_handles_off_by_default_keeps_signatures_distinct():
    calls = []

    @cached_llm_response
    def ask(request):
        calls.append(request)
        return {"answer": f"reply {len(calls)}"}

    assert ask(body(SIGNED_A)) == {"answer": "reply 1"}
    assert ask(body(SIGNED_B)) == {"answer": "reply 2"}
    assert len(calls) == 2
