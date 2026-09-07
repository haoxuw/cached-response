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
| `testing` | Also prefixed hexadecimal identifiers and recognized provider tool-call IDs. Can ask the existing HTTP model endpoint to approve additional normalization rules. |
| `risky` | Also automatically discovered shapes of rare tokens containing both letters and digits. Learned patterns are reused automatically. |

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

Every enabled call reports hit/miss and the cumulative fraction served from
cache to stderr. These are response counts, not token or cost savings. A bypass
or failed upstream call is not a hit. Production bypasses emit no cache metrics.
`cache_stats()` returns these process-local counters; `cached-response --path
PATH` reports persistent entry count and size without displaying prompts.

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

Testing mode includes a default verification prompt and can learn small rules on
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

The prototype considers at most eight recent entries with matching function,
HTTP identity, model settings, tools, and message roles. It proposes only small
numeric or generated-looking line changes and makes at most one verifier call
per lookup. Learned rules keep an exact hash of everything outside their approved
spans and are tied to the original cached input and response. They do not ignore
arbitrary task history or changed provider state. Values learned by this path
are ignored only when they are not detected in the returned response; the
prototype does not rewrite those learned values. Existing identifier rebinding
still applies.

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
This experimental path currently runs only in `testing` mode.

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
