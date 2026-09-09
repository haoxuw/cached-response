# cached-response

Save function results on disk and reuse them. No cache server or required
dependencies. Python 3.11+.

```sh
python -m pip install cached-response
```

## cached_llm_response

Reuse LLM answers during repeated tests. Add an import and a decorator to your
existing function:

```python
from cached_response import cached_llm_response

@cached_llm_response(mode="testing")
def ask_llm(request):
    return your_model_call(request)
```

Return text or JSON data. Regular and async functions work. Inputs need at least
100 words; matching calls reuse the answer. Logging and console reports are off
by default.

| Mode | What it does |
| --- | --- |
| `disabled` (default) | Always calls your function. Keep this in production. |
| `conservative` | Exact input matches only. |
| `testing` | Exact matches, plus guarded review of declared metadata changes. |
| `risky` | Same safeguards as `testing`; retained for compatibility. |

### Matching in three rules

1. Reuse exact inputs only within the same caller, settings, policy, and freshness window.
2. Find similar candidates, but reject every change outside explicitly declared irrelevant metadata.
3. Review remaining changes with a fast model; save exact approvals and tightly scoped metadata patterns.

Responses use exact input keys. Regexes recognize UUIDs, timestamps, and generated
IDs for candidate search only. Optional masked and NLTK word signatures find more
candidates; neither similarity nor a random-looking ID permits a hit.

For broader matching, declare specific string fields whose values cannot change
the answer or action. Only tool content and top-level `metadata` are eligible.
Instructions, resource targets, facts, types, message order and provider state
stay exact. Changed metadata referenced elsewhere or in the response causes a miss.
The package never substitutes resource IDs in an old answer.

```python
@cached_llm_response(
    mode="testing",
    metadata_paths=("messages.*.content.diagnostic_trace",),
    verifier_model="your-fast-model",     # A model your existing connection supports
    verifier_options={"reasoning_effort": "none"},  # Provider-specific; optional
    verifier_timeout="10s",
    verifier_version="1",
)
def ask_llm(request):
    return your_model_call(request)
```

The verifier sees numbered changed fields, both full inputs, and the cached
response. It must review every change. Saved approvals cover the exact pair,
response and policy; they expire with the original response. Optional learned
regexes cover only declared metadata under identical surrounding context.
Generated regexes are restricted to bounded character classes, never arbitrary
expressions. `learning=False` disables this path. `metadata_paths=()` is the
default, so changed inputs miss unless you explicitly declare metadata.

The built-in verifier uses your original connection with a separate model when
configured. It removes inherited thinking settings and tools. Configure reasoning
options for your provider; absence of a thinking setting does not guarantee zero
reasoning. A synchronous `verifier_overrider` can replace the model call.
See the [callback contract and system prompt](docs/reference.md#verification).

Try `python examples/minimal/input_pair.py` locally without credentials.
Use `signature_matching=True` after installing `cached-response[signatures]` and
running `python -m nltk.downloader words`. `cache_signatures(path="cache.db")`
shows persistent request/hit/miss counters.

**Defaults changed:** normalized keys no longer authorize reuse. Existing cache
entries start cold. `structural_matching=True` broadens retrieval only; changed
histories still miss. This intentionally removes unsafe hits. Model review can
still be wrong if metadata is declared incorrectly; measure correctness and total
inference time, including verifier overhead.

## cached_staticmethod

Reuse a result only when the arguments match exactly. Always enabled; no mode or
word minimum. No class is needed.

```python
from cached_response import cached_staticmethod

@cached_staticmethod
def square(number):
    return number * number
```

Works with regular and async functions. Inside a class, put `@staticmethod` above
it. Use it for pure functions: caching skips execution, so it must not skip a
needed action or a check for fresh data.

## Storage, expiry, and settings

Both decorators store inputs and results in a local SQLite file. On Unix this is
usually `/tmp/cached-response-<uid>/cache.sqlite3`; on Windows it is
`cached-response/cache.sqlite3` inside your user's temporary directory. Temporary
files may be deleted by the operating system. Use `path` for persistent storage.

Set options directly on either decorator:

```python
@cached_staticmethod(path="cache.sqlite3", refresh_start="1d", refresh_force="7d")
```

| Option | Meaning |
| --- | --- |
| `path` | Where to store the database. It contains your inputs and results. |
| `refresh_start="1d"` | Start occasionally running the function again after one day. |
| `refresh_force="7d"` | Always run it again once the saved result is seven days old. |
| `report=True` | Opt in to cache count summaries on stderr (default: off). |
| `diagnostics=True` | Keep miss statistics and up to 20 recent redacted examples in memory. |
| `diagnostic_text=False` | Mask diagnostic text by default; `True` explicitly enables raw excerpts. |
| `near_miss_threshold=0.90` | Similarity threshold for flagging a miss for investigation; never permits reuse. |
| `namespace="my-app"` | Keep separate applications or users' caches apart. |
| `version="2"` | Stop reusing old results when a hidden dependency changes. |
| `min_words=100` | Minimum input length for the LLM decorator only. |
| `learning=False` | Turn off model verification for the LLM decorator. |
| `metadata_paths=()` | Exact dotted string paths or wildcards declaring irrelevant metadata. |
| `verifier_version="1"` | Change this when your verifier policy or callback changes. |
| `verifier_model=None` | Separate review model; `None` uses the original model. |
| `verifier_timeout="10s"` | Maximum time spent waiting for each review. |
| `verifier_overrider=...` | Replace the built-in LLM verification function. |

Between days 1 and 7, the chance of refresh grows evenly: at day 4 it is 50%.
Age starts from when the result was generated; a cache hit does not reset it.
Durations accept seconds or strings such as `"30m"`, `"12h"`, and `"7d"`.
Call either function with `use_cache=False` to skip caching for that call.
There is no automatic limit on total disk usage.

## Miss examples and statistics

Inspect the running process without enabling logging:

```python
from cached_response import cache_misses, cache_stats

print(cache_stats())       # Hits, misses, reasons, prompt sizes and symbol counts
print(cache_misses(3))     # Three most recent LLM misses, newest first
```

Examples identify the candidate key, similarity score, rejection checks, changed
field paths, and masked before/after excerpts. A high score indicates textual
overlap, not confidence that the answer is safe to reuse. Redaction keeps the
first four and last four characters, masks interior letters and numbers with
`*`, and preserves spaces, dashes, and other symbols. This also applies to
verifier explanations and dynamic field names. Strings of eight characters or
fewer remain visible because they have no interior between the preserved edges.

For an optional file log:

```python
from cached_response import configure_logging

configure_logging(path="cache_diagnostics.log")  # File only; no console output
# configure_logging(console=True)               # Explicit console opt-in
# configure_logging(enabled=False)              # Remove this helper's handlers
```

This configures only `cached_response` loggers. It never changes root handlers,
root levels, or third-party loggers, and package records do not propagate to root.
Stats and examples are process-local; file output is created only when enabled.
Redaction applies to diagnostics; the cache database itself stores original
inputs and responses for matching and replay.

Run `python examples/minimal/miss_diagnostics.py` for a local demonstration with
no model/API calls. See [captured demo output](docs/miss-diagnostics-output.json)
and [diagnostic details and limitations](docs/reference.md#miss-diagnostics).

[Runnable examples](examples/minimal/README.md) · [All settings](docs/reference.md)
