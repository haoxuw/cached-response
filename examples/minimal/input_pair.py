"""Learn a SAFE metadata rule with a deterministic local test verifier."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cache_stats, cached_llm_response


def request(target, note):
    return {
        "model": "local-demo",
        "messages": [
            {"role": "system", "content": "Inspect the requested resource."},
            {"role": "user", "content": f"Inspect {target}."},
            {
                "role": "tool",
                "content": json.dumps({"target": target, "note": note}),
            },
        ],
    }


reviews = []


def test_verifier(evidence):
    # A fixture for this example, not a production equivalence validator.
    reviews.append(evidence["verification_kind"])
    return {
        "decision": "SAFE",
        "segments": [0],
        "reason": "Approve this synthetic test pair.",
        "patterns": [{"segment": 0, "pattern": r"\Atrace_[a-z]{1,32}\Z"}],
    }


with TemporaryDirectory(prefix="cached-response-pair-") as directory:

    @cached_llm_response(
        mode="testing",
        metadata_paths=("messages.*.content.note",),
        min_words=0,
        path=Path(directory) / "cache.db",
        verifier_overrider=test_verifier,
    )
    def ask(body):
        print("upstream executed")
        return {"action": "inspect", "target": "job_ab12cd34"}

    ask(request("job_ab12cd34", "trace_old"))
    ask(request("job_ab12cd34", "trace_new"))
    result = ask(request("job_ab12cd34", "trace_third"))
    stats = cache_stats()
    print(
        json.dumps(
            {
                "result": result,
                "reviews": reviews,
                "requests": stats["requests"],
                "hits": stats["hit"],
                "misses": stats["miss"],
                "hit_reasons": stats["hit_reasons"],
            },
            indent=2,
        )
    )
