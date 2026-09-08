"""Conservative preparation for verifier-approved, non-generalized input pairs."""

import re
from collections import defaultdict

from .normalize import MARKER, PROTECTED, THOUGHT_SEPARATOR, dumps, rebind

REFERENCE = re.compile(re.escape(MARKER) + r"[^⟫]+⟫")
PAIR_MARKER = "<CACHED_RESPONSE_PAIR_REFERENCE:"
EDITABLE_ROLES = {"assistant", "tool"}


class PairRejected(ValueError):
    def __init__(self, reason, path=()):
        super().__init__(reason)
        self.reason, self.path = reason, path


def footprints(value):
    """Identify references by all their structured paths and local occurrences."""
    found = defaultdict(list)

    def walk(item, path=()):
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, (*path, key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, (*path, index))
        elif isinstance(item, str):
            for index, match in enumerate(REFERENCE.finditer(item)):
                found[match.group()].append((*path, index))

    walk(value)
    return {key: frozenset(paths) for key, paths in found.items()}


def prepare(
    old, current, previous_bindings, current_bindings, response, state_keys
):
    """Map only unambiguous reference footprints; never drop structured data.

    This returns a candidate for semantic review, not authorization to reuse it.
    All non-content fields, instructions, and opaque state remain exact after
    alignment. Tool/assistant content may change only within identical structure.
    """
    if PAIR_MARKER in dumps(old) + dumps(current):
        raise PairRejected("reserved_pair_marker")
    previous, present = footprints(old), footprints(current)
    by_footprint = {paths: key for key, paths in present.items()}
    mapping = {
        key: by_footprint[paths]
        for key, paths in previous.items()
        if paths in by_footprint
        and key.rsplit(":", 1)[-1] == by_footprint[paths].rsplit(":", 1)[-1]
    }
    # No missing identifier may survive in the cached answer/action. The judge
    # cannot repair it or supply a mapping. Check the complete envelope too.
    output = dumps(response)
    for key, value in previous_bindings.items():
        if key not in mapping and str(value) in output:
            raise PairRejected("unmapped_output_reference")
    reverse = {new: old for old, new in mapping.items()}

    def align(item):
        if isinstance(item, dict):
            return {key: align(value) for key, value in item.items()}
        if isinstance(item, list):
            return [align(value) for value in item]
        if isinstance(item, str):
            return REFERENCE.sub(
                lambda m: reverse.get(m.group(), PAIR_MARKER + m.group() + ">"),
                item,
            )
        return item

    aligned = align(current)

    def check(left, right, path=()):
        if type(left) is not type(right):
            raise PairRejected("pair_type_changed", path)
        if not isinstance(left, (dict, list)) and left == right:
            return
        if left != right and any(
            part in state_keys | PROTECTED
            for part in path
            if isinstance(part, str)
        ):
            raise PairRejected("pair_provider_state_changed", path)
        if isinstance(left, str) and THOUGHT_SEPARATOR in left + right:
            raise PairRejected("pair_provider_state_changed", path)
        if isinstance(left, dict):
            if left.keys() != right.keys():
                raise PairRejected("pair_fields_changed", path)
            for key in left:
                check(left[key], right[key], (*path, key))
        elif isinstance(left, list):
            if len(left) != len(right):
                raise PairRejected("pair_sequence_changed", path)
            for index, (a, b) in enumerate(zip(left, right)):
                check(a, b, (*path, index))
        elif (
            len(path) < 3
            or path[0] != "messages"
            or path[2] != "content"
            or old["messages"][path[1]].get("role") not in EDITABLE_ROLES
            or current["messages"][path[1]].get("role") not in EDITABLE_ROLES
        ):
            raise PairRejected("pair_instruction_or_control_changed", path)

    check(old, aligned)
    mapped_old = {key: previous_bindings[key] for key in mapping}
    mapped_new = {
        key: current_bindings[value] for key, value in mapping.items()
    }
    result = rebind(response, mapped_old, mapped_new)
    return result, {
        "mapped_reference_count": len(mapping),
        "unmapped_previous_reference_count": len(previous_bindings)
        - len(mapping),
        "unmapped_current_reference_count": len(current_bindings)
        - len(mapping),
    }
