# Configuration and matching reference

## Matching contract

Caching defaults to disabled. Conservative mode reuses exact inputs only.
Testing/risky modes additionally review changes to caller-declared irrelevant
metadata. An explicit `test_aliases` callback can also translate approved random
test handles before inference and restore them in responses. See the
[test-ID adapter](test-id-aliases.md) for its contract and streaming limits.
All remaining changes miss, including UUIDs and dates that merely normalize alike.
`Rule` regexes and masked/NLTK signatures only retrieve and rank candidates.
They never authorize reuse. See the [input relevance checklist](input-relevance.md).

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
`miss_reasons` and `hit_reasons` (`exact`, `verified_pair`, `approved_pair`,
`learned_metadata`). Disabled calls and `use_cache=False` are excluded. A miss counts
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
does not authorize reuse. `structural_matching` broadens retrieval across role
sequences, but changed histories still fail the guard. Non-chat argument shapes use
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

The request reason `cold` means its exact input key was absent. Candidate
reasons explain why other entries were missed: `verification_disabled`,
`validator_rejected`, `validator_error`, `metadata_reference_present`,
`undeclared_or_meaningful_change`, `pair_provider_state_changed`,
`pair_type_changed`, `pair_fields_changed`, `pair_sequence_changed`,
`verifier_rejected`, `verifier_uncertain`, `rejected_pair`, `learned_unsafe`,
`invalid_verdict`, `verifier_error`,
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

Rules accept a name, regex, and dotted paths with `*` wildcards. They influence
candidate normalization only. Keep caller-authored regexes bounded and narrow. An optional synchronous
`validator(old_input, new_input)` can reject a candidate; false or an exception
causes a miss. It can call an LLM if the caller chooses, but that adds inference
cost and cannot prove equivalence.

## Verification

Testing/risky mode compares up to eight recent candidates, plus eight per enabled
signature bucket and eight from the declared-metadata index. One model review is
allowed per lookup, including errors. Metadata indexing masks only declared
fields for retrieval; the full pair still has to pass all guards.

Declare `metadata_paths=("messages.*.content.diagnostic_trace",)` only when the
application contract says those values cannot affect the answer or action.
Paths address string leaves in tool content (including JSON text), or top-level
`metadata`. System/developer/user/assistant content and other request controls
cannot be declared away. Unknown fields, field sets, list lengths and JSON types
stay exact. Each changed value must be nonempty and at most 512 characters;
there can be at most 16 changes. Repeated values, output references, and references
in unchanged context reject reuse. Bytes/tuple response envelopes are not eligible
for broader reuse. No identifier rebinding takes place.

`metadata_rules=(Rule.preset("uuid", paths=("metadata.trace",)),)` adds a
caller-reviewed rule. Presets also include `iso_time`, `hex_id` and `digits`.
Custom `Rule(name, pattern, paths)` expressions must match both complete string
values. Rules skip model review only when they cover every changed segment after
the guards and validator pass. They work with `learning=False`, only in testing
and risky modes, and do not renew answer age. They are part of the cache policy.
Use bounded patterns without nested repetition; custom regexes are trusted caller
configuration. The existing `rules` option remains candidate-only.

`verifier_model` selects a separate model through the original function or HTTP
connection. `verifier_options` supplies provider-specific parameters such as
`reasoning_effort="none"`. Inherited thinking controls, tools and streaming options
are removed; the request forces nonstreaming JSON, temperature zero and a 2,048
token limit. If no model is configured, the original model remains the fallback.
The application must make the selected model available through its connection.

The default system prompt lives in
[`matching.py`](../src/cached_response/matching.py). It explains the cache goal,
requires full-context review of all numbered changes, treats input as untrusted,
and rejects changed targets, facts, permissions, deadlines or references. Examples
contrast a diagnostic trace change with a changed resource owner or expiry.

An optional synchronous `verifier_overrider(evidence)` receives:
`instruction`, `verification_kind="input_pair"`, `old_input`, `new_input`,
`cached_response`, and `segments` containing each change's `path`, `old` and `new`.
It also receives the configured verifier model/options. Return:

```json
{"decision": "SAFE", "reason": "Only a diagnostic label changed", "segments": [0], "patterns": []}
```

`segments` must enumerate all segment indexes in order, exactly once. Only a
`SAFE`, `UNSAFE` or `UNCERTAIN` decision and a string reason are accepted. `SAFE`
approves the pair; `UNSAFE` rejects it and remembers that rejection. `UNCERTAIN`
runs the original function without saving a decision. Invalid or timed-out reviews
also fall back. The verifier cannot override guards.

Existing callbacks using `safe_to_reuse: true` still approve. A legacy `false`
means uncertain, since it does not distinguish a known mismatch from doubt.
Contradictory boolean and three-way decisions are invalid.

