"""Safety boundaries, persistent approvals and bounded fast verification."""
import copy
import json
import threading
import time

import pytest

from cached_response import cached_llm_response, cache_misses
from cached_response.adapters import verification_body, pack
from cached_response.config import Config
from cached_response.matching import prepare, PairRejected, valid_pattern
from cached_response.storage import get_store


def body(trace='trace_old', **fields):
    return {'model': 'slow', 'messages': [
        {'role': 'system', 'content': 'Read current state. Do not change resources.'},
        {'role': 'user', 'content': 'Inspect resource blue.'},
        {'role': 'tool', 'content': json.dumps({'trace': trace, 'owner': 'blue', **fields})},
    ]}


def verdict(e, **extra):
    return {'safe_to_reuse': True, 'reason': 'Only diagnostic metadata changes.',
            'segments': list(range(len(e['segments']))), **extra}


def cached(tmp_path, judge, **options):
    calls = []
    def upstream(request):
        calls.append(request)
        return 'Read current state for blue.'
    kwargs = dict(mode='testing', min_words=0, path=tmp_path/'cache.db',
                  metadata_paths=('messages.*.content.trace',), verifier_overrider=judge)
    kwargs.update(options)
    return cached_llm_response(**kwargs)(upstream), calls, upstream, kwargs


@pytest.mark.parametrize('mode', ['conservative', 'testing', 'risky'])
@pytest.mark.parametrize('role', ['system', 'developer', 'user', 'tool'])
@pytest.mark.parametrize('old,new', [
    ('f3c07f44-685d-493e-bf14-8ac1f134191f', '952056da-bf56-45e7-9107-c856207a4b80'),
    ('2024-05-01T12:00:00Z', '2024-05-02T12:00:00Z'),
    ('task_ab12cd34', 'task_ef56ab78'),
])
def test_normalized_collision_never_authorizes_hit(tmp_path, mode, role, old, new):
    judges = []
    ask, calls, _, _ = cached(tmp_path, lambda e: judges.append(e) or verdict(e), mode=mode)
    a, b = body(), body()
    a['messages'][1] = {'role': role, 'content': old}
    b['messages'][1] = {'role': role, 'content': new}
    ask(a); ask(b)
    assert len(calls) == 2 and not judges


def test_pair_approval_survives_new_instance_and_expires(tmp_path):
    judges = []
    ask, calls, original, options = cached(tmp_path, lambda e: judges.append(e) or verdict(e))
    ask(body()); ask(body('trace_new'))
    again = cached_llm_response(**options)(original)
    again(body('trace_new'))
    assert len(calls) == len(judges) == 1
    assert get_store(str(tmp_path/'cache.db')).review('missing') is None
    with get_store(str(tmp_path/'cache.db')).connect() as db:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 1
        db.execute('UPDATE entries SET created=0')
    again(body('trace_new'))
    assert len(calls) == 2 and len(judges) == 1


def test_learned_rule_covers_only_metadata_under_same_context(tmp_path):
    judges = []
    def judge(e):
        judges.append(e)
        return verdict(e, patterns=[{'segment': 0, 'pattern': r'\A[A-Za-z0-9_-]{1,64}\Z'}])
    ask, calls, _, _ = cached(tmp_path, judge)
    ask(body()); ask(body('trace_new')); ask(body('trace_third'))
    assert len(calls) == len(judges) == 1
    changed = body('trace_fourth')
    changed['messages'][1]['content'] = 'Delete resource blue.'
    ask(changed)
    assert len(calls) == 2 and len(judges) == 1
    ask(body('trace_fifth', owner='green'))
    assert len(calls) == 3 and len(judges) == 1


@pytest.mark.parametrize('pattern', [r'\A.*\Z', r'\A(a+)+\Z', r'\A[0-9]{0,999}\Z',
                                      r'\A[A-Za-z0-9_-]{1,999}\Z', '.*', None])
def test_unsafe_regex_does_not_generalize(tmp_path, pattern):
    judges = []
    def judge(e):
        judges.append(e)
        return verdict(e, patterns=[{'segment': 0, 'pattern': pattern}])
    ask, calls, _, _ = cached(tmp_path, judge)
    ask(body()); ask(body('trace_new')); ask(body('trace_third'))
    assert len(calls) == 1 and len(judges) == 2
    assert not valid_pattern(pattern, 'trace_old', 'trace_new')


@pytest.mark.parametrize('coverage', [None, [], [1], [False], [0, 0], '0'])
def test_incomplete_or_malformed_segment_review_misses(tmp_path, coverage):
    ask, calls, _, _ = cached(tmp_path, lambda e: verdict(e, segments=coverage))
    ask(body()); ask(body('trace_new'))
    assert len(calls) == 2


