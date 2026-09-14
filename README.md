# cached-response

**Your test suite asks the model the same questions every run. This replays the
answers it already has — and refuses to when anything that matters changed.**

No cache server, no required dependencies, one decorator. Python 3.11+.

```sh
python -m pip install cached-response
```

```python
from cached_response import cached_llm_response

@cached_llm_response(mode="testing")
def ask_llm(request):
    return your_model_call(request)
```

Caching is **off unless you turn it on** (`mode` or `CACHED_RESPONSE_MODE`), so
importing this package cannot change production behavior. Sync and async both
work, and the answer is stored in a local SQLite file.

Start here: [integration guide and rule recipes](src/cached_response/skills/configure-cached-response/integration.md).

## The one idea: finding is clever, approving is not

Two steps, deliberately unequal.

**Finding** a candidate is allowed to be fuzzy. Ids, timestamps and generated
tokens are stripped out, and the remaining skeleton is matched against stored
requests. A wrong guess here costs one model call.

**Approving** it is not fuzzy at all. Every byte outside the fields *you*
declared irrelevant must match exactly. A wrong approval corrupts an answer, so
there is no confidence threshold — only an exact guarantee.

That asymmetry is the whole design. Similarity never authorizes reuse.

## What it matches, with real examples

These pairs are two runs of one agent test. The cache treats them as the same
question:

```
run 1   "report their current status: t_41dfe03f"
run 2   "report their current status: t_217deef9"
```

Ids that appear in questions *and* answers are handled by **aliases**, not by
ignore-rules: each run's ids map to stable handles, and the replayed answer is
rendered back with the caller's own ids — so a reused answer talks about *your*
task, never last run's. See [test-ID aliases](docs/test-id-aliases.md).

```
run 1   "created_at": 1788907973    "pid": 248801    "current_run_id": 32
run 2   "created_at": 1788908055    "pid": 249064    "current_run_id": 33
```

Bookkeeping fields are handled by **declared metadata** — you list the exact
paths that cannot affect the answer. Nothing is declared by default, and a
recognizable format is never itself a reason: a UUID may be a trace id or a
resource target, and only you know which.

And a pair from the same capture that the cache **correctly refuses**:

```
run 1   events: [created, claimed, spawned, heartbeat]
run 2   events: [created, claimed, spawned, heartbeat, heartbeat]
```

One more event arrived. The world changed, so that call runs live. Quantities
are the same story: `"Listed 4 clusters"` is an answer, not noise, and no
number-shaped rule will ever be allowed to blur it.

## Thinking tokens (Gemini and friends)

Providers that return a thought signature attach a fresh one to every
generation and require it echoed back:

```
run 1   call_346990__thought__EosRCogRARFNMg+G9bvw4j1yvTBrza0H…
run 2   call_470754__thought__ErARCq0RARFNMg/wlv0d7ieH8gwq3Wfc…
```

Two runs of one conversation are therefore never byte-equal past their first
tool call, and a single live turn used to make every later turn in that
conversation miss. `signed_call_handles=True` keys those tokens as stable
positional handles **for lookup only** — the request sent to your provider and
the response handed back always carry the real bytes. Signature bytes are never
rewritten, never invented, and never matched across.

Measured on a production agent's CI suite: **445 of 2199 model calls served
from cache (20%)**, all exact matches, with fresh task ids and fresh signatures
on every repetition.

## Self-learning, and its limits

With `learning=True`, near-miss pairs go to a model judge that answers
**SAFE**, **UNSAFE**, or **UNCERTAIN** — and uncertain means miss. A SAFE
verdict mints a narrow rule; UNSAFE is remembered too, so the same junk pair is
not re-judged forever.

The judge never writes the pattern. Python derives it from the observed
difference, bounded to anchored character classes, scoped to declared metadata,
and pinned by a hash of everything outside the approved span. A model asked to
write a regex will happily generalize `Listed 4 clusters` into `Listed \d+`;
this design makes that impossible rather than unlikely. Start with
`learning=False` and explicit rules; add the judge once you have measured what
it costs. See [learning from misses](docs/learning-from-misses.md).

## Diagnose a miss

Every miss records why. Counters are process-local and content-free; excerpts
are redacted unless you explicitly ask for raw text.

```python
from cached_response import cache_misses, cache_stats

print(cache_stats())    # hits, misses, reasons, candidate rejections
print(cache_misses(3))  # newest misses, with the closest candidate and the diff
```

The package also ships an agent skill for this workflow — inspect real misses,
propose narrow rules, measure the result:

```sh
cached-response --skill
cached-response --install-skill .agents/skills
```

## Modes

| Mode | Behavior |
| --- | --- |
| `disabled` (default) | Always calls your function. Keep this in production. |
| `conservative` | Exact input matches only. |
| `testing` | Exact matches, plus test-ID aliases and declared-metadata review. |
| `risky` | Same safeguards as `testing`; retained for compatibility. |

A cached answer measures the cache, not the model. Never enable this on a run
whose results you intend to trust as a measurement of the model itself — the
honest uses are dev loops, retries, and re-runs while you iterate on the code
around the model.

## cached_staticmethod

Exact-argument caching for pure functions. Always enabled, no mode, no word
minimum:

```python
from cached_response import cached_staticmethod

@cached_staticmethod
def square(number):
    return number * number
```

Caching skips execution, so never wrap something whose side effect or freshness
check must happen every call.

## Settings

Set on either decorator, or globally with `configure()` (decorator options win).

| Option | Meaning |
| --- | --- |
| `path` | Where the SQLite database lives. It holds your inputs and results. |
| `namespace`, `version` | Separate caches; bump `version` when hidden behavior changes. |
| `min_words=100` | Minimum input length for the LLM decorator. |
| `refresh_start="1d"`, `refresh_force="7d"` | Age window; refresh chance grows evenly between them (50% at day 4). |
| `metadata_paths`, `metadata_rules` | Declare irrelevant fields — reviewed by the judge, or by you. |
| `test_aliases`, `alias_version` | Stable handles for fresh per-run ids ([guide](docs/test-id-aliases.md)). |
| `signed_call_handles=False` | Key provider thought-signature tokens as positional handles. |
| `learning=False` | Turn off model review; explicit rules still apply. |
| `verifier_model`, `verifier_timeout`, `verifier_overrider` | Who reviews, how long, or replace it entirely. |
| `signature_matching`, `structural_matching` | Broaden candidate *search* only; neither authorizes a hit. |
| `report`, `diagnostics`, `diagnostic_text`, `diagnostic_raw_inputs` | Reporting and how much miss detail is kept. |

Call any wrapped function with `use_cache=False` to bypass for one call. Storage
defaults to a temporary directory the OS may clear — set `path` to keep it. The
database stores original inputs and responses in plain text; redaction applies
to diagnostics, not to the cache itself.

[Runnable examples](examples/minimal/README.md) ·
[All settings and contracts](docs/reference.md) ·
[Numeric test metadata](docs/test-metadata.md)
