# Reuse model work across fresh tests

**Experimental and opt-in.** This helps developers repeat the same agent test
when each run creates a new random task ID. Tools still run for the current task.
Changed facts, instructions and settings still need a new model response.

## How it works

Your app maps a fresh test handle to a stable handle that fits its tool schema:

```text
Provider sees: inspect("test_00000001")
First test executes: inspect("test_ab12cd34")
Next test executes:  inspect("test_ef56ab78")
```

The package translates IDs **before both cache lookup and the original model
call**. It stores the canonical response, then renders the current IDs for the
application. Requests must otherwise match exactly, unless separate declared
metadata rules apply. Similarity never grants permission to reuse.

This contract is for random handles in isolated tests. It must not rename real
resource targets, users, permissions, dates or meaningful facts.

## Configure an explicit test contract

`test_aliases(request)` returns `(conversation_key, {actual_id: stable_id})`, or
`None` when the request has no approved test handles. Here is an application-owned
example for a test that creates exactly one fresh task. This example app puts
`{"test_task_id": "test_ab12cd34"}` in its first message:

```python
import json
from cached_response import cached_llm_response


def isolated_task(request):
    task_id = json.loads(request["messages"][0]["content"])["test_task_id"]
    return task_id, {task_id: "test_00000001"}


@cached_llm_response(mode="testing", test_aliases=isolated_task, alias_version="1")
def ask(request):
    return your_model_call(request)
```

Try the [complete local example](../examples/minimal/test_handles.py) without
credentials: `python examples/minimal/test_handles.py`.

The package has no built-in task-ID pattern. Choose a stable handle that your
schema accepts. Select active handles from structured task data; old IDs in
system-prompt examples must stay unchanged. IDs must contain 8–128 ASCII letters,
digits, underscores or dashes. The callback receives a copy of the input.
It must be quick and local.

For multiple handles, your callback must assign stable, distinct values and a
stable conversation key. SQLite retains earlier mappings across turns, truncated
history and process restarts. A handle cannot change its mapping, and two handles
cannot collapse into one. Existing canonical text cannot collide with an unrelated
input value. Limit: 128 handles per conversation.

Change `alias_version` when the contract changes. Start fresh conversations when
changing mode or contract; never change either midway through signed history.
`use_cache=False` skips caching while retaining an active test-ID translation.
Conservative and disabled modes do not activate this feature.

## Why thought signatures need special handling

A thought signature is encrypted model state, not a task ID. Google requires
signed function-call parts to be sent back unchanged. See
[Google's thought-signature guide](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures).

The adapter keeps signature bytes unchanged. It records the original tool-call
arguments, checks that the next turn still describes that call, and restores the
original argument text before contacting the provider. This also handles clients
that parse and reformat argument JSON. It never guesses a replacement signature.
Unknown or altered signed calls raise an error instead of sending mismatched
history. Old responses generated from raw task IDs are not converted into new
canonical entries.

Streaming responses are buffered until complete, so IDs split across chunks can
be rendered correctly. This delays the first token on misses. Incomplete,
unsupported or oversized streams cannot be rendered as successful cache hits.
Text, JSON and successful JSON/SSE HTTP responses are supported.

Mappings and original arguments live in the cache database, with private file
permissions. Keep this database private. They share its 4,096-record `reviews` storage
and expire after `refresh_force` since last recorded use. Missing signed history
fails closed; do not delete the database during an active conversation.

## Prove a rule before relying on it

Start with a few isolated tests. Verify that tools receive the fresh task ID,
the current task completes, and reported facts match fresh tool results. Reset
task history and workspaces between repetitions; retain only the model cache.
A fresh chat alone does not reset an agent's recent-work summaries.

Compare cache-off, cold and warm runs using the same model and task. Include all
attempts, failures, review time and end-to-end latency. A regex proposal or a
judge approval is not proof of a speedup. Automatic learning still covers
[declared metadata](learning-from-misses.md); test-handle contracts require caller
configuration.
