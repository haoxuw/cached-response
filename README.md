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
| `conservative` | Matches some changing UUIDs, timestamps, and generated IDs. |
| `testing` | Matches more ID formats and supports LLM-approved rules and input-pair review. |
| `risky` | Adds rare-token patterns and includes testing-mode verification. |

### Learning and optional override

Experimental signature retrieval is opt-in with `signature_matching=True`.
Install `cached-response[signatures]` and run `python -m nltk.downloader words`
first. It hashes the fully masked input and a second version preserving
NLTK-recognized English words, retrieves scoped candidates, then ranks their
actual differences before the existing reuse checks. Signatures never approve
a hit. Persistent request/hit/miss counts are available through
`cache_signatures(path="cache.db")` or `cached-response --path cache.db --signatures`.

When identifier sets or tool-result text differ, a verifier can approve a specific
input pair without learning a broader rule. List structure, instructions, provider
state, and output-reference checks still apply. Pair approvals are checked again
on every lookup; `learning=False` disables verification. See the
[matching reference](docs/reference.md) for the boundaries and cost.

For histories with added tool results or repeated ID occurrences, testing/risky
mode can opt into `structural_matching=True`. It aligns conversation steps and
requires verification of the complete pair; it never discards added history.
This option needs no NLTK dependency. Large built-in verifier requests share
identical JSON sections once, without truncating input. Run
`python examples/minimal/structural_pairs.py` for a synthetic demonstration.

Testing and risky modes include verification prompts. For a function taking one chat-request
dictionary with `messages`, the package uses your existing model function to
check whether a proposed matching rule is safe. Supported HTTP handlers work too.
Approved rules are saved; later matches need no extra judge call. No prompt or
hook is required. Unusable replies fall back to a fresh answer.

To replace the built-in check, optionally use:

```python
@cached_llm_response(mode="testing", verifier_overrider=my_verifier)
```

Your override receives the evidence and default instructions. Return a dictionary
with `safe_to_reuse` (true or false) and `reason` (text). You can change the prompt
or use another model inside it. [Override example](docs/reference.md#optional-configuration).

Matching can be wrong, and old answers can be stale—such as yesterday's weather.
Learning also costs model calls. A replay does not test the model again.

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
| `learning=False` | Turn off rule learning for the LLM decorator. |
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
