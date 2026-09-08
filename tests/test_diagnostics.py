import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest


def test_excerpt_does_not_reveal_new_interior_edges():
    from cached_response.diagnostics import differences
    before = 'A' * 170 + 'LEAK' + 'X' * 26 + 'old' + 'B' * 200
    after = 'A' * 170 + 'LEAK' + 'X' * 26 + 'new' + 'B' * 200
    change = differences(before, after)[0][0]
    assert change['before'] == '*' * 160
    assert change['after'] == '*' * 160

from cached_response import (
    cache_misses,
    cache_stats,
    cached_llm_response,
    configure_logging,
)
from cached_response import decorators, diagnostics
from cached_response.config import Config
from cached_response.diagnostics import differences, prompt_stats, redact
from cached_response.storage import get_store
from test_learning import request


@pytest.fixture(autouse=True)
def isolated_metrics():
    with decorators._stats_lock:
        previous = decorators._stats.copy()
        decorators._stats.clear()
    with diagnostics._examples_lock:
        examples = list(diagnostics._examples)
        diagnostics._examples.clear()
    yield
    configure_logging(enabled=False)
    with decorators._stats_lock:
        decorators._stats.clear()
        decorators._stats.update(previous)
    with diagnostics._examples_lock:
        diagnostics._examples.clear()
        diagnostics._examples.extend(examples)


