"""The two minimal examples demonstrate a miss followed by a cache hit."""

import os
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
    assert "miss (cold)" in result.stderr
    assert "hit (exact)" in result.stderr
    assert "1/2 cached (50.0%)" in result.stderr
