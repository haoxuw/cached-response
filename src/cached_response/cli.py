"""Inspect local cache size without displaying prompts or responses."""

import argparse
import json
import sqlite3

from .config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(settings().path))
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.path}?mode=ro", uri=True) as db:
        entries = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM entries"
        ).fetchone()
        rules = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    print(
        json.dumps(
            {
                "entries": entries[0],
                "payload_bytes": entries[1],
                "learned_rules": rules,
            }
        )
    )
