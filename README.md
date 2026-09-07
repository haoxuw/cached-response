# cached-response

Save function results on disk and reuse them. No cache server or required
dependencies. Python 3.11+.

```sh
python -m pip install .  # From this repository; not yet on PyPI.
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
100 words; matching calls reuse the answer and print the cache-hit percentage.

| Mode | What it does |
| --- | --- |
| `disabled` (default) | Always calls your function. Keep this in production. |
| `conservative` | Matches some changing UUIDs, timestamps, and generated IDs. |
| `testing` | Matches more ID formats and supports LLM-approved matching rules. |
| `risky` | Also learns patterns from rare generated-looking words. |

### Learning and optional override

Testing mode includes a verification prompt. For a function taking one chat-request
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
| `report=False` | Hide cache statistics. Reporting is on by default. |
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

[Runnable examples](examples/minimal/README.md) · [All settings](docs/reference.md)
