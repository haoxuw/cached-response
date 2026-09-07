import asyncio
from concurrent.futures import ThreadPoolExecutor
import enum
import json
import subprocess
import sys
import time

import pytest

from cached_response import Rule, cached_llm_response, cached_staticmethod, configure
from cached_response.config import Config, refresh_probability
from cached_response.normalize import normalize, rebind

CONTEXT = "This is repeated context for an isolated cache test. " * 15
UUID_A = "f3c07f44-685d-493e-bf14-8ac1f134191f"
UUID_B = "952056da-bf56-45e7-9107-c856207a4b80"


def body(text=""):
    return {"model": "fixed-model", "messages": [{"role": "system", "content": CONTEXT},
                                                {"role": "user", "content": text}]}


@pytest.fixture(autouse=True)
def cache_environment(monkeypatch, tmp_path):
    configure(path=tmp_path / "private" / "cache.sqlite3", mode="disabled")
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "conservative")


@pytest.mark.parametrize("environment", [None, "disabled", "production", "staging", "test", ""])
def test_disabled_means_no_cache_io(monkeypatch, tmp_path, environment):
    if environment is None:
        monkeypatch.delenv("CACHED_RESPONSE_MODE")
    else:
        monkeypatch.setenv("CACHED_RESPONSE_MODE", environment)
    calls = []

    @cached_llm_response
    def ask(request):
        calls.append(request)
        return object()

    assert ask(body()) is not ask(body())
    assert len(calls) == 2
    assert not (tmp_path / "private").exists()


def test_word_threshold_and_use_cache(tmp_path):
    calls = []

    @cached_llm_response(report=False)
    def ask(request):
        calls.append(request)
        return {"answer": len(calls)}

    short = {"messages": [{"content": "word " * 99}]}
    assert ask(short) != ask(short)
    assert not (tmp_path / "private").exists()
    long = {"messages": [{"content": "word " * 100}]}
    assert ask(long) == ask(long)
    assert ask(long, use_cache=False) != ask(long)
    assert len(calls) == 4


@pytest.mark.parametrize("result", [None, False, [], {}, {"answer": [1, 2]}, (1, 2), b"bytes"])
def test_exact_serializable_results(result):
    calls = []

    @cached_staticmethod(report=False)
    def compute(value=3):
        calls.append(value)
        return result

    assert compute() == compute(value=3) == result
    assert len(calls) == 1


def test_staticmethod_and_enum_input():
    class Color(enum.Enum):
        RED = "red"

    class Example:
        @staticmethod
        @cached_staticmethod(report=False)
        def compute(kind, color):
            return {"type": kind.__name__, "color": color.value}

    assert Example.compute(int, Color.RED) == Example.compute(int, Color.RED)


def test_uuid_references_are_rebound():
    calls = []

    @cached_llm_response(report=False)
    def ask(request):
        calls.append(request)
        identifier = request["messages"][-1]["content"]
        return {"text": f"Read {identifier}.", "arguments": json.dumps({"id": identifier})}

    ask(body(UUID_A))
    result = ask(body(UUID_B))
    assert result == {"text": f"Read {UUID_B}.", "arguments": json.dumps({"id": UUID_B}, separators=(",", ":"))}
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["conservative", "testing", "risky"])
@pytest.mark.parametrize("before,after", [("inspect", "delete"), ("503", "403"), ("view", "cluster-admin")])
def test_meaningful_changes_miss(monkeypatch, mode, before, after):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", mode)
    calls, judges = [], []

    @cached_llm_response(report=False)
    def ask(request):
        if request.get("response_format") == {"type": "json_object"}:
            judges.append(request)
            return {"safe_to_reuse": False, "reason": "The error code matters"}
        calls.append(request)
        return "answer"

    ask(body(before))
    ask(body(after))
    assert len(calls) == 2

    assert len(judges) == int(mode in ("testing", "risky") and before == "503")


def test_testing_handles_execution_ids_and_metadata(monkeypatch):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "testing")
    calls = []

    @cached_llm_response(report=False)
    def ask(request):
        calls.append(request)
        return {"arguments": json.dumps({"task_id": "t_1234abcd"})}

    first = body('work kanban task t_1234abcd')
    second = body('work kanban task t_9876abef')
    ask(first)
    assert "t_9876abef" in ask(second)["arguments"]
    assert len(calls) == 1


def test_alias_relationship_is_preserved():
    same = normalize(body(f"Compare {UUID_A} with {UUID_A}"), "conservative")
    different = normalize(body(f"Compare {UUID_A} with {UUID_B}"), "conservative")
    assert same.body != different.body


def test_urls_remain_exact():
    first = normalize(body(f"https://example.com/{UUID_A}"), "risky")
    second = normalize(body(f"https://example.com/{UUID_B}"), "risky")
    assert first.body != second.body


def test_substitution_is_simultaneous_and_signatures_opaque():
    previous = {"a": UUID_A, "b": UUID_B}
    current = {"a": UUID_B, "b": UUID_A}
    result = rebind({"text": f"{UUID_A} {UUID_B}", "thought_signature": UUID_A}, previous, current)
    assert result == {"text": f"{UUID_B} {UUID_A}", "thought_signature": UUID_A}


def test_ambiguous_output_references_are_rejected():
    with pytest.raises(ValueError):
        rebind({"text": f"prefix{UUID_A}"}, {"id": UUID_A}, {"id": UUID_B})
    with pytest.raises(ValueError):
        rebind({"answer": 9}, {"run": 9}, {"run": 10})


