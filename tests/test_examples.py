"""The two minimal examples demonstrate a miss followed by a cache hit."""

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "minimal"


@pytest.mark.parametrize("example", ["exact.py", "llm.py"])
def test_minimal_decorator_example(example, tmp_path):
    environment = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "CACHED_RESPONSE_MODE": "disabled",
    }
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / example)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.count("executed") == 1
    assert result.stderr == ""


def test_miss_diagnostics_demo():
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "miss_diagnostics.py")],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    output = json.loads(result.stdout)
    assert output["stats"]["requests"] == 3
    assert output["stats"]["near_misses"] == 1
    assert output["miss_example"]["candidate_reason"] == "verifier_rejected"
    assert "alice" not in result.stdout
    assert result.stderr == ""
