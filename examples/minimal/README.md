# Minimal examples

From the package repository root:

```sh
python -m pip install .
python examples/minimal/exact.py
python examples/minimal/llm.py
```

Each example adds just an import and a decorator to a function. On the first run,
the function executes once and the second call uses its cached result. Both calls
print their result; package logging and reports stay silent by default. Later runs may use both cached
results because the local database persists.

- `exact.py` uses `cached_staticmethod` for an ordinary pure function. Despite the
  name, a class is not required. Inside a class, put `@staticmethod` above it.
- `llm.py` uses `cached_llm_response` with a local stand-in for a model call, so it
  needs no API key and makes no network requests. Replace the function body with
  your own model call returning text or JSON-compatible data. Its request meets
  the default 100-word minimum.

`cached_staticmethod` is enabled without a mode. The LLM example explicitly
selects testing mode; LLM caching is disabled by default. See the [matching and verifier guide](../../README.md#matching-in-three-rules)
for connecting your own judge model.

Run `python examples/minimal/miss_diagnostics.py` for a fresh, isolated run that
explicitly prints statistics and a masked near-miss example. It uses a local
stand-in and a rejecting verifier, with no API calls. Inspect `cache_stats()` and
`cache_misses()` inside your application process for its own results. File logging
is optional through `configure_logging(path="cache_diagnostics.log")`.

Run `python examples/minimal/metadata_rules.py` to try caller-reviewed rules.
Two changed trace labels reuse the answer; a changed resource status misses.
It prints four results and reports two upstream calls, with no network access.

Run `python examples/minimal/input_pair.py` to learn a SAFE regex from a local
fixture judge. Three requests make one upstream call and one review; the third
request hits `learned_metadata`. No credentials or model calls are needed.



`signature_counts.py` exercises persistent masked and NLTK signature counters.
Install `.[signatures]`, run `python -m nltk.downloader words` once, then
`python examples/minimal/signature_counts.py`. Both rows should report two
requests, one hit, and one miss. No model credentials are needed.

`structural_pairs.py` compares structural matching off/on using a fixture
verifier. It adds a history message and changes a repeated resource ID. The
two cases both make two upstream calls and skip verification: changed targets
and histories cannot be declared irrelevant. Run `python examples/minimal/structural_pairs.py`; no credentials
or NLTK data are needed.
