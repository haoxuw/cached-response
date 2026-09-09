# Which inputs matter to an LLM response cache?

Keep information that can change the answer, tool choice, target, permissions, or
reasoning state. Also keep information the API needs to interpret the request.
The following is a broad checklist; relevance ultimately depends on the task.
No vocabulary list or character detector can exhaustively decide relevance.

There are three separate questions:

1. Does this value affect what the model should answer?
2. Does it affect which resources or earlier messages the request refers to?
3. Does the provider need it to continue or validate the conversation?

A value can matter for any one of these reasons. Looking random does not answer
any of them. The implementation keeps undeclared changes exact and uses normalization only
for retrieval. Declared metadata still needs guarded review; model approval
cannot establish general semantic equivalence.

## Usually valuable: keep exact by default

| Information | Why it matters |
| --- | --- |
| Requested action: inspect, create, update, delete | Changes what the agent should do. |
| Negation: not, never, except, unless | Can reverse the instruction. |
| Goal and success criteria | Define what counts as completing the task. |
| User corrections and changed requirements | Override earlier assumptions. |
| Limits on scope | Distinguish one resource from an entire fleet. |
| Permission to act versus permission to explain | Changes whether a tool should run. |
| Read-only requirements | Exclude mutations. |
| Approval requirements and approval status | Determine whether an action is authorized. |
| Risk tolerance | Can change the recommended action. |
| Cost, time, and resource budgets | Constrain the solution. |
| Deadlines and urgency | Affect scheduling and prioritization. |
| Requested output language | Changes the response. |
| Requested detail, tone, and audience | Change how information is presented. |
| Output format, JSON schema, required fields | Define a valid response. |
| Citation and evidence requirements | Affect research and attribution. |
| Examples, counterexamples, and expected answers | Define the intended behavior. |
| System and developer instructions | Set the rules for the response. |
| Message roles | Distinguish instructions from user or tool content. |
| Message ordering | Establishes which statements are current. |
| Boundaries around quoted or untrusted content | Distinguish data from instructions. |
| Previous decisions and commitments | Affect continuation of the work. |
| Unresolved questions and failed attempts | Prevent repetition and guide the next step. |
| Pronouns and references such as this, previous, and second | Connect instructions to earlier context. |
| Which values are equal or different | Establishes relationships even when literal IDs are arbitrary. |
| User, customer, tenant, and organization identity | Can determine ownership, access, and personalization. |
| Roles, group membership, and access policies | Determine permitted actions and visible data. |
| Project, account, subscription, and workspace identifiers | Select the environment. |
| Cluster, namespace, database, and service names | Select the target resource. |
| Resource UUIDs, object IDs, and database keys | Can identify entirely different objects. |
| File paths, object-storage keys, and repository paths | Locate inputs and outputs. |
| URLs, hostnames, ports, IP addresses, and CIDRs | Identify endpoints and network boundaries. |
| Region, zone, and geographic location | Affect routing, availability, and local answers. |
| Locale, timezone, calendar, and unit conventions | Change interpretation of dates and numbers. |
| Application versions and feature flags | Change available behavior. |
| Git revisions, branches, and container image digests | Identify the exact code being discussed. |
| Dependency versions and lockfiles | Determine reproducibility and compatibility. |
| Function, class, module, and variable names in code | Can change which code executes or what it means. |
| Operators, punctuation, escaping, and indentation in code | Can alter behavior even when no word changes. |
| Capitalization, Unicode characters, and invisible separators | Can distinguish identifiers, change parsing, or be the subject of a security investigation. |
| Configuration values and environment variables shown to the model | Often explain the problem or select a target. |
| Error codes, exception types, and error messages | Distinguish different failures. |
| Stack traces and failing source locations | Locate the cause. |
| HTTP status codes and command exit codes | Distinguish success, denial, timeout, and failure. |
| Resource conditions and lifecycle status | Determine the appropriate response. |
| CPU, memory, storage, quota, and replica counts | Affect diagnosis and capacity decisions. |
| Latency, error rates, throughput, and queue depth | Describe system health. |
| Prices, balances, quantities, and measurements | Often directly determine the answer. |
| Units, signs, precision, and comparison operators | Distinguish 5 ms from 5 s, or less than from greater than. |
| Thresholds, limits, and policy parameters | Determine whether a condition is acceptable. |
| Booleans, nulls, empty values, and missing fields | Can represent different states. |
| List membership, ordering, and duplicates | Can affect ranking, sequence, or counts. |
| Search results and retrieved documents | Supply factual evidence. |
| Document revisions and data freshness | Determine whether evidence is current. |
| Tool results, including empty or partial results | Determine what the agent knows next. |
| Tool names and descriptions | Determine which capability the model selects. |
| Tool parameter schemas, defaults, enums, and required fields | Determine valid tool calls. |
| Tool arguments | Specify the actual operation and target. |
| Available tools and tool-choice restrictions | Change possible next actions. |
| Images, screenshots, audio, video, and binary attachments | Their content may contain the entire task. |
| OCR text, coordinates, bounding boxes, and media timestamps | Connect information to locations or moments. |
| Checksums, hashes, fingerprints, and expected signatures | Can be the evidence in an integrity or authenticity task. |
| Certificates, expiry dates, issuers, and subject names | Can determine why authentication is failing. |
| Credential scopes and authorization failures | Explain what access is available. |
| SQL, regular expressions, shell commands, and encoded scripts | Symbols and numbers can carry executable meaning. |

