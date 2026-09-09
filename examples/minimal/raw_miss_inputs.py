from pathlib import Path
from tempfile import TemporaryDirectory

from cached_response import cache_misses, cached_llm_response, configure

with TemporaryDirectory() as directory:

    @cached_llm_response(
        mode="testing", min_words=0, path=Path(directory) / "cache.db"
    )
    def ask(body):
        return "fixture answer"

    ask({"messages": [{"role": "user", "content": "synthetic request A"}]})
    print("raw enabled by default:", "raw_inputs" in cache_misses(1)[0])
    configure(diagnostic_raw_inputs=True)
    ask({"messages": [{"role": "user", "content": "synthetic request B"}]})
    print(
        cache_misses(1)[0]["raw_inputs"]["caller_input"]["messages"][0][
            "content"
        ]
    )
    configure(diagnostic_raw_inputs=False)
