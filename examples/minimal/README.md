# Minimal examples

From the package repository root:

```sh
python -m pip install .
python examples/minimal/exact.py
python examples/minimal/llm.py
```

Each example adds just an import and a decorator to a function. On the first run,
the function executes once and the second call uses its cached result. Both calls
print their result; cache statistics go to stderr. Later runs may use both cached
results because the local database persists.

- `exact.py` uses `cached_staticmethod` for an ordinary pure function. Despite the
  name, a class is not required. Inside a class, put `@staticmethod` above it.
- `llm.py` uses `cached_llm_response` with a local stand-in for a model call, so it
  needs no API key and makes no network requests. Replace the function body with
  your own model call returning text or JSON-compatible data. Its request meets
  the default 100-word minimum.

`cached_staticmethod` is enabled without a mode. The LLM example explicitly
selects testing mode; LLM caching is disabled by default. See the [learning hook tutorial](../../README.md#learning-and-optional-override)
for connecting your own judge model.
