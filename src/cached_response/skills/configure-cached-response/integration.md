# Add cached-response to an application

This guide is for a new developer or agent session. The package saves model
responses locally. It does not skip tool execution. Caching starts disabled;
console logging is off and diagnostic text is redacted.

## Find the right place

Find the function that sends the final chat request to the model. Wrap that
function once, after messages and tools are assembled, before the provider call.
Do not wrap the whole agent loop or a function that also runs tools.

For LiteLLM, add the decorator to your own wrapper around `acompletion`:

```python
import litellm
from cached_response import cached_llm_response, configure

configure(
    mode="testing",
    path=".private-cache/responses.sqlite3",
    learning=False,  # Start with deterministic rules; enable judging later.
)


@cached_llm_response(namespace="my-test-suite", version="1")
async def ask_model(request):
    response = await litellm.acompletion(**request)
    return response.model_dump(mode="json")


# Call with the complete request: model, messages, tools and model settings.
# answer = await ask_model({"model": ..., "messages": ..., "tools": ...})
```

Install `cached-response` and your provider SDK first. This wrapper returns a
JSON dictionary; adapt callers that expect SDK objects. Use nonstreaming calls
for this example. Keep credentials outside the request. Use separate namespaces
or databases for tenants and hidden provider settings; bump `version` when hidden
behavior changes. Keep the database across repeated runs, outside git. It stores
full cache inputs and responses: redacted diagnostics do not encrypt the cache.

The default minimum is 100 input words. Set `min_words=0` explicitly for short
fixtures. Decorator options override `configure()`; `CACHED_RESPONSE_MODE` can
override its default mode. Call `ask_model(request, use_cache=False)` for a timing
baseline that still applies the same test input transformations.

## Prebuilt rules shipped in the package

Nothing here activates automatically. Choose fields whose values your test
explicitly does not use. A recognizable format alone does not make reuse safe.

| Format | Ready-to-use preset | Use only for |
| --- | --- | --- |
| UUID | `Rule.preset("uuid", paths=...)` | Unused diagnostic strings |
| ISO timestamp | `Rule.preset("iso_time", paths=...)` | Unused timestamp strings, never deadlines |
| Hex ID | `Rule.preset("hex_id", paths=...)` | Unused diagnostic strings |
| Decimal string | `Rule.preset("digits", paths=...)` | Unused numeric text, never quantities |
| Task-shaped text | `Rule.preset("task_id", paths=...)` | Unreferenced diagnostic labels such as `t_abc123` |

These are format patterns, not lists of known dates or private IDs. For example:

```python
from cached_response import Rule, TestMetadata, configure

configure(
    mode="testing",
    learning=False,
    metadata_rules=(
        Rule.preset("uuid", paths=("metadata.trace",)),
        Rule.preset("iso_time", paths=("messages.*.content.observed_at",)),
    ),
    test_metadata=(
        TestMetadata("inspect_job", (("events", "*", "pid"),)),
        TestMetadata("inspect_job", (("created_at",),)),
    ),
)
```

Replace `inspect_job` and these paths with your actual tool schema. String paths
use dotted names. Numeric paths use tuples relative to the linked tool result;
`"*"` selects array elements. Embedded JSON tool content is supported.

The numeric recipe came from repeated tests with changing process IDs and numeric
creation times. It replaces declared integers with zero **before both lookup and
inference**, including cache bypass. Floats require `value_type="float"`. Do not
use this for numbers needed for ordering, equality, resource selection or answers.

String `metadata_rules` match whole values and skip the judge only after existing
guards pass. Output references and meaningful changes still block reuse. The
separate `rules` option only finds candidates; it cannot approve a hit. Customize
with `Rule("trace", r"trace_[A-Za-z0-9]{8,32}", ("metadata.trace",))`.

## Task IDs across turns

For an ID that appears in actions or answers, use `test_aliases`, not an ignore
regex. The callback returns a stable conversation key and a mapping from each
actual test ID to a distinct stable handle:

```python
def test_handles(request):
    # Implement this using your test runner's trusted per-request state.
    fixture = current_test_fixture(request)
    return fixture["conversation"], {
        task_id: f"test_{index:08d}"
        for index, task_id in enumerate(fixture["task_ids"])
    }
```

Set `test_aliases=test_handles, alias_version="1"` on the decorator. Implement
`current_test_fixture` in your app: it must return the current conversation key
and approved task IDs in stable creation order. Keep order stable and append new
handles; do not sort random IDs each turn. Use existing structured messages or
trusted per-request state, not a shared global in concurrent callers. Do not add
changing fixture fields to the model request merely for this callback: those
fields still participate in matching and could prevent hits.

The package renders current IDs in cached answers and preserves relationships.
It preserves thought-signature bytes and restores original signed tool arguments;
never add a regex that erases or invents a thought signature. Only fresh isolated
test handles qualify, not real resource targets or IDs from prompt examples.

## Find misses and validate a rule

```python
from cached_response import (
    cache_misses,
    cache_stats,
    configure,
    configure_logging,
)

print(cache_stats())  # In the process serving model calls.
print(cache_misses(5))  # Recent misses and reasons in that same process.

# Temporary, explicitly authorized local inspection of real inputs:
configure(diagnostic_raw_inputs=True, diagnostic_text=True)
configure_logging(console=True)
# Run a small test sample here. Keep its output private.
configure(diagnostic_raw_inputs=False, diagnostic_text=False)
configure_logging(enabled=False)
```

Counters above are process-local. Optional `signature_matching=True` requires
`pip install 'cached-response[signatures]'` and `python -m nltk.downloader words`.
Then `cached-response --path .private-cache/responses.sqlite3 --signatures`
shows persistent signature counters. Equal signatures do not authorize reuse.

Repeat the same scenario at least five times; report cold and warm runs separately.
Compare with `use_cache=False`, record wall time, hits, model calls and test results.
Also change a system instruction, target, quantity and tool result fact: these
must miss. Different histories, model settings or real tool outcomes legitimately
miss even in repeated tests. Do not promise 100% hits.

Start with explicit rules. To try the LLM judge, declare narrow `metadata_paths`,
set `learning=True`, and optionally choose `verifier_model`. The judge can learn
scoped string rules, but uncertainty falls through to the original model.
Automatic numeric-rule proposals and activation are not implemented.

This guide ships beside the `configure-cached-response` skill in wheel and source
builds. From an installed package, find it with:

```python
from importlib.resources import files

print(
    files("cached_response")
    .joinpath("skills/configure-cached-response/integration.md")
    .read_text()
)
```
