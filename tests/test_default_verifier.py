"""Default verification uses the existing inference function and package prompt."""

import asyncio
import json

import pytest

from cached_response import cached_llm_response
from cached_response.matching import INSTRUCTION


from test_matching import request


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("response_kind", ["dict", "text", "chat"])
def test_default_prompt_reviews_each_pair(tmp_path, asynchronous, response_kind):
    calls, judges = [], []

    def model(body):
        if body.get("response_format") == {"type": "json_object"}:
            judges.append(body)
            verdict = {"safe_to_reuse": True, "segments": [0], "reason": "Test verdict"}
            if response_kind == "dict":
                return verdict
            if response_kind == "text":
                return json.dumps(verdict)
            return {"choices": [{"message": {"content": json.dumps(verdict)}}]}
        calls.append(body)
        return "Inspect the current state."

    async def async_model(body):
        return model(body)

    cached = cached_llm_response(
        metadata_paths=("messages.*.content",), mode="testing", path=tmp_path / "cache.db", report=False
    )(async_model if asynchronous else model)
    if asynchronous:

        async def run():
            for day in ("07", "08", "09"):
                assert (
                    await cached(request(day)) == "Inspect the current state."
                )

        asyncio.run(run())
    else:
        for day in ("07", "08", "09"):
            assert cached(request(day)) == "Inspect the current state."
    assert len(calls) == 1 and len(judges) == 2
    assert judges[0]["messages"][0]["content"] == INSTRUCTION
    evidence = json.loads(judges[0]["messages"][1]["content"])
    assert evidence["new_input"] == request("08")
    assert evidence["old_input"] == request("07")


@pytest.mark.parametrize(
    "verdict",
    [
        {"safe_to_reuse": False, "reason": "Time matters"},
        {"safe_to_reuse": "true", "reason": "Wrong type"},
        "not JSON",
    ],
)
def test_default_rejection_or_invalid_verdict_runs_model(tmp_path, verdict):
    calls, judges = [], []

    @cached_llm_response(
        metadata_paths=("messages.*.content",), mode="testing", path=tmp_path / "cache.db", report=False
    )
    def model(body):
        if body.get("response_format") == {"type": "json_object"}:
            judges.append(body)
            return verdict
        calls.append(body)
        return "Inspect the current state."

    model(request("07"))
    model(request("08"))
    assert len(calls) == 2
    assert len(judges) == 1


def test_default_request_preserves_routing_and_disables_tools():
    from cached_response.adapters import verification_body

    body = {
        **request("07"),
        "tenant": "first",
        "max_completion_tokens": 50,
        "tools": [{"function": {"name": "mutate"}}],
        "tool_choice": "required",
    }
    result = verification_body(body, {"instruction": INSTRUCTION})
    assert result["tenant"] == "first"
    assert result["model"] == "test"
    assert "tools" not in result and "tool_choice" not in result
    assert result["max_completion_tokens"] == 2048
    assert "max_tokens" not in result
    assert body["tools"]  # Building a judge request does not mutate the caller.
