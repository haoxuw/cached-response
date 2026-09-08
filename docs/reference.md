# Configuration and matching reference

## Choose how much the LLM cache ignores

```python
@cached_llm_response(mode="testing")
async def ask_llm(request):
    return await existing_model_call(request)
```

| Mode | Differences eligible for matching |
| --- | --- |
| `disabled` (default) | No caching or normalization. |
| `conservative` | UUIDs, ISO date-time strings, and long identifiers containing many symbols. |
| `testing` | Also prefixed hexadecimal identifiers and recognized provider tool-call IDs. Can ask the existing model to approve a concrete input pair. |
| `risky` | Same matching behavior as testing; retained for compatibility. |

Numeric values stay exact during normalization. Changed tool data can reach
pair review; numeric metadata names do not make those values irrelevant.
Caller-supplied `Rule` objects explicitly authorize additional ID formats.
`risky` remains accepted for compatibility and now behaves like `testing`.
Automatic rare-token discovery and learned regex masks have been removed.

Normalized equality is an approximation: an ID or timestamp can change the
meaning of a request. Rebinding preserves identifier relationships, not semantic
equivalence. Keep production caching disabled and validate replayed outcomes.
See the [input relevance checklist](input-relevance.md) for the boundaries.

## Defaults

The package starts caching at 100 whitespace-separated words across message
contents, including system context and tool results. JSON keys and tool schemas
do not count. Short requests call the model directly. Functions without a
`messages` field count words in their string arguments instead.

The cache uses the system temporary directory: normally `/tmp/cached-response-<uid>/`
on Unix, or `cached-response/` inside the user temporary directory on Windows.
Files are private, but contain prompts and responses. `/tmp` is temporary and
does not share a cache across machines or survive every container restart.
Use `configure(path=...)` to point to retained local storage. SQLite
WAL is intended for one machine, not a shared network filesystem.

Every enabled call counts hit/miss and the cumulative fraction served from
cache. Console reporting is off by default; use `report=True` to enable summaries
on stderr. These are response counts, not token or cost savings. A bypass
or failed upstream call is not a hit. Production bypasses emit no cache metrics.
`cache_stats()` returns these process-local counters; `cached-response --path
PATH` reports persistent entry count and size without displaying prompts.

## Miss diagnostics

`cache_stats()` returns aggregate process-wide counts for both decorators:
`requests`, `hit`, `miss`, `cache_hit_percent`, `cache_miss_percent`, and
`miss_reasons`. Disabled calls and `use_cache=False` are excluded. A miss counts
the lookup decision, even if the subsequent upstream call fails.

With `diagnostics=True` (default), LLM misses also contribute:

| Field | Meaning |
| --- | --- |
| `diagnosed_misses` | Misses with diagnostic collection enabled. |
| `misses_with_candidates` | Requests where at least one candidate was found. |
| `near_misses` | Missed requests with at least one candidate at or above `near_miss_threshold` (default `0.90`). |
| `candidates_missed` | Total candidates described across those requests. |
| `candidate_rejections` | Counts by each candidate's final rejection reason. |
| `miss_prompt` | Sample count and total, min, max, mean for characters, UTF-8 bytes, words, symbols, digits, and lines. |

Prompt statistics count string values in message contents (or string arguments
for other call shapes). They exclude JSON framing, role labels, tool schemas,
and protected provider signature fields. Words are whitespace-separated;
symbols are Unicode punctuation and symbol characters, including emoji.
Characters are Python Unicode code points, not model tokens. Misses whose input
cannot be decoded may lack prompt measurements, so use `miss_prompt.count` as
the measurement denominator. Metrics with no events may be absent or zero.

`cache_misses(limit=5)` returns independent copies of recent LLM miss examples,
newest first, from a bounded 20-entry process-local buffer. Each includes the
request key, miss reason, prompt measurements, and candidate details. Both APIs
are also available as `your_decorated_function.cache_stats()` and `.cache_misses()`;
they combine all decorated functions in that process. Nothing prints by default.
Set `diagnostics=False` to skip collection and extra candidate scans; basic
hit/miss counts remain available. Restarting the process resets counters and
examples; the CLI reads storage size, not these runtime metrics.

