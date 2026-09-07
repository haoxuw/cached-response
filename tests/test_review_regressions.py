"""Regressions discovered while reviewing the package for public release."""

import subprocess
import sys

from cached_response import cached_llm_response, cached_staticmethod

UUID_A = "f3c07f44-685d-493e-bf14-8ac1f134191f"
UUID_B = "952056da-bf56-45e7-9107-c856207a4b80"


def test_disabled_function_can_have_unresolved_annotations():
    @cached_llm_response(mode="disabled")
    def echo(value: "OnlyAvailableToTypeChecker"):
        return value

    assert echo(3) == 3


def test_dictionary_key_references_cause_a_miss(tmp_path):
    calls = []

    @cached_llm_response(
        mode="conservative", path=tmp_path / "cache.db", min_words=0
    )
    def answer(identifier):
        calls.append(identifier)
        return {identifier: "ok"}

    assert answer(UUID_A) == {UUID_A: "ok"}
    assert answer(UUID_B) == {UUID_B: "ok"}
    assert len(calls) == 2


def test_every_output_reference_must_be_rewritable(tmp_path):
    calls = []

    @cached_llm_response(
        mode="conservative", path=tmp_path / "cache.db", min_words=0
    )
    def answer(identifier):
        calls.append(identifier)
        return f"{identifier} prefix_{identifier}"

    answer(UUID_A)
    assert answer(UUID_B) == f"{UUID_B} prefix_{UUID_B}"
    assert len(calls) == 2


def test_import_and_reuse_without_unix_uid(tmp_path):
    script = """
import os
import sys
if hasattr(os, "getuid"):
    del os.getuid
from cached_response import cached_staticmethod
from cached_response.storage import Store
@cached_staticmethod(path=sys.argv[1], report=False)
def echo(value):
    return value
assert echo(3) == echo(3) == 3
Store(sys.argv[1])  # Also open an existing database without a Unix UID.
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "cache.db")], check=True
    )


def test_http_query_and_dependency_arguments_are_part_of_key(tmp_path):
    import asyncio
    from starlette.requests import Request

    calls = []

    @cached_llm_response(
        mode="testing", path=tmp_path / "cache.db", min_words=0
    )
    async def answer(request: Request, model="a"):
        calls.append(model)
        return {"model": model, "query": request.url.query}

    def request(query):
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/chat",
                "query_string": query,
                "headers": [],
            },
            receive,
        )

    async def run():
        assert await answer(request(b"x=1"), model="a") == {
            "model": "a",
            "query": "x=1",
        }
        assert await answer(request(b"x=1"), model="b") == {
            "model": "b",
            "query": "x=1",
        }
        assert await answer(request(b"x=2"), model="b") == {
            "model": "b",
            "query": "x=2",
        }
        assert await answer(request(b"x=2"), model="b") == {
            "model": "b",
            "query": "x=2",
        }

    asyncio.run(run())
    assert calls == ["a", "b", "b"]


def test_cancelled_lookup_releases_ticket_when_worker_finishes(
    tmp_path, monkeypatch
):
    import asyncio
    import threading
    from cached_response.storage import Store

    claimed = threading.Event()
    resume = threading.Event()
    released = threading.Event()
    original_claim, original_release = Store.claim, Store.release

    def claim(store, *args):
        result = original_claim(store, *args)
        if result:
            claimed.set()
            assert resume.wait(5)
        return result

    def release(store, *args):
        original_release(store, *args)
        released.set()

    monkeypatch.setattr(Store, "claim", claim)
    monkeypatch.setattr(Store, "release", release)
    path = tmp_path / "cancel.db"
    calls = []

    @cached_staticmethod(path=path, report=False)
    async def answer(value):
        calls.append(value)
        return value

    async def run():
        task = asyncio.create_task(answer(1))
        assert await asyncio.to_thread(claimed.wait, 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            resume.set()
        assert await asyncio.to_thread(released.wait, 2)

    asyncio.run(run())
    assert not calls
    with Store(path).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
