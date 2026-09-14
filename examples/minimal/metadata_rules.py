"""Run locally: a reviewed trace rule saves calls while a changed fact misses."""

import json
import tempfile
from pathlib import Path

from cached_response import (
    Rule,
    cache_misses,
    cache_stats,
    cached_llm_response,
)


def main():
    calls = 0
    with tempfile.TemporaryDirectory() as directory:

        @cached_llm_response(
            mode="testing",
            path=Path(directory) / "cache.db",
            min_words=0,
            learning=False,
            metadata_rules=(
                Rule.preset("hex_id", paths=("messages.*.content.trace",)),
            ),
        )
        def ask(request):
            nonlocal calls
            calls += 1
            return json.loads(request["messages"][-1]["content"])["status"]

        def request(trace, status="ready"):
            return {
                "messages": [
                    {"role": "user", "content": "Report the resource status."},
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {"trace": trace, "status": status}
                        ),
                    },
                ]
            }

        print("cold:", ask(request("a1b2c3d4")))
        print("new trace:", ask(request("b2c3d4e5")))
        print("another trace:", ask(request("c3d4e5f6")))
        print("changed fact:", ask(request("d4e5f6a7", "failed")))
        print("upstream calls:", calls)
        print("hit reasons:", cache_stats()["hit_reasons"])
        print("last rejection:", cache_misses(1)[0]["candidates"][0]["reason"])
        assert calls == 2


if __name__ == "__main__":
    main()