Candidate examples show their storage key, age, `signature_similarity`,
`high_similarity`, rejection `checks`, prompt sizes, and at most 12 structural
differences. Each difference includes its path, character lengths, edit offset,
and before/after excerpts capped at 160 characters. Default redaction preserves
the first four and last four characters of each excerpt, replaces interior
letters and numbers with `*`, and keeps spaces, dashes, punctuation, and other
symbols. For example, `abcd-Alice 1234-wxyz` becomes `abcd-***** ****-wxyz`.
Strings of eight characters or fewer remain visible because the preserved edges
cover the whole string. Unknown field names and verifier explanations use the
same redaction; long values are capped at 160 characters while retaining their
last four characters. Explicit `diagnostic_text=True` on the decorator or `configure()`
allows raw excerpts and verifier explanations in new examples and logs. It does
not retroactively change retained examples. Exception diagnostics contain error
types only, with no exception messages or tracebacks.

Candidate lookup considers at most eight recent indexed entries in the same
function, namespace, mode, rules, and HTTP identity. Chat requests also require
matching model settings and tools. By default reuse requires matching message roles; when
that group has no candidates, diagnostics can inspect nearby role sequences and
report `message_role_sequence_changed` with both message counts. This observation
does not authorize reuse. Opt-in `structural_matching` can instead align differing
histories for pair review. Non-chat argument shapes use
the function scope. This is a bounded investigation of nearby entries, not a
full-database search; older entries without a candidate index may not appear.
Candidates skipped by an early verifier rejection are not all evaluated.

The similarity signature uses character-trigram Dice overlap of normalized JSON,
penalized for differences in total length. For inputs over 65,536 characters,
only the first and last 32,768 characters are sampled and `signature_sampled`
is true. It is a heuristic overlap score, never a probability of correctness or
permission to reuse. Shared context can dominate the score while a short changed
instruction makes reuse unsafe. A `near_miss` is a candidate for investigation,
not proof of a lost safe cache hit. Cache hashes themselves have no similarity
interpretation, and opaque provider signatures are not treated as confidence.

The request reason `cold` means its exact normalized key was absent. Candidate
reasons explain why other entries were missed: `verification_disabled`,
`validator_rejected`, `validator_error`, `unmapped_output_reference`,
`pair_instruction_or_control_changed`, `pair_provider_state_changed`,
`pair_type_changed`, `pair_fields_changed`, `pair_sequence_changed`,
`verifier_rejected`, `invalid_verdict`, `verifier_error`,
`verifier_input_too_large`, `refresh`, or decoding/rebinding failures.
Pair guards include a redacted field path where available. Masking a free-text
verifier explanation retains the machine-readable rejection reason.
Rebinding failures include `previous_reference_count`, `current_reference_count`,
and `reference_structure_matches` to explain changed identifier relationships
without logging identifier values.

Optional logging is confined to the package:

```python
from cached_response import configure_logging

configure_logging(path="cache_diagnostics.log")  # Only a file
configure_logging(console=True)                 # Switch to console
configure_logging(enabled=False)                # Close helper-owned handlers
```

The helper replaces only its own handlers; application-installed handlers on
`cached_response` are preserved. The package uses a `NullHandler` and disables
propagation to root by default. It never calls `basicConfig`, changes the root
level, or configures third-party loggers. Applications can instead attach their
own handler directly to `logging.getLogger("cached_response")` and set its level.
Diagnostic records include a JSON object in the message and the same object in
`LogRecord.cache_diagnostic` for structured handlers. HTTP verifier metadata
contains sizes and timing, with no raw request or response.

Only explicitly enabled file logs persist diagnostics. The SQLite response cache
still stores original inputs/results needed for matching and replay; diagnostic
redaction does not encrypt or redact that database.

## Refresh behavior

Refresh starts probabilistically after one day and becomes mandatory after seven
days, measured since successful generation. Between those ages the probability
increases linearly. With a 3–7 day window, day 5 has a 50% refresh probability.
One producer per key normally refreshes while other callers wait. Leases expire
after five minutes so a crashed process cannot block a key indefinitely. This
does not provide a global limit across different keys or machines.

