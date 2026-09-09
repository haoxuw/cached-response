# Turn a miss into a safe rule

A similar prompt may still need a different answer. Read the changed fields before
trying to raise the hit rate. These patterns came from recorded agent test traffic;
the snippets below use replacement names and IDs, not private captures.

| Changed input | Why it misses | Useful next step |
| --- | --- | --- |
| `work on task t_ab12cd34` → `work on task t_ef56ab78` | The user selected a different task. | Run the task lookup again. Do not erase its ID. |
| A system message's working directory changes | Commands may run against different files. | Keep the system prompt exact. |
| Another tool result is added to the conversation | The agent has new evidence. | Compare the new facts; do not drop history to force a match. |
| Task metadata changes `pid: 163` → `pid: 165` | A number's purpose is unknown; it might identify a process to stop. | Trace its use in the app. Current metadata matching accepts strings only. |
| A candidate exceeds the review model's input limit | The reviewer cannot see enough context. | Use a caller-reviewed metadata rule only after checking the full pair. |

The last row is a tested size limit; it was not a model approval in the live run.
Earlier high-hit tests used model approval for fields such as process IDs and lock
labels. Those approvals took time and did not prove the fields were always safe.

## A useful rule

Suppose your app emits `diagnostic_trace` only for debugging. You check the code
and real request pairs: nothing uses it as a target, and it never appears in the
answer. The following synthetic example shows the resulting configuration:

```python
from cached_response import Rule, cached_llm_response

@cached_llm_response(
    mode="testing",
    metadata_rules=(Rule.preset(
        "uuid", paths=("messages.*.content.diagnostic_trace",)
    ),),
)
def ask(request):
    return your_model_call(request)
```

Only that field may change. Both values must be UUIDs. Instructions, other facts,
provider state and output references still have to pass the guards. The rule
avoids a review call, including for large prompts. Changing an `owner_id` or an
expiry is not safe merely because it has the same format.

## Let an agent investigate

Read the skill shipped in the installed package:

```sh
cached-response --skill
cached-response --install-skill .agents/skills
```

Ask the agent to use `configure-cached-response` on a repeated test. It starts
with `cache_stats()` and redacted `cache_misses()` in the running process. A
high-similarity miss includes `next_step` with the skill and capture command.
Nothing automatically turns on logging or exports private text.

For authorized test traffic, `capture_misses(..., include_text=True)` temporarily
writes full pairs to a bounded private file. The agent reads them, proposes a
narrow rule, tests meaningful changes that must still miss, then measures total
time including review calls. Captures stay local; the reviewed configuration is
the useful result. See the [bundled skill](../src/cached_response/skills/configure-cached-response/SKILL.md)
for the complete workflow.
