import re

from cached_response import Rule


def test_task_preset_is_bounded_and_requires_explicit_scope():
    rule = Rule.preset("task_id", paths=("metadata.trace",))
    assert rule.paths == ("metadata.trace",)
    for value in ("t_abc123", "t_02-aadfsa"):
        assert re.fullmatch(rule.pattern, value)
    for value in ("t_x", "some t_abc123 text", "t_" + "a" * 127):
        assert not re.fullmatch(rule.pattern, value)
