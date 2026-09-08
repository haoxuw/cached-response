"""Experimental candidate retrieval. Signatures never authorize response reuse."""

import hashlib
import re
from difflib import SequenceMatcher
from functools import lru_cache

from .normalize import dumps

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
DISTANCE_CHARS = 8192
VERSION = "1"


def mask(text):
    return "".join("*" if char.isalnum() else char for char in text)


@lru_cache(maxsize=1)
def vocabulary():
    # Never download data or load NLTK on ordinary package imports/lookups.
    try:
        from nltk.corpus import words

        values = frozenset(word.casefold() for word in words.words())
    except (ImportError, LookupError) as exc:
        raise ValueError(
            "Signature matching requires cached-response[signatures] and the "
            "NLTK words corpus; run python -m nltk.downloader words first"
        ) from exc
    revision = hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()
    return values, revision


def fingerprints(body):
    text = dumps(body)
    words, revision = vocabulary()
    lexical = TOKEN.sub(
        lambda match: (
            match.group()
            if match.group().isalpha() and match.group().casefold() in words
            else mask(match.group())
        ),
        text,
    )
    return {
        "masked": hashlib.sha256(mask(text).encode()).hexdigest(),
        "lexical:" + VERSION + ":" + revision: hashlib.sha256(
            lexical.encode()
        ).hexdigest(),
    }


def distance(before, after):
    """Rank structured pairs by changed leaves; bounded text edit estimates.

    Sampling affects ranking only, never the full-input checks or verification.
    Equal large boilerplate leaves do not drown differences in a small leaf.
    """
    scores = []

    def walk(left, right):
        if type(left) is not type(right):
            scores.append(1.0)
        elif isinstance(left, dict) and left.keys() == right.keys():
            for key in left:
                walk(left[key], right[key])
        elif isinstance(left, list) and len(left) == len(right):
            for a, b in zip(left, right):
                walk(a, b)
        elif left != right:
            if isinstance(left, str):
                half = DISTANCE_CHARS // 2

                def sample(value):
                    return (
                        value[:half] + value[-half:]
                        if len(value) > DISTANCE_CHARS
                        else value
                    )

                ratio = SequenceMatcher(
                    None, sample(left), sample(right), autojunk=True
                ).ratio()
                scores.append(max(1 / max(len(left), len(right), 1), 1 - ratio))
            else:
                scores.append(1.0)

    walk(before, after)
    return sum(scores)