**Cached answers can contain outdated information.** Asking “What is the weather
today?” may return an earlier forecast. Changing a date in the cached answer does
not fetch current weather. A short question with a long system prompt can still
exceed the threshold. Use `use_cache=False` whenever fresh information or an
independent model response is required. Replaying three CI repetitions does not
provide three independent observations of model behavior.

## Optional configuration

LLM mode has one precedence order: an explicit decorator `mode=` wins, then
`CACHED_RESPONSE_MODE`, then `configure(mode=...)`, then `disabled`. There is no
separate enable flag and no other package environment variables. Unrecognized
environment values disable caching; invalid explicit Python settings raise an
error. A conditional decorator expression is evaluated when the function is
defined; an omitted mode reads the current process setting on each call.

Optional Python settings change paths, rules, or refresh behavior. Decorator
options override process defaults.

```python
@cached_llm_response(
    mode="disabled" if is_production else "testing",
    refresh_start="1d",
    refresh_force="7d",
    min_words=100,
)
async def ask_llm(request):
    return await existing_model_call(request)
```

Durations accept seconds, `datetime.timedelta`, or strings such as `"30m"`,
`"12h"`, and `"7d"`. They measure entry age, not a clock time or calendar date.

```python
from cached_response import Rule, configure

configure(
    refresh_start="3d",
    refresh_force="7d",
    report=False,
    rules=(Rule("job", r"\bjob_[0-9a-f]{12}\b",
                paths=("messages.*.content",)),),
)
```

Rules accept a name, regex, and dotted paths with `*` wildcards. They authorize
normalization at those positions, so keep them narrow. An optional synchronous
`validator(old_input, new_input)` can reject a candidate; false or an exception
causes a miss. It can call an LLM if the caller chooses, but that adds inference
cost and cannot prove equivalence.

Testing and risky modes use one candidate-review path after a normalized miss.
The three rules are listed in the README. Ordinary lookups inspect eight recent
entries; signatures can contribute up to eight entries per bucket, at most 24
total. Candidates share caller identity, namespace, mode, explicit rules, model
settings and tool schemas. Without `structural_matching`, message roles also
remain part of the lookup scope.

The default pair check preserves JSON types, fields, list lengths, instructions,
non-content controls and opaque provider state. Tool/assistant content may
change, but requires full model review. IDs map by their structured occurrences;
mappings must be one-to-one. An unmapped old ID cannot survive in the response.
Changed timestamps remain visible to the verifier, without renumbering IDs when
two events previously happened to share a timestamp.

For sync/async functions taking one chat-request dictionary, verification calls
the original undecorated function. HTTP handlers likewise use their existing
provider connection. The request disables tools and asks for JSON.
`learning=False` disables verification; conservative and disabled modes never
invoke it. At most one verifier is called per lookup, including failed reviews.

An optional synchronous `verifier_overrider(evidence)` replaces that call. It
receives `instruction`, `verification_kind="input_pair"`, `old_input`, `new_input`,
`original_cached_response`, `cached_response`, and ID-mapping counts in
`reference_alignment`. The old `proposed_changes` field stays empty for callback
compatibility. Return `{"safe_to_reuse": bool, "reason": str}`. Only boolean true
with a string reason approves reuse. False, invalid output or an exception
falls back upstream. Inputs and responses in the evidence remain untrusted data.

Approvals are never generalized or persisted. Even the same non-exact pair must
be approved again. This trades fewer code paths and simpler safeguards for extra
verifier calls compared with the removed learned-rule implementation. No hit
renews the stored response's age. Change `version=` or `namespace=` when changing
verifier policy or hidden dependencies.

Built-in async/HTTP verification uses `verifier_timeout="90s"`. Synchronous calls
and custom overrides need timeouts in their own clients. The total verifier
instruction and serialized evidence are limited to 300,000 characters.
For large inputs, identical top-level fields (other than messages) appear once
under `shared_input`; merge these into both inputs to reconstruct them. This
keeps repeated tool schemas out of the wire payload without a recursive codec.
Custom callbacks still receive the original complete dictionaries. Unique large
histories can exceed the limit and fall back upstream.

## Migration from the experimental implementation

