# Ignore unused numbers in isolated tests

Repeated tests can return different process IDs or numeric timestamps. Regex
rules handle text, so they cannot match these JSON numbers.

Use `TestMetadata` only for fields your test does not need:

```python
from cached_response import TestMetadata, configure

configure(
    mode="testing",
    test_metadata=(
        TestMetadata(
            tool="inspect_job",
            paths=(("created_at",), ("events", "*", "pid")),
        ),
    ),
)
```

Three rules explain the behavior:

1. Match an exact tool name and explicitly declared numeric fields in its linked result.
2. Replace those fields with typed zero before both cache lookup and model inference.
3. Keep other input unchanged; changed facts, instructions and provider state still prevent exact reuse.

Paths are tuples of JSON keys relative to the tool result. `"*"` selects list
elements, not arbitrary dictionary keys. Embedded JSON text works too. The default
type is `"int"`; use `value_type="float"` for floats. Booleans and numeric strings
are not converted. Missing fields stay missing. Tool schemas, system messages,
tool arguments and protected provider state are not projected.

This is an explicit **input change**, not an attempt to reconstruct old numbers
in an answer. The model sees zero on misses too, including `use_cache=False`.
Do not declare identifiers, quantities, deadlines, time intervals, or any value
needed by the answer. Equalities and ordering between declared numbers are lost.
For IDs that must preserve relationships, use [test aliases](test-id-aliases.md).

The default is empty. Disabled and conservative modes never apply these rules.
Rule configuration separates cache entries, so enabling a rule starts a new
cache policy. For fair timing comparisons, use testing mode in both arms and
`use_cache=False` for the arm that should still call the model.

## Learning rules with an LLM

The structured rule format is also the target for future learned proposals.
Automatic numeric-rule learning is **not implemented yet**.

The intended flow is: inspect real missed pairs, ask an LLM to propose a tool,
paths and numeric type, then replay the proposal against examples and meaningful
changes. Activation must stay inside application-declared testing scope. A number
looking like a timestamp is not enough: the test must establish that it is unused.
Uncertain proposals stay inactive. Learned rules should retain their evidence and
support rollback. Existing regex learning does not authorize numeric projection.

For local investigation, enable `diagnostic_raw_inputs=True` explicitly. It records
the original caller input separately from the projected lookup input. Logging
stays silent and redacted by default; keep private captures out of source control.