## Time and identifiers: frequently mistaken for noise

| Information | When it matters |
| --- | --- |
| Current date and time | Questions about today, current status, or what is due. |
| Event timestamps | Ordering incidents and establishing causality. |
| Certificate, token, lease, and subscription expiration | Determining whether something is valid now. |
| Start and finish times | Calculating duration or detecting stuck work. |
| Retry times, backoff, and attempt counts | Diagnosing retry storms and repeated failures. |
| Last heartbeat and last successful run | Detecting liveness and staleness. |
| Ages such as 2m ago | Distinguishing recent from old events. |
| Business dates and reporting periods | Selecting the correct records. |
| Random resource names | Selecting resources whose identities still differ. |
| Request IDs and trace IDs in diagnostic logs | Joining events across services. |
| Task IDs and run IDs | Selecting the task to inspect, update, or complete. |
| Process IDs | Diagnosing or operating on a specific process. |
| Pod UIDs and container IDs | Distinguishing a replacement from its predecessor. |
| Transaction IDs and idempotency keys | Distinguishing retries from new operations. |
| Revision numbers, generations, and resource versions | Detecting updates and avoiding stale writes. |
| Session, conversation, and response IDs | Identifying context maintained elsewhere. |
| Opaque pagination and continuation tokens | Selecting which results come next. |
| Nonces and challenges | Participating in a protocol or a validation task. |
| Random seeds | Can affect generated data or model behavior. |
| Encoded values | May encode meaningful text, media, state, or executable content. |

For example, two UUIDs in `inspect resource <UUID>` do not mean equivalent
requests. Two timestamps in `has this certificate expired?` do not mean
equivalent requests either. A process ID can be irrelevant to a report's wording
and essential to the command that the report recommends.

## Provider state and request settings: keep exact even when unreadable

| Information | Why it belongs in the cache identity |
| --- | --- |
| Provider, endpoint, deployment, model, and model revision | They can produce different behavior. |
| Temperature, top-p, sampling options, and seed | They change generation behavior. |
| Output token limits and stop sequences | They constrain or truncate the answer. |
| Reasoning effort, budget, and context settings | They change how reasoning is performed or retained. |
| Response format and constrained-decoding settings | They change valid outputs. |
| Safety settings and application policy versions | They change permitted behavior. |
| Prompt-template and adapter versions | They can change what the provider actually receives. |
| Retrieval corpus, index version, and access scope | They determine which evidence is available. |
| Provider-hosted conversation and previous-response references | They may stand for input absent from the visible request. |
| Thought signatures and encrypted reasoning | They carry hidden continuation state. |
| Encrypted compaction items | They may stand for earlier conversation content. |
| Tool-call IDs and result associations | The API needs to know which result answers which call. |
| Item types, roles, and block boundaries | The API uses them to interpret the payload. |

Gemini exposes `thoughtSignature`/`thought_signature` on content parts and
requires preservation in relevant tool-call histories. These values carry
reasoning context. [Google documentation](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)

