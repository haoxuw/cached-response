import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import TestMetadata, cached_llm_response

seen = []
with TemporaryDirectory() as directory:

    @cached_llm_response(
        mode="testing",
        min_words=0,
        learning=False,
        path=Path(directory) / "cache.db",
        test_metadata=(TestMetadata("inspect_job", (("pid",),)),),
    )
    def ask(body):
        seen.append(json.loads(body["messages"][-1]["content"]))
        return "ready"

    for pid in (123, 456):
        ask(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_demo",
                                "type": "function",
                                "function": {
                                    "name": "inspect_job",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_demo",
                        "content": json.dumps({"pid": pid}),
                    },
                ]
            }
        )
    print("upstream calls:", len(seen))
    print("upstream saw:", seen)
