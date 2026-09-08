"""Requires the signatures extra and `python -m nltk.downloader words`."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cache_signatures, cached_llm_response

with TemporaryDirectory(prefix="cached-response-signatures-") as directory:
    path = Path(directory) / "cache.db"

    @cached_llm_response(
        mode="testing", signature_matching=True, min_words=0, path=path
    )
    def ask(body):
        return "Inspect the resource."

    body = {"messages": [{"role": "user", "content": "Inspect 01-acxan."}]}
    ask(body)
    ask(body)
    rows = cache_signatures(path=path)
    assert len(rows) == 2
    assert all(
        (row["requests"], row["hits"], row["misses"]) == (2, 1, 1)
        for row in rows
    )
    print(json.dumps(rows, indent=2))
