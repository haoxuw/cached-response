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
| `testing` | Also prefixed hexadecimal identifiers and recognized provider tool-call IDs. Can ask the existing model to approve scoped normalization rules or a concrete input pair. |
| `risky` | Also automatically discovered shapes of rare tokens containing both letters and digits, plus the verification paths available in testing. Learned patterns are reused automatically. |

Numeric values stay exact until an explicit rule is approved in testing mode;
names such as `created_at` or `run_id` do not authorize ignoring their values.
There is no application-specific list of ID prefixes. The hexadecimal detector
requires both letters and digits and preserves the prefix when comparing IDs.
The built-in normalizers keep ordinary alphabetic words, model settings, and tool schemas. Opaque
provider signature fields recognized by the package, including signatures embedded in LiteLLM tool-call IDs,
are preserved. Risky mode uses the least frequent 5%
of vocabulary to propose machine-token patterns, never arbitrary English words.
Patterns are scoped to the function, model settings and tool schemas, HTTP
identity, full request URL, and other bound handler arguments, and the position of the value. Once rules are learned
for a scope, later calls apply them without repeating the frequency count. There
is no human review queue. These frequency-based risky rules are separate from
the experimental LLM-approved testing rules described below.

Matching requires the rest of the normalized request to be exactly equal.
Distinct identifiers get distinct placeholders; repeated references stay linked.
On a hit, the package substitutes the new identifiers into cached text and tool
arguments. Ambiguous substitutions cause a fresh model call. Stream hits emit
complete, coalesced SSE events; original chunk sizes and timing are not preserved.

**All three modes can make incorrect matches.** A timestamp can describe an
expired certificate, a UUID can identify another resource, and a rare token can
be an important error code. Long context does not make rare information harmless.
Replacing visible identifiers also cannot rewrite hidden provider reasoning
state. Validate tool outcomes and continuation with your provider before relying
on normalized replay, even in conservative mode.

The [input relevance checklist](input-relevance.md) lists information that
can affect inference and compares ways to recognize it without conversation
tracking. It distinguishes format recognition from permission to ignore a value.

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
matching model settings and tools. Reuse requires matching message roles; when
that group has no candidates, diagnostics can inspect nearby role sequences and
report `message_role_sequence_changed` with both message counts. This observation
does not authorize cross-sequence reuse. Non-chat argument shapes use
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
`validator_rejected`, `validator_error`, `proposal_rejected` (with a fixed
explanation such as `Provider state changed`), `change_fraction_too_large`,
`output_references_changed_value`, `verifier_rejected`, `invalid_verdict`,
`verifier_error`, `verifier_input_too_large`, `refresh`, or decoding/rebinding
failures. Saved-rule checks also expose guard and shape mismatches. Masking a
free-text verifier explanation retains the machine-readable rejection reason.
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

Testing and risky modes include a default verification prompt and can learn small rules on
an exact-cache miss. For HTTP chat-completion handlers it reuses the undecorated
handler. For sync or async Python functions taking one chat-request dictionary
with a `messages` list, it reuses the undecorated function. The verification request
uses system/user messages and requests a JSON verdict, without tools. Other call
shapes need an override; unsupported or uncertain replies cause a normal miss.
`learning=False` disables this extra path.

No user-written prompt or hook is required for the supported wrappers. To replace
the default check, pass a synchronous `verifier_overrider(evidence)` callback
returning `{"safe_to_reuse": bool, "reason": str}`. Its evidence includes the
package's default `instruction`; the callback may replace that instruction or use
another model. Configure a timeout in that model client.

Use your own model through a lambda; `my_json_llm` here is your adapter that returns
the verdict dictionary above. The package supplies the verification instructions,
current input, cached response, and proposed regexes and differences.

```python
import json

@cached_llm_response(
    mode="testing",
    verifier_overrider=lambda evidence: my_json_llm(
        system=evidence["instruction"], user=json.dumps(evidence),
    ),
)
async def ask_llm(request):
    return await existing_model_call(request)
```

