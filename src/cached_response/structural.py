"""Bounded history alignment for opt-in, individually verified cache pairs."""

from collections import defaultdict
from difflib import SequenceMatcher

from .normalize import PROTECTED, THOUGHT_SEPARATOR, dumps, rebind
from .pairwise import PAIR_MARKER, REFERENCE, PairRejected

MAX_MESSAGES = 256
MAX_CANDIDATES = 24
INSTRUCTION_ROLES = {"system", "developer", "user"}
DATA_ROLES = {"assistant", "tool"}


def aligned_messages(old, current):
    if max(len(old), len(current)) > MAX_MESSAGES:
        raise PairRejected("history_alignment_limit")
    if not old or not current or old[-1].get("role") != current[-1].get("role"):
        raise PairRejected("history_terminal_role_changed")

    def tokens(messages):
        owners = {
            call.get("id"): call.get("function", {}).get("name")
            for message in messages
            for call in message.get("tool_calls", [])
        }
        result = []
        for message in messages:
            role = message.get("role")
            if role not in INSTRUCTION_ROLES | DATA_ROLES:
                raise PairRejected("history_unknown_role")
            names = tuple(
                call.get("function", {}).get("name")
                for call in message.get("tool_calls", [])
            )
            if role == "tool":
                names = (owners.get(message.get("tool_call_id")),)
            result.append((role, names))
        return result

    left, right = tokens(old), tokens(current)
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    pairs = [
        (block.a + i, block.b + i)
        for block in matcher.get_matching_blocks()
        for i in range(block.size)
    ]
    # Instructions are mandatory anchors, including repeated user messages.
    a = [i for i, m in enumerate(old) if m["role"] in INSTRUCTION_ROLES]
    b = [i for i, m in enumerate(current) if m["role"] in INSTRUCTION_ROLES]
    if len(a) != len(b):
        raise PairRejected("history_instructions_changed")
    required = list(zip(a, b))
    if any(pair not in pairs for pair in required):
        raise PairRejected("history_instruction_alignment_ambiguous")
    return pairs


def prepare(
    old, current, previous_bindings, current_bindings, response, state_keys
):
    if PAIR_MARKER in dumps(old) + dumps(current):
        raise PairRejected("reserved_pair_marker")
    if dumps({k: v for k, v in old.items() if k != "messages"}) != dumps(
        {k: v for k, v in current.items() if k != "messages"}
    ):
        raise PairRejected("history_settings_changed")
    before, after = old["messages"], current["messages"]
    pairs = aligned_messages(before, after)
    targets, sources = defaultdict(set), defaultdict(set)

    def anchors(left, right):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in left.keys() & right.keys() - state_keys:
                anchors(left[key], right[key])
        elif isinstance(left, list) and isinstance(right, list):
            for a, b in zip(left, right):
                anchors(a, b)
        elif isinstance(left, str) and isinstance(right, str):
            if REFERENCE.sub("<REF>", left) == REFERENCE.sub("<REF>", right):
                for a, b in zip(
                    REFERENCE.findall(left), REFERENCE.findall(right)
                ):
                    if a.rsplit(":", 1)[-1] == b.rsplit(":", 1)[-1]:
                        targets[a].add(b)
                        sources[b].add(a)

    for a, b in pairs:
        anchors(before[a], after[b])
    mapping = {
        a: next(iter(bs))
        for a, bs in targets.items()
        if len(bs) == 1 and len(sources[next(iter(bs))]) == 1
    }
    output = dumps(response)
    for marker, value in previous_bindings.items():
        if marker not in mapping and str(value) in output:
            raise PairRejected("unmapped_output_reference")
    reverse = {b: a for a, b in mapping.items()}

    def align(value):
        if isinstance(value, dict):
            return {k: align(v) for k, v in value.items()}
        if isinstance(value, list):
            return [align(v) for v in value]
        if isinstance(value, str):
            return REFERENCE.sub(
                lambda m: reverse.get(m.group(), PAIR_MARKER + m.group() + ">"),
                value,
            )
        return value

    aligned = align(after)

    def protected(value):
        if isinstance(value, dict):
            return any(
                k in state_keys | PROTECTED or protected(v)
                for k, v in value.items()
            )
        if isinstance(value, list):
            return any(protected(v) for v in value)
        return isinstance(value, str) and THOUGHT_SEPARATOR in value

    def check(left, right, path=()):
        if type(left) is not type(right):
            raise PairRejected("pair_type_changed", path)
        if dumps(left) == dumps(right):
            return
        if isinstance(left, dict):
            for key in left.keys() | right.keys():
                if key in state_keys | PROTECTED:
                    if (
                        key not in left
                        or key not in right
                        or dumps(left[key]) != dumps(right[key])
                    ):
                        raise PairRejected(
                            "pair_provider_state_changed", (*path, key)
                        )
                elif key in left and key in right:
                    check(left[key], right[key], (*path, key))
                elif protected(left.get(key)) or protected(right.get(key)):
                    raise PairRejected(
                        "pair_provider_state_changed", (*path, key)
                    )
        elif isinstance(left, list):
            for i, (a, b) in enumerate(zip(left, right)):
                check(a, b, (*path, i))
            if any(
                protected(v) for v in left[len(right) :] + right[len(left) :]
            ):
                raise PairRejected("pair_provider_state_changed", path)
        elif isinstance(left, str) and THOUGHT_SEPARATOR in left + right:
            raise PairRejected("pair_provider_state_changed", path)

    for a, b in pairs:
        if before[a]["role"] in INSTRUCTION_ROLES:
            if dumps(before[a]) != dumps(aligned[b]):
                raise PairRejected(
                    "pair_instruction_or_control_changed", ("messages", a)
                )
        else:
            check(before[a], aligned[b], ("messages", a))
    used_a, used_b = {a for a, _ in pairs}, {b for _, b in pairs}
    for items, used in ((before, used_a), (after, used_b)):
        if any(protected(m) for i, m in enumerate(items) if i not in used):
            raise PairRejected("pair_provider_state_changed")
    result = rebind(
        response,
        {a: previous_bindings[a] for a in mapping},
        {a: current_bindings[b] for a, b in mapping.items()},
    )
    return result, {
        "mapped_reference_count": len(mapping),
        "unmapped_previous_reference_count": len(previous_bindings)
        - len(mapping),
        "unmapped_current_reference_count": len(current_bindings)
        - len(mapping),
        "message_alignment": [list(pair) for pair in pairs],
        "removed_messages": sorted(set(range(len(before))) - used_a),
        "added_messages": sorted(set(range(len(after))) - used_b),
        "requires_full_history_review": True,
    }
