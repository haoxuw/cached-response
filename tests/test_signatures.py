import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cached_response import cache_signatures, cached_llm_response
from cached_response import signatures
from cached_response.normalize import dumps
from cached_response.storage import Store


@pytest.fixture(autouse=True)
def known_words(monkeypatch):
    monkeypatch.setattr(signatures, 'vocabulary', lambda: (frozenset({'ready', 'error', 'inspect', 'the', 'resource'}), 'test'))


def request(note):
    return {'model': 'example', 'messages': [
        {'role': 'user', 'content': 'Inspect the resource.'},
        {'role': 'tool', 'content': json.dumps({'note': note})},
    ]}


def test_mask_preserves_symbols_whitespace_and_unicode_lengths():
    text = '01-acxan\n02-aadfsa é９_+ ☺'
    assert signatures.mask(text) == '**-*****\n**-****** **_+ ☺'
    left, right = signatures.fingerprints(request('ready')), signatures.fingerprints(request('error'))
    assert left['masked'] == right['masked']
    assert left['lexical:1:test'] != right['lexical:1:test']
    assert left['masked'] == hashlib.sha256(signatures.mask(dumps(request('ready'))).encode()).hexdigest()


def test_signature_counts_persist_and_updates_are_atomic(tmp_path):
    path = tmp_path / 'cache.db'
    first, second = Store(path), Store(path)
    prints = signatures.fingerprints(request('ready'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: (first if i % 2 else second).count_signatures('scope', prints, i % 2 == 0), range(100)))
    rows = Store(path).signature_stats()
    assert len(rows) == 2
    assert all((r['requests'], r['hits'], r['misses']) == (100, 50, 50) for r in rows)
    assert all(r['first_seen'] <= r['last_seen'] for r in rows)
    assert 'ready' not in json.dumps(rows)


def test_same_signature_does_not_authorize_reuse(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode='testing', min_words=0, signature_matching=True,
                         path=tmp_path / 'cache.db', verifier_overrider=lambda e: judges.append(e) or {'safe_to_reuse': False, 'reason': 'state changed'})
    def ask(body):
        calls.append(body)
        return 'ready'
    ask(request('ready'))
    ask(request('error'))
    assert len(calls) == 2
    assert not judges
    assert any(r['kind'] == 'masked' and r['requests'] == r['misses'] == 2 for r in cache_signatures(path=tmp_path / 'cache.db'))


@pytest.mark.parametrize('mode', ['testing', 'risky'])
def test_signature_retrieval_recovers_older_candidate(tmp_path, mode):
    calls, judges = [], []
    enabled = False
    def verifier(evidence):
        judges.append(evidence)
        return {'safe_to_reuse': enabled, 'segments': [0], 'reason': 'specific test fixture'}
    @cached_llm_response(mode=mode, min_words=0, signature_matching=True, metadata_paths=('messages.*.content.note',),
                         path=tmp_path / 'cache.db', verifier_overrider=verifier)
    def ask(body):
        calls.append(body)
        return 'inspect'
    ask(request('ready ' + 'x' * 300))
    for i in range(10):
        ask(request('error ' + 'y' * (310 + i)))
    enabled = True
    judges.clear()
    assert ask(request('ready ' + 'z' * 300)) == 'inspect'
    assert len(calls) == 11
    assert len(judges) == 1
    assert 'ready ' in judges[0]['old_input']['messages'][1]['content']


def test_retrieval_keeps_scope_isolation(tmp_path):
    store = Store(tmp_path / 'cache.db')
    prints = signatures.fingerprints(request('ready'))
    store.claim('one', 'owner', 60)
    store.put('one', 'owner', dumps({'input': request('ready'), 'result': 'answer'}))
    store.index_signatures('tenant-one', prints, 'one')
    assert not store.signature_candidates('tenant-two', prints, 8)


def test_distance_uses_actual_values_and_nested_types():
    assert signatures.distance({'a': 'ready'}, {'a': 'error'}) > 0
    assert signatures.distance({'a': 1}, {'a': True}) > 0
    assert signatures.distance({'a': 'ready'}, {'a': 'ready'}) == 0