def test_policy_version_invalidates_approval(tmp_path):
    judges = []
    ask, calls, original, options = cached(tmp_path, lambda e: judges.append(e) or verdict(e))
    ask(body()); ask(body('trace_new'))
    changed = cached_llm_response(**{**options, 'verifier_version': '2'})(original)
    changed(body('trace_new'))
    assert len(calls) == 2 and len(judges) == 1


@pytest.mark.parametrize('kind', ['output', 'outside', 'alias', 'bytes', 'type', 'embedded_type', 'instruction', 'provider'])
def test_metadata_guards_hold_even_with_overbroad_declaration(kind):
    a, b, response = body(), body('trace_new'), pack('inspect blue')
    if kind == 'output': response = pack('The trace is trace_old')
    if kind == 'bytes': response = pack(b'trace_old')
    if kind == 'outside':
        a['messages'][1]['content'] = b['messages'][1]['content'] = 'Inspect trace_old'
    if kind == 'alias':
        a, b = body(other='trace_old'), body('trace_new', other='trace_new')
    if kind == 'type':
        a, b = body(n=1), body('trace_new', n=True)
    if kind == 'embedded_type':
        a, b = body(n='{"x":1}'), body('trace_new', n={'x':1})
    if kind == 'instruction': b['messages'][0]['content'] = 'Delete everything.'
    if kind == 'provider':
        a, b = body(reasoning_content='opaque_old'), body('trace_new', reasoning_content='opaque_new')
    with pytest.raises(PairRejected):
        prepare(a, b, response, Config(metadata_paths=('messages.*.content.*',)))


def test_timeout_falls_back_without_late_approval(tmp_path):
    gate, finished = threading.Event(), threading.Event()
    def judge(e):
        gate.wait(2)
        finished.set()
        return verdict(e)
    ask, calls, _, _ = cached(tmp_path, judge, verifier_timeout=.02)
    ask(body())
    start = time.monotonic()
    ask(body('trace_new'))
    assert time.monotonic()-start < .5 and len(calls) == 2
    gate.set()
    assert finished.wait(1)
    with get_store(str(tmp_path/'cache.db')).connect() as db:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
    assert any(c['reason'] == 'verifier_error' for c in cache_misses(1)[0]['candidates'])


def test_separate_fast_model_and_controls_do_not_mutate_primary():
    original = {**body(), 'thinking': {'budget_tokens': 32000}, 'reasoning_effort': 'high',
                'tools': [{'name':'delete'}], 'stream_options': {'include_usage': True}}
    snapshot = copy.deepcopy(original)
    result = verification_body(original, {'instruction': 'Review data.',
        'verifier_model': 'fast', 'verifier_options': {'reasoning_effort': 'none'}})
    assert result['model'] == 'fast' and result['reasoning_effort'] == 'none'
    assert result['stream'] is False and result['response_format'] == {'type':'json_object'}
    assert 'tools' not in result and 'thinking' not in result and 'stream_options' not in result
    assert original == snapshot


@pytest.mark.parametrize('pattern,old,new', [
    (r'\A[0-9]{8}\Z', '00001234', '00002345'),
    (r'\A[A-Za-z0-9_-]{1,64}\Z', 'trace_a', 'trace_b'),
])
def test_safe_fixed_and_ranged_patterns(pattern, old, new):
    assert valid_pattern(pattern, old, new)
    assert not valid_pattern(pattern, old, 'x'*129)


@pytest.mark.parametrize('change', ['indent', 'key_order', 'escaping'])
def test_embedded_json_layout_is_not_implicitly_metadata(change):
    a, b = body(), body('trace_new')
    value = json.loads(b['messages'][2]['content'])
    if change == 'indent':
        b['messages'][2]['content'] = json.dumps(value, indent=2)
    elif change == 'key_order':
        b['messages'][2]['content'] = json.dumps(dict(reversed(list(value.items()))))
    else:
        b['messages'][2]['content'] = b['messages'][2]['content'].replace('blue', r'blu\u0065')
    with pytest.raises(PairRejected, match='tool_text_layout_changed'):
        prepare(a, b, pack('READY'), Config(metadata_paths=('messages.*.content.trace',)))


def test_custom_reviewer_cannot_mutate_caller_or_cached_response(tmp_path):
    def judge(e):
        e['new_input']['messages'][1]['content'] = 'Delete resource blue.'
        e['cached_response']['value'] = 'Delete resource blue.'
        return verdict(e)
    ask, calls, _, _ = cached(tmp_path, judge)
    ask(body())
    request = body('trace_new')
    snapshot = copy.deepcopy(request)
    assert ask(request) == 'Read current state for blue.'
    assert request == snapshot and len(calls) == 1