The verification paths consider at most eight recent entries with matching function,
HTTP identity, model settings, tools, and message roles. Rule learning proposes small numeric or generated-looking line changes. Both
paths together make at most one verifier call per lookup. Learned rules keep an exact hash of everything outside their approved
spans and are tied to the original cached input and response. They do not ignore
arbitrary task history or changed provider state. Values learned by this path
are ignored only when they are not detected in the returned response; the
prototype does not rewrite those learned values. Existing identifier rebinding
still applies.

If ordinary rebinding or rule proposal rejects a candidate, a separate **input-pair
review** can handle changed tool/assistant content and additional identifiers.
Identifiers are aligned by their complete structured paths and local occurrence
positions, preserving reference types. An old identifier without an unambiguous
mapping must not occur in the cached response. The verifier sees the complete old
and new inputs, the original response, and the rebound response; it must reject
changed targets, meaningful facts, stale answers, or uncertainty. This is model
judgment for tests, not a guarantee of semantic equivalence.

This path preserves system/user instructions and non-content controls after
identifier alignment, opaque provider state, object keys, value types, and list
lengths (including JSON embedded in tool text). It does not discard events,
ignore arbitrary history, or reuse across different message-role sequences.
Decoded tool/assistant content can change within that structure, including
multiline text. Unstructured text still requires semantic review in full.

Its evidence has `verification_kind="input_pair"`, `old_input`, `new_input`,
`original_cached_response`, `cached_response`, and numeric `reference_alignment`
details. `proposed_changes` is empty: no regex is being authorized. Approval
returns a `verified_pair` hit only for this lookup; it persists no learned rule
and must be obtained again even for the same pair. The total verifier-input
limit is 300,000 characters. Rejection, invalid output, timeout, or input beyond
that bound causes a miss. `learning=False` disables both verification paths;
conservative and disabled modes never use them.

Diagnostics retain the original failure and the subsequent pair rejection, such
as `unmapped_output_reference`, `pair_sequence_changed`,
`pair_instruction_or_control_changed`, or `pair_provider_state_changed`, with
redacted field paths. `normalization_state_changed` rejects candidates whose
stored bindings cannot be reconstructed, including changed risky-mode patterns.

Python generates anchored regexes with literal punctuation, observed character
classes, and observed length bounds. For example, a changing five-character
lowercase/alphanumeric suffix becomes `[a-z0-9]{5}`, keeping its surrounding text
literal. The LLM approves or rejects that regex; it cannot supply executable code
or a broader regex. Each difference includes its location and up to ten characters
of surrounding context. The verifier still sees the full input and cached answer;
the excerpts alone cannot establish relevance.

Ordinary wording uses only the two observed alternatives, escaped as literal
regex text. A third wording needs another approval. Large proportional changes
are eligible only in prior assistant messages and only as those finite alternatives;
long system context cannot hide a changed short user instruction. Added or removed
list items, including events, remain misses. Every repeated occurrence is checked,
but copies of the same value change share the 16-change discovery budget.

For learning, timestamps are compared by their locations instead of whether two
events happened to share the same timestamp. Each changed time still needs
approval; UUID and task-ID relationships remain checked. Provider token counts
and response creation metadata are excluded when checking whether the answer
refers to a changed value.

Rules are persisted in SQLite and checked without another model call on later
matches. Random rechecks are disabled by default to avoid paying repeatedly for
the same approval. Set `learning_recheck=0.05` to audit 5% of matches if desired.
Configure this and `verifier_timeout="90s"` on the decorator or through
`configure()`. That timeout controls built-in async/HTTP verification. Synchronous model calls
and custom overrides need a timeout in their own model client. A failed or
uncertain verification falls back to normal inference.
Expired answers stay expired; learning does not renew their age. Changing a
verifier's policy should also change the decorator's `version=` or `namespace=`.
These experimental verification paths run in `testing` and `risky` modes.

An approving model can be wrong. Exact equality outside a learned rule does not
prove that future values inside that rule are harmless. The verifier itself
consumes inference. The HTTP verifier reads the full current input, cached
response, and proposed differences, avoiding a second copy of the old input. Measure incorrect
reuse and total inference cost as well as cache hits. Production remains
disabled by the canonical mode setting.

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