Optional suggestions use `patterns=[{"segment": 0, "pattern": "..."}]`.
Accepted expressions have one allowlisted character class and a fixed or ranged
length from 1 to 128, optionally preceded by up to 32 literal letters, digits,
underscores or dashes. Examples: `\A[A-Za-z0-9_-]{1,64}\Z` and
`\Atrace_[a-z]{1,32}\Z` (escape backslashes in JSON).
Both old and new values must match. Arbitrary alternation, groups, repetition,
lookarounds, unbounded lengths and unknown classes are rejected. A full suggestion
set is required to learn. Suggestions inherit the judge's `SAFE` or `UNSAFE`
decision. Invalid suggestions do not invalidate a concrete decision. Never propose
a decisive rule from uncertainty or from an identifier's format alone.

SQLite persists pair decisions and learned patterns, bound to caller, original
entry, response, untouched context, declared paths, verifier model/options and
`verifier_version`. Change that version when a callback or its policy changes.
Learned patterns never apply to undeclared fields. Every reuse still checks the
full guard, references, validator and original response age. An `UNSAFE` decision
skips only its candidate. Uncovered changes ask the judge. Opposite decisions saved
under one key atomically become `UNCERTAIN`; that key cannot regain a decisive
fast path before expiry or a policy change. Pair/rule disagreements also ask the
judge. No judge reason or raw segment values are saved in these review records.
Decisions expire with that response; storage retains at most 4,096 review records.
The three-way review format relearns earlier approvals; source responses stay usable.

`verifier_timeout="10s"` bounds waiting for built-in and custom reviewers.
Async provider calls are cancelled on deadline. Python cannot kill a synchronous
callback: at most four outstanding daemon workers can finish in the background,
without writing late approvals. Set provider-side timeouts too. Saturated review
capacity falls back immediately. Separate keys/processes can still duplicate reviews.

The total instruction and serialized evidence are limited to 60,000 characters.
Large shared top-level settings appear once under `shared_input`; merging these
into each input reconstructs both requests. No input is truncated. Larger requests
miss instead of receiving a review without sufficient context. Custom callbacks
receive the complete original dictionaries within the same size bound.

## Migration

Format version 6 uses exact input keys; version 5 normalized entries start cold.
Existing counter data remains inspectable. `metadata_paths` defaults to empty;
there is no automatic authority to ignore generated IDs or timestamps. History
alignment and response rebinding are removed from cache reuse. The internal pair
preparation helpers and old callback mapping fields are removed. Callbacks now
need explicit segment coverage. Review timeout changes from 90 to 10 seconds.
Logging remains silent and redacted by default; root logging is untouched.

Adding `metadata_rules` to the policy invalidates earlier policy keys once, even
when the setting is empty. Keep the database between later runs to measure warm
behavior. Exact hits skip normalization. Diagnostics cache up to 32 candidate
summaries; each cached source string is limited to 262,144 characters. Larger
inputs still receive diagnostics without entering that in-memory summary cache.

## Agent-guided configuration

`cached-response --skill` prints the bundled skill without opening a database.
`cached-response --install-skill DIR` writes
`DIR/configure-cached-response/SKILL.md` and refuses to overwrite an existing file.
Pip installation never modifies an agent's configuration automatically.

High-similarity miss examples include `next_step` with the skill name, read
command, capture API and authorization requirement. This is a hint for an agent,
not an automatic action. See [blocked-hit examples](learning-from-misses.md).

`capture_misses(path, include_text=False, limit=10, seconds=300,
max_bytes=8_000_000)` is a process-wide temporary capture context. It writes a new
JSONL file with mode 0600 on Unix. It captures misses in the serving process,
including when ordinary diagnostics are disabled. Disabled caching and explicit
cache bypasses do not produce capture records. Decorator-level capture hooks
override process defaults.

By default records contain redacted differences. `include_text=True` explicitly
adds the full current input and the closest candidate's input and cached response.
There is no new response yet at lookup time. Record and byte limits bound writes;
the deadline stops new captures, and context exit closes the file and restores
the previous hook. Oversized records are skipped whole. The yielded dictionary
reports `written`, `skipped` and `bytes`. The byte budget limits file size, not the
size of an incoming request held by the application.

Capture does not reconfigure logging or upload anything. Raw request bodies can
contain secrets; use authorized test traffic and keep files private. Redaction
always applies to default capture excerpts, even if `diagnostic_text=True` was
separately enabled. A custom `diagnostic_capture(diagnostic, details)` hook is
trusted process-local code with access to raw candidates; errors fail open to the
original function. Prefer the bounded context manager for investigations.

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
frequency and distance never approve reuse, and signature retrieval itself learns no rules.

`cache_signatures(path="cache.db", limit=20)` and
`cached-response --path cache.db --signatures` show persistent counters ordered by
request frequency. Each scoped signature records requests, hits, misses, first seen,
and last seen. Every request increments both kinds, so do not sum counts across
signature kinds. Miss counts describe cache decisions, including upstream failures.
Updates are atomic and survive process restarts; logging and in-memory diagnostic
settings do not control them. Counters contain hashes and numbers, with no input
excerpts. They accumulate until the cache database is removed. Existing response
payloads in that database still retain original inputs and outputs.

## Broader retrieval

`structural_matching=True` omits message roles from candidate grouping. It does
not permit changed histories, targets, instructions or opaque state. It requires
no NLTK dependency. `examples/minimal/structural_pairs.py` demonstrates that even
an approving model cannot override these guards.