def test_default_is_silent_even_with_host_root_logging(tmp_path):
    script = """
import logging
import sys
logging.basicConfig(level=logging.DEBUG)
root = logging.getLogger()
handlers, level = list(root.handlers), root.level
third_party = logging.getLogger("httpx")
third_party.setLevel(logging.DEBUG)
from cached_response import cached_llm_response, cache_misses
@cached_llm_response(mode="testing", min_words=0, path=sys.argv[1])
def ask(body):
    return "answer"
ask("alice@example.com")
ask("alice@example.com")
assert cache_misses()
assert root.handlers == handlers and root.level == level
assert third_party.level == logging.DEBUG
logging.getLogger("host").warning("host still works")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "silent.db")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""
    assert result.stderr == "WARNING:host:host still works\n"


def test_package_directory_does_not_shadow_standard_logging():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; assert callable(logging.getLogger)",
        ],
        cwd=Path(diagnostics.__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr == ""


def test_verifier_miss_example_stats_and_file_are_redacted(tmp_path, capsys):
    calls, judges = [], []
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    path = tmp_path / "diagnostics.log"
    configure_logging(path=path, level=logging.DEBUG)

    def verify(evidence):
        judges.append(evidence)
        return {
            "safe_to_reuse": False,
            "reason": "alice@example.com needs live state",
        }

    @cached_llm_response(
        mode="testing", path=tmp_path / "cache.db", verifier_overrider=verify
    )
    def ask(body):
        calls.append(body)
        return "answer for alice@example.com"

    ask(request("07", user="Inspect alice@example.com"))
    ask(request("08", user="Inspect alice@example.com"))
    ask(request("08", user="Inspect alice@example.com"))
    assert len(calls) == 2 and len(judges) == 1
    example = cache_misses(1)[0]
    candidate = example["candidates"][0]
    assert candidate["reason"] == "verifier_rejected"
    assert candidate["high_similarity"]
    assert candidate["differences"][0]["path"] == ["messages", 0, "content"]
    assert "*" in candidate["differences"][0]["before"]
    assert " " in candidate["differences"][0]["before"]
    assert candidate["checks"][-1]["verifier_reason"] == (
        "alic*@*******.*** ***** **** *tate"
    )
    assert "alice@example.com" not in json.dumps(cache_misses())
    assert "alice@example.com" not in path.read_text()
    assert "September" not in path.read_text()
    assert "verifier_rejected" in path.read_text()
    assert capsys.readouterr() == ("", "")
    assert root.handlers == handlers and root.level == level
    stats = cache_stats()
    assert (stats["requests"], stats["miss"], stats["hit"]) == (3, 2, 1)
    assert stats["near_misses"] == stats["misses_with_candidates"] == 1
    assert stats["candidate_rejections"] == {"verifier_rejected": 1}
    assert stats["miss_prompt"]["count"] == 2
    assert (
        stats["miss_prompt"]["chars"]["mean"]
        == prompt_stats(request("07", user="Inspect alice@example.com"))[
            "chars"
        ]
    )
    example["prompt"]["chars"] = -1
    assert cache_misses(1)[0]["prompt"]["chars"] > 0


@pytest.mark.parametrize(
    "case,expected",
    [
        ("validator", "validator_rejected"),
        ("validator_error", "validator_error"),
        ("verifier_error", "verifier_error"),
        ("invalid", "invalid_verdict"),
        ("references", "output_references_changed_value"),
        ("proportion", "pair_instruction_or_control_changed"),
        ("structure", "pair_instruction_or_control_changed"),
        ("refresh", "refresh"),
    ],
)
def test_candidate_rejection_reasons(tmp_path, case, expected):
    def fail(*args):
        raise ValueError("secret@example.com")

    options = {"mode": "testing", "path": tmp_path / "cache.db"}
    options["verifier_overrider"] = (
        fail if case == "verifier_error" else lambda _: {}
    )
    if case.startswith("validator"):
        options["validator"] = (
            fail if case == "validator_error" else lambda *_: False
        )

    @cached_llm_response(**options)
    def ask(body):
        return "The day is 07." if case == "references" else "inspect"

    before, after = request("07"), request("08")
    if case == "proportion":
        before, after = (
            request("07", user="1"),
            request("07", user="99999999999999999999"),
        )
    if case == "structure":
        after["messages"][0]["content"] += "\nextra line"
    ask(before)
    if case == "refresh":
        with get_store(str(options["path"])).connect() as db:
            db.execute("UPDATE entries SET created=0")
        after = before
    if case.startswith("validator"):
        after = before
    ask(after)
    candidate = cache_misses(1)[0]["candidates"][0]
    assert candidate["reason"] == expected
    assert "secret@example.com" not in json.dumps(cache_misses())


def test_similarity_does_not_enable_reuse_and_respects_scope(tmp_path):
    calls = []

    def upstream(body):
        calls.append(body)
        return "fresh"

    options = {"mode": "conservative", "path": tmp_path / "cache.db"}
    ask = cached_llm_response(**options)(upstream)
    ask(request("07"))
    ask(request("08"))
    assert len(calls) == 2
    assert cache_misses(1)[0]["near_miss"]
    assert (
        cache_misses(1)[0]["candidates"][0]["reason"] == "verification_disabled"
    )
    different_model = {**request("09"), "model": "other"}
    ask(different_model)
    assert cache_misses(1)[0]["candidates"] == []
    other = cached_llm_response(**options, namespace="other")(upstream)
    other(request("09"))
    assert cache_misses(1)[0]["candidates"] == []


def test_short_input_stats_and_disabled_diagnostics(tmp_path):
    @cached_llm_response(mode="conservative", path=tmp_path / "never.db")
    def ask(body):
        return "fresh"

    ask("Hi, 世界!🙂 12\n")
    stats = cache_stats()
    assert stats["miss_reasons"] == {"short_input": 1}
    assert stats["miss_prompt"]["chars"]["total"] == 12
    assert stats["miss_prompt"]["symbols"]["total"] == 3
    assert stats["miss_prompt"]["digits"]["total"] == 2
    assert stats["miss_prompt"]["bytes"]["total"] == 19
    assert not (tmp_path / "never.db").exists()
    ask("bypassed", use_cache=False)
    assert cache_stats()["requests"] == 1
    disabled = cached_llm_response(
        mode="testing", diagnostics=False, min_words=0, path=tmp_path / "off.db"
    )(lambda body: body)
    disabled("text")
    assert len(cache_misses(20)) == 1
    assert cache_stats()["requests"] == 2


def test_rebinding_miss_explains_changed_identifier_relationships(tmp_path):
    @cached_llm_response(mode="testing", path=tmp_path / "references.db")
    def ask(body):
        return "inspect"

    old = request("07", user="Inspect job_ab12cd34 and job_ab12cd34")
    new = request("07", user="Inspect job_ef56ab78 and job_0123abcd")
    ask(old)
    ask(new)
    check = cache_misses(1)[0]["candidates"][0]["checks"][0]
    assert check == {
        "reason": "rebind_failed",
        "error": "ValueError",
        "previous_reference_count": 1,
        "current_reference_count": 2,
        "reference_structure_matches": False,
    }


def test_examples_bounded_and_raw_text_is_explicit(tmp_path):
    @cached_llm_response(
        mode="conservative",
        min_words=0,
        path=tmp_path / "cache.db",
        diagnostic_text=True,
    )
    def ask(body):
        return "fresh"

    for i in range(23):
        ask(f"Inspect person{i}@example.com")
    examples = cache_misses(100)
    assert len(examples) == 20
    assert len(examples[0]["candidates"]) == 8
    assert "@example.com" in json.dumps(examples)
    assert cache_misses(0) == []
    changes, truncated = differences(
        {"alice@example.com": "private"}, {"alice@example.com": "other"}
    )
    assert "alice" not in json.dumps(changes) and not truncated
    assert differences(list(range(20)), list(range(1, 21)))[1]


def test_logging_opt_in_disable_and_external_handlers(tmp_path, capsys):
    logger = logging.getLogger("cached_response")
    external = logging.NullHandler()
    logger.addHandler(external)
    try:
        configure_logging(console=True)
        logger.info("package enabled")
        assert "package enabled" in capsys.readouterr().err
        configure_logging(enabled=False)
        logger.warning("package disabled")
        assert capsys.readouterr() == ("", "")
        assert external in logger.handlers
    finally:
        logger.removeHandler(external)


@pytest.mark.parametrize("threshold", [-1, 2, float("nan")])
def test_invalid_threshold(threshold):
    with pytest.raises(ValueError, match="near_miss_threshold"):
        Config(near_miss_threshold=threshold)


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "abcd-Alice 1234@example.com-wxyz",
            "abcd-***** ****@*******.***-wxyz",
        ),
        ("ABCD-é中٢Ⅳ +🙂-WXYZ", "ABCD-**** +🙂-WXYZ"),
        ("abcd\tsecret\n1234-wxyz", "abcd\t******\n****-wxyz"),
        ("", ""),
        ("abc", "abc"),
        ("abcdefgh", "abcdefgh"),
        ("abcd9wxyz", "abcd*wxyz"),
        ("ABCD" + "a" * 200 + "WXYZ", "ABCD" + "*" * 152 + "WXYZ"),
    ],
)
def test_redaction_preserves_edges_and_symbols(text, expected):
    assert redact(text) == expected