def test_litellm_embedded_thought_signatures_are_preserved():
    def request(identifier):
        value = body()
        value["messages"].append({"role": "assistant", "tool_calls": [{"id": identifier}]})
        return value

    a = normalize(request("call_123456__thought__opaque+/="), "testing")
    b = normalize(request("call_987654__thought__opaque+/="), "testing")
    c = normalize(request("call_987654__thought__different+/="), "testing")
    assert a.body == b.body
    assert a.body != c.body
    assert rebind({"id": "call_123456__thought__opaque+/="}, a.bindings, b.bindings) == {"id": "call_987654__thought__opaque+/="}


def test_custom_rule_and_validator():
    calls = []

    @cached_llm_response(rules=(Rule("job", r"\bjob_[a-f0-9]{4}\b"),), validator=lambda old, new: False, report=False)
    def ask(request):
        calls.append(request)
        return "answer"

    ask(body("job_abcd"))
    ask(body("job_abce"))
    assert len(calls) == 2


def test_model_and_tool_schema_changes_miss(monkeypatch):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "risky")
    calls = []

    @cached_llm_response(report=False)
    def ask(request):
        calls.append(request)
        return "answer"

    request = body(UUID_A)
    ask(request)
    ask({**request, "model": "another-model"})
    ask({**request, "tools": [{"name": "tool", "description": UUID_B}]})
    assert len(calls) == 3


def test_cache_error_does_not_rerun_application(monkeypatch):
    calls = []

    @cached_staticmethod(report=False)
    def compute(value):
        calls.append(value)
        raise RuntimeError("application failure")

    with pytest.raises(RuntimeError, match="application failure"):
        compute(3)
    assert calls == [3]


def test_thread_single_producer():
    calls = []

    @cached_staticmethod(report=False)
    def compute(value):
        calls.append(value)
        time.sleep(0.1)
        return {"value": value}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compute, [3] * 8))
    assert results == [{"value": 3}] * 8
    assert len(calls) == 1


def test_async_single_producer():
    calls = []

    @cached_llm_response(report=False)
    async def ask(request):
        calls.append(request)
        await asyncio.sleep(0.1)
        return {"answer": "done"}

    async def run():
        return await asyncio.gather(*(ask(body()) for _ in range(8)))

    assert asyncio.run(run()) == [{"answer": "done"}] * 8
    assert len(calls) == 1


def test_refresh_probability():
    assert refresh_probability(0, 3, 7) == 0
    assert refresh_probability(3, 3, 7) == 0
    assert refresh_probability(5, 3, 7) == 0.5
    assert refresh_probability(7, 3, 7) == 1
    assert refresh_probability(20, 3, 7) == 1
    with pytest.raises(ValueError):
        Config(refresh_start=7, refresh_force=3)


def test_decorator_mode_is_the_only_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "risky")
    calls = []

    @cached_llm_response(mode="disabled")
    def disabled(request):
        calls.append(request)
        return len(calls)

    assert disabled(body()) != disabled(body())
    assert not (tmp_path / "private").exists()
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "disabled")

    @cached_llm_response(mode="testing", report=False)
    def active(request):
        calls.append(request)
        return len(calls)

    assert active(body()) == active(body())
    assert len(calls) == 3


def test_readable_durations_and_decorator_options():
    from datetime import timedelta
    config = Config(refresh_start="12h", refresh_force=timedelta(days=7))
    assert config.refresh_start == 43200
    assert config.refresh_force == 604800
    calls = []

    @cached_llm_response(mode="testing", refresh_start="1d", refresh_force="7d", report=False)
    def ask(request):
        calls.append(request)
        return "answer"

    assert ask(body()) == ask(body())
    assert len(calls) == 1
    with pytest.raises(ValueError):
        Config(refresh_start="tomorrow")


def test_expiry_refreshes_and_resets_age(monkeypatch, tmp_path):
    import sqlite3
    calls = []

    @cached_staticmethod(refresh_start="1d", refresh_force="7d", report=False)
    def compute(value):
        calls.append(value)
        return len(calls)

    assert compute(1) == compute(1) == 1
    path = tmp_path / "private" / "cache.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE entries SET created=created-?", (8 * 86400,))
    assert compute(1) == 2
    assert compute(1) == 2
    assert len(calls) == 2


def test_serialization_tags_cannot_collide():
    calls = []

    @cached_staticmethod(report=False)
    def compute(value):
        calls.append(value)
        return isinstance(value, type)

    assert compute(int) is True
    assert compute({"$type": "builtins.int"}) is False
    assert len(calls) == 2


def test_persistence_across_processes(tmp_path):
    script = tmp_path / "process.py"
    marker = tmp_path / "calls"
    script.write_text('''from cached_response import cached_staticmethod, configure
from pathlib import Path
import sys
configure(path=Path(sys.argv[1]).with_suffix(".sqlite3"))
@cached_staticmethod(report=False)
def compute(value):
    with Path(sys.argv[1]).open('a') as f: f.write('called\\n')
    return {'value': value}
assert compute(3) == {'value': 3}
''')
    for _ in range(2):
        subprocess.run([sys.executable, str(script), str(marker)], check=True)
    assert marker.read_text() == "called\n"


def test_risky_learns_scoped_patterns(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHED_RESPONSE_MODE", "risky")
    import sqlite3
    calls = []

    @cached_llm_response(report=False)
    def ask(request):
        calls.append(request)
        return "answer"

    ask(body("abcd1234"))
    ask(body("efgh5678"))
    assert len(calls) == 1
    with sqlite3.connect(tmp_path / "private" / "cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0
