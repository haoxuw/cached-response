"""Changed targets and histories miss even with an approving verifier."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cached_llm_response


def request(target, extra=False):
    messages = [
        {"role": "system", "content": f"Work in /workspace/{target}."},
        {"role": "user", "content": f"Inspect {target}."},
    ]
    if extra:
        messages.append({"role": "assistant", "content": "A progress note."})
    messages.append({"role": "tool", "content": json.dumps({"target": target})})
    return {"messages": messages}


with TemporaryDirectory() as directory:
    for enabled in (False, True):
        calls, reviews = [], []

        def verifier(pair):
            reviews.append(pair)
            # Fixture approval only; real use needs the built-in or custom judge.
            return {"safe_to_reuse": True, "reason": "Synthetic test pair."}

        @cached_llm_response(
            mode="testing",
            min_words=0,
            path=Path(directory) / f"{enabled}.db",
            structural_matching=enabled,
            verifier_overrider=verifier,
        )
        def ask(body):
            calls.append(body)
            target = json.loads(body["messages"][-1]["content"])["target"]
            return {"action": "inspect", "target": target}

        ask(request("job_ab12cd34"))
        result = ask(request("job_ef56ab78", extra=True))
        assert result["target"] == "job_ef56ab78"
        assert len(calls) == 2
        assert len(reviews) == 0
        print(
            json.dumps(
                {
                    "structural_matching": enabled,
                    "upstream_calls": len(calls),
                    "verifier_calls": len(reviews),
                    "result": result,
                }
            )
        )
