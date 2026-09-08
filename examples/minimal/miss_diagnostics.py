"""Print actual metrics and a redacted miss; no API key or network needed."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cache_misses, cache_stats, cached_llm_response


def request(day):
    return {
        "model": "local-demo",
        "messages": [
            {
                "role": "system",
                "content": "Use the supplied context to inspect current state. "
                * 15,
            },
            {"role": "user", "content": "Inspect alice@example.com's task."},
            {
                "role": "tool",
                "content": f"Conversation started: September {day}, 2026",
            },
        ],
    }


with TemporaryDirectory(prefix="cached-response-demo-") as directory:

    @cached_llm_response(
        mode="testing",
        path=Path(directory) / "cache.db",
        verifier_overrider=lambda _: {
            "safe_to_reuse": False,
            "reason": "alice@example.com's task needs current state",
        },
    )
    def ask(body):
        return "Inspect the current state."

    ask(request("07"))  # Cold miss; saves a candidate.
    ask(request("08"))  # Similar input, but verification rejects reuse.
    ask(request("08"))  # Exact hit on the newly saved result.

    stats = cache_stats()
    miss = cache_misses(1)[0]
    candidate = miss["candidates"][0]
    print(
        json.dumps(
            {
                "stats": {
                    key: stats[key]
                    for key in (
                        "requests",
                        "hit",
                        "miss",
                        "cache_miss_percent",
                        "near_misses",
                        "miss_reasons",
                        "candidate_rejections",
                        "miss_prompt",
                    )
                },
                "miss_example": {
                    "reason": miss["reason"],
                    "candidate_key": candidate["key"],
                    "signature_similarity": candidate["signature_similarity"],
                    "high_similarity": candidate["high_similarity"],
                    "candidate_reason": candidate["reason"],
                    "differences": candidate["differences"],
                },
            },
            indent=2,
        )
    )
