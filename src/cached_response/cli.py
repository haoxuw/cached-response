"""Inspect local cache size without displaying prompts or responses."""

import argparse
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from .config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(settings().path))
    skill = parser.add_mutually_exclusive_group()
    skill.add_argument(
        "--skill",
        action="store_true",
        help="Read the bundled configuration skill",
    )
    skill.add_argument(
        "--install-skill",
        metavar="DIR",
        help="Copy the skill into DIR without overwriting",
    )
    parser.add_argument(
        "--signatures",
        action="store_true",
        help="Show the 20 most frequent persistent signature counters",
    )
    args = parser.parse_args()
    if args.skill or args.install_skill:
        name = "configure-cached-response"
        text = (
            files("cached_response")
            .joinpath("skills", name, "SKILL.md")
            .read_text(encoding="utf-8")
        )
        if args.skill:
            print(text)
        else:
            destination = (
                Path(args.install_skill).expanduser() / name / "SKILL.md"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as output:
                output.write(text)
            print(destination)
        return
    with sqlite3.connect(f"file:{args.path}?mode=ro", uri=True) as db:
        entries = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM entries"
        ).fetchone()
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
                **(
                    {"signatures": signatures} if signatures is not None else {}
                ),
            }
        )
    )