`evidence.py`, `structural.py`, `pairwise.py`, and `learning.py` are replaced by
one `matching.py` pipeline. Automatic regex learning, rare-token discovery,
`learning_recheck`, and the CLI's `learned_rules` count are removed. Old rule
tables in existing databases are ignored; they are not deleted automatically.
The cache format/scope version changes, so existing entries start cold while
old persisted signature counters remain inspectable. Imports, decorators,
explicit `Rule` configuration, logging and counter APIs remain available.

Use `namespace=` to separate application/tenant scopes that are not represented
in arguments. Function implementation and explicit `version=` separate entries;
HTTP authentication headers contribute only a hash. Hidden dependencies still
need an explicit version/namespace change. Cache-control `use_cache` is reserved.
Cache errors bypass storage; errors from your function propagate normally.
Only complete successful responses are stored, up to 16 MiB per entry. There is
currently no automatic total-disk-size eviction.

For ordinary pure functions, use exact argument caching with no word minimum:

```python
from cached_response import cached_staticmethod

class Example:
    @staticmethod
    @cached_staticmethod
    def compute(value):
        return {"answer": value * 2}
```

This decorator is always enabled and has no `mode` option. LLM mode settings do
not affect it. Shared storage, expiry, and reporting options still apply. Call
with `use_cache=False` to bypass. Do not cache functions whose side effects must
run or whose hidden inputs change without a version change.
It can also wrap a read-only MCP call if the wrapper returns JSON-compatible data.
Raw SDK result objects pass through uncached. Cache only when an earlier result
is acceptable; tool calls that change state must still execute.


## Experimental signature retrieval

Enable `signature_matching=True` after installing `cached-response[signatures]`
and downloading `words` with `python -m nltk.downloader words`. This is opt-in;
ordinary imports and lookups neither import NLTK nor download data. Missing NLTK
or corpus data raises a configuration error when enabled. Install the same corpus
on all workers; its digest versions the lexical signatures.

Both signatures use the entire canonical JSON input, preserving string whitespace
and punctuation. The masked signature replaces every alphanumeric character with
`*`, then hashes the resulting string with SHA-256. The lexical signature retains
alphabetic tokens found in NLTK's English word list and masks other letters and
numbers. It preserves case. Neither signature establishes semantic equivalence;
identifiers, unknown terminology, numbers, and non-English words can be meaningful.

Testing/risky lookups retrieve up to eight entries per signature plus eight recent
entries, deduplicate them, and rank lexical matches, masked matches, then recent
fallbacks. Within each group, a structured distance estimate ranks actual normalized
input differences; text comparisons sample at most 8,192 characters per changed
leaf. All authorization checks still inspect full inputs. The verifier budget stays
at one call per lookup. Conservative mode can collect counters but retains its
existing reuse checks; disabled mode bypasses all of this.

Candidates stay scoped by function, namespace, mode, rules, HTTP identity, model
settings and tools; message roles also apply unless broader matching is enabled. Existing cache files upgrade automatically, but
signature indexes only cover entries written after enabling the feature. Candidate
frequency and distance never approve reuse, and this feature learns no new ID formats.

`cache_signatures(path="cache.db", limit=20)` and
`cached-response --path cache.db --signatures` show persistent counters ordered by
request frequency. Each scoped signature records requests, hits, misses, first seen,
and last seen. Every request increments both kinds, so do not sum counts across
signature kinds. Miss counts describe cache decisions, including upstream failures.
Updates are atomic and survive process restarts; logging and in-memory diagnostic
settings do not control them. Counters contain hashes and numbers, with no input
excerpts. They accumulate until the cache database is removed. Existing response
payloads in that database still retain original inputs and outputs.

## Broader matching

`structural_matching=True` changes the existing pair check and scope; there is
no second candidate index or fallback matcher. It needs no NLTK dependency.
Role/function alignment is bounded to 256 messages and includes the producing
function for tool results. Every system/developer/user message must align and
remain exact after mapping. Unknown roles or a changed terminal role reject.

Matching structured locations and string contexts can anchor one-to-one IDs even
with extra occurrences. Added/removed tool or assistant data may reach review;
all full inputs remain in the evidence. Changed opaque state, unmapped output
IDs and type changes at aligned locations still reject before model review.
No alignment metadata or similarity score substitutes for full-pair review.

Run `python examples/minimal/structural_pairs.py` to compare the option off/on.
This example uses a synthetic verifier and requires no credentials.
