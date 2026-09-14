"""Two isolated test tasks reuse two model turns; no credentials needed."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cached_llm_response


def isolated_task(request):
    task_id = json.loads(request["messages"][0]["content"])["test_task_id"]
    return task_id, {task_id: "test_00000001"}


def main():
    calls = 0
    with TemporaryDirectory() as directory:

        @cached_llm_response(
            mode="testing",
            path=Path(directory) / "cache.sqlite3",
            min_words=0,
            test_aliases=isolated_task,
        )
        def ask(request):
            nonlocal calls
            calls += 1
            task_id = json.loads(request["messages"][0]["content"])[
                "test_task_id"
            ]
            action = "Inspect" if len(request["messages"]) == 1 else "Ready"
            return f"{action}: {task_id}"

        for task_id in ("test_ab12cd34", "test_ef56ab78"):
            request = {
                "model": "local-fixture",
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps({"test_task_id": task_id}),
                    }
                ],
            }
            first = ask(request)
            assert first == f"Inspect: {task_id}"
            print(first)
            request["messages"].extend(
                [
                    {"role": "assistant", "content": first},
                    {"role": "user", "content": "The fresh task is ready."},
                ]
            )
            second = ask(request)
            assert second == f"Ready: {task_id}"
            print(second)
        assert calls == 2
        print(f"4 requests, {calls} model calls, 2 cache hits")


if __name__ == "__main__":
    main()