Claude exposes `signature` on thinking blocks and `data` on redacted-thinking
blocks. It also uses the signature to verify that thinking came from Claude.
Preserve these blocks unchanged. [Anthropic documentation](https://platform.claude.com/docs/en/build-with-claude/thinking)

OpenAI Responses uses `encrypted_content` in reasoning items to carry reasoning
into subsequent requests. Identify the typed item, rather than relying on the
appearance of the string. [OpenAI documentation](https://developers.openai.com/api/docs/guides/reasoning)

Recognition can be stateless: inspect each request using its API schema. Unknown
formats should remain exact. Keeping the original signature on an upstream miss
does not make ignoring it in the cache key safe: a cache hit would still merge
potentially different hidden states. Replacing a visible task ID also cannot
rewrite that ID if it is present inside encrypted reasoning.

## Sometimes unimportant, but only under a known contract

| Candidate | Condition needed before treating it as interchangeable |
| --- | --- |
| Random test-fixture IDs | The fixtures have equivalent state, permissions, and relationships. |
| Temporary workspace directory names | Contents, permissions, and path-dependent behavior are equivalent. |
| Generated task labels | They are display labels, not selectors or instructions. |
| Request and trace IDs | Nothing needs to correlate them with other events. |
| Process IDs | No diagnosis, command, or result depends on process identity. |
| Absolute event times | Only the preserved order or duration matters, and current time is irrelevant. |
| Creation timestamps | Age, expiration, sorting, and freshness are irrelevant. |
| Retry counters | They do not describe failure history or change the next action. |
| Random suffixes in names | They do not distinguish resources or appear in meaningful commands. |
| Test session labels | The label is not connected to different environment state. |
| Log filenames | Their contents and role are equivalent and the name is not evidence. |
| Display-only progress markers | They do not affect task completion or tool decisions. |
| Whitespace outside JSON strings | Both requests are parsed into the same data structure. |
| JSON object key ordering | The API's object semantics ignore ordering; raw prompt strings are a different case. |
| Equivalent serialization escapes | The API decodes them into the same value. |
| Redundant derived metadata | Its source remains present and the consumer does not treat it separately. |
| Human-readable rendering of an ID | All references remain consistent and its literal text is unimportant. |
| Tool-call labels | The protocol permits relabeling and every associated result remains correctly connected. |

These conditions cannot be inferred reliably from the value's character
distribution. Even identical visible tool calls can have different encrypted
reasoning attached. Normalized replay is therefore an approximation when the
application has not supplied an equivalence contract.

## Usually irrelevant to generation when they stay outside the model request

These values are candidates for omission only if the serving stack does not use
them to change input, model selection, permissions, tools, or output behavior.

| Metadata | Important boundary |
| --- | --- |
| Cache lookup start and finish times | Local measurements, not model context. |
| Cache hit/miss counters | Observability only. |
| Local SQLite row IDs | Storage implementation only. |
| Local cache filenames and compression level | Storage representation only. |
| Proxy trace and span IDs | Only if no routing or diagnostic prompt uses them. |
| HTTP client connection IDs | Transport only. |
| TCP source ports and socket handles | Only if they do not affect routing or identity. |
| Process and thread IDs of the caching library | Only if not exposed as task context. |
| Network packet boundaries | Transport framing only. |
| HTTP header capitalization and ordering | Only where the protocol treats them equivalently. |
| Content length and transfer encoding | Only when they represent the same decoded body. |
| TLS session details | Only if they do not select a different identity or destination. |
| Internal queue sequence numbers | Only if they do not alter scheduling-dependent inputs. |
| Metrics-exporter labels | Only if they are not used in routing or policy. |
| Local test repetition labels | Only if they are absent from the model request and application behavior. |

Authentication is a special case: the model may never see an authorization
header, but it can still select a tenant, accessible data, or provider account.
Keep an appropriate identity/access scope in the cache key even when the raw
credential is not part of the prompt. Likewise, external state that is missing
from the arguments can still make a cached answer stale.

## Approaches that cover variants

Separate recognition from permission to ignore. A parser can establish that a
value is a timestamp. It cannot establish that the timestamp is irrelevant to
the question. No fully generic method can classify every arbitrary input safely:
the same UUID can be an interchangeable fixture label in one task and the exact
object to delete in another. Encrypted state creates an additional information
barrier because the cache cannot inspect its meaning.

| Approach | What it catches well | What it cannot establish | Recommended use |
| --- | --- | --- | --- |
| API schema and typed-object recognition | Declared thought, reasoning, compaction, tool-call, and message blocks | Undocumented provider extensions and semantic equivalence | Preserve complete protocol blocks; use built-in adapters. |
| Exact field paths within a recognized schema | CamelCase/snake_case variants and nested metadata with known roles | Whether an arbitrary field called `id` or `signature` has the same meaning | Match provider/API plus block type plus field, rather than field name alone. |
| SDK type inspection | Structured SDK objects and discriminated unions | Meaning of arbitrary strings or extra fields | Convert recognized objects without dropping unknown fields. |
| JSON parsing and canonical serialization | Object key order, external whitespace, and equivalent escaping | JSON inside prompt text is not automatically equivalent to the model | Canonicalize the API envelope; be more cautious with embedded text. |
| Case folding and Unicode normalization | Some textual spelling and representation variants | Whether the application treats those strings as equivalent | Apply only where the protocol defines equivalence; preserve arbitrary prompt text and identifiers. |
| UUID parsers | Recognized UUID syntax without depending on one literal value | Whether two UUIDs identify equivalent resources | Mark a candidate; preserve it unless its role allows substitution. |
| Date/time parsers | Supported timezone, offset, fractional-second, and date formats | Freshness, expiration, and temporal relevance | Identify type; retain semantics, time ordering, and duration when relevant. |
| URL, IP, CIDR, and path parsers | Structured locations and addresses | Whether different destinations are interchangeable | Protect them as resource selectors by default. |
| Programming-language parsers | Function names, identifiers, literals, operators, and valid syntax | Incomplete snippets, every language, or external runtime behavior | Protect code; if parsing fails, preserve the whole fragment. Python's standard `ast` module helps with Python. |
| Log-format parsers | Labeled timestamps, levels, messages, trace IDs, and process IDs | Whether correlation or timing is essential to the diagnosis | Keep message and relationships; omit fields only under a known log contract. |
| Base64/hex decoding checks | Valid encoded strings across many lengths | Whether the decoded content is reasoning, media, a hash, or task data | Identify opaque data to preserve, not automatic noise to remove. |
| Entropy and character-distribution scores | Many generated-looking tokens without enumerating every prefix | Their purpose, relevance, or interchangeability | Candidate detection or protection only; never sufficient for a hit. |
| Low-frequency vocabulary | Unusual tokens in a long request | Whether rarity implies irrelevance | Candidate discovery only. Important errors and instructions can occur once. |
| Dictionaries and word segmentation | Many ordinary words and some words joined into identifiers | Domain vocabulary, abbreviations, other languages, and contextual importance | Optional protection signal; not an authorization to redact unknown words. |
| Named-entity recognition | Many people, organizations, dates, and places | Complete coverage of technical IDs or whether an entity matters | Usually a reason to preserve a value, not remove it. |
| Pairwise structural diff | Exact locations changed between two requests | Whether those changes are harmless | Require an authorized normalization rule for every difference. |
| Consistent typed placeholders | Repeated references, distinct values, and structural equality after allowed substitutions | Whether the substituted resources or hidden states are equivalent | Preserve one-to-one mappings and reject ambiguous output rebinding. |
| Embedding similarity | Related topics and paraphrases | Exact negation, numbers, permissions, tool arguments, or equivalent actions | Candidate search only; unsuitable as the final cache-hit condition. |
| LLM relevance classification | Contextual judgments beyond simple syntax | Guaranteed correctness, complete opaque-state interpretation, or zero inference cost | Optional experimental rejection/checking; never a proof. |
| A blacklist of important fields | Known dangerous normalization sites | All future fields and wrappers | Useful defense, but insufficient alone. Unknown values should stay exact. |
| An allowlist of irrelevant metadata roles | Fields whose role is already established by the integration | Undeclared metadata embedded in arbitrary prose | Strongest automatic omission rule when the contract is correct. |
| Comparison with a fresh model run | Observed answer and tool-outcome agreement | All future requests; model outputs are stochastic | Measure false hits and coverage on real workloads. |
| Changed-fixture negative tests | Concrete cases where similar-looking input must produce different behavior | Every possible semantic edge case | Change permissions, resource identity, expiry, error codes, and tool results deliberately. |

A small implementation can use standard-library parsers (`json`, `uuid`,
`datetime`, `ast`, `urllib.parse`, `ipaddress`, `base64`, and `re`) plus a compact
registry of provider formats. No NLP package is needed for that foundation.
Supporting new APIs still requires maintaining their contracts; eliminating
user setup does not eliminate package maintenance.

For a stateless normalizer, apply the same procedure independently to every
request:

1. Recognize the API envelope and its typed blocks.
2. Keep model settings, instructions, tools, selectors, code, unknown fields,
   and opaque provider state exact.
3. Omit only metadata whose irrelevance is established by the API/application
   contract. Apply broader candidate rules only in explicitly approximate modes.
4. Give allowed substitutions distinct typed placeholders and preserve repeated
   references. Do not discard word order, punctuation, or structural boundaries.
5. Require exact equality of everything that remains and an unambiguous output
   substitution. Otherwise run inference.

This procedure needs no record of which conversation originally produced a
value. Persistent automatic rule learning is a separate, stateful choice: it
can make normalization depend on earlier traffic, so it should not be confused
with this stateless recommendation.

“Catch all variants” has two different meanings. Covering documented spellings
and structures is practical with parsers and adapters. Automatically discovering
every semantically irrelevant value in arbitrary prompts, without mistakes or
application knowledge, is not a guarantee this package can make.
