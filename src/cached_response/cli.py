"""Inspect local cache size without displaying prompts or responses."""

import argparse
import json
import sqlite3

from .config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(settings().path))
    parser.add_argument(
        "--signatures",
        action="store_true",
        help="Show the 20 most frequent persistent signature counters",
    )
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.path}?mode=ro", uri=True) as db:
        entries = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM entries"
        ).fetchone()
        rules = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        signatures = None
        if args.signatures:
            cursor = db.execute(
                "SELECT * FROM signature_counts ORDER BY requests DESC, scope, kind, signature LIMIT 20"
            )
            columns = [column[0] for column in cursor.description]
            signatures = [dict(zip(columns, row)) for row in cursor.fetchall()]
    print(
        json.dumps(
            {
                "entries": entries[0],
                "payload_bytes": entries[1],
                "learned_rules": rules,
                **(
                    {"signatures": signatures} if signatures is not None else {}
                ),
            }
        )
    )
