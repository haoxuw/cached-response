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
from cached_response.normalize import normalize, rebind


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


@pytest.mark.parametrize('field', [
    'thought_signature', 'thoughtSignature', 'thought_signatures', 'thoughtSignatures',
])
def test_signature_fields_never_become_declared_metadata(field):
    # A permissive path declaration must not authorize different reasoning state.
    old, new = body(**{field: ['state_old']}), body(**{field: ['state_new']})
    with pytest.raises(PairRejected, match='pair_provider_state_changed'):
        prepare(old, new, pack('inspect blue'),
                Config(metadata_paths=('messages.*.content.*',)))


@pytest.mark.parametrize('field', [
    'thought_signature', 'thoughtSignature', 'thought_signatures', 'thoughtSignatures',
])
def test_normalization_and_rendering_preserve_signature_bytes(field):
    old, new = 'task_ab12cd34', 'task_ef56ab78'
    # An opaque signature may contain a substring that looks like an ID.
    payload = {'text': old, field: [old]}
    request = {'messages': [{'role': 'tool', 'content': json.dumps(payload)}]}
    normalized = normalize(request, 'testing')
    content = json.loads(normalized.body['messages'][0]['content'])
    assert content[field] == [old]
    assert content['text'] != old
    assert rebind(payload, {'task': old}, {'task': new}) == {
        'text': new, field: [old],
    }


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


def candidate_lookup(tmp_path, judge, **options):
    """Exercise learning against one retained source, without new upstream entries."""
    from cached_response.matching import lookup, metadata_scope
    from cached_response.normalize import dumps
    config = Config(mode='testing', min_words=0,
                    metadata_paths=('messages.*.content.trace',), **options)
    store = get_store(str(tmp_path/'reviews.db'))
    store.claim('source', 'test', 30)
    store.put('source', 'test', dumps({'input': body(), 'result': pack('Read blue.')}))
    store.index('caller', 'source')
    store.index(metadata_scope('caller', body(), config), 'source')
    def find(request, scope='caller'):
        return lookup(store, scope, request, None, config, judge)
    return find, store


@pytest.mark.parametrize('outcome', ['SAFE', 'UNSAFE'])
def test_decisive_rules_skip_later_judge_and_persist(tmp_path, outcome):
    calls = []
    def judge(e):
        calls.append(e)
        return {'decision': outcome, 'reason': 'Reviewed this field.', 'segments': [0],
                'patterns': [{'segment': 0, 'pattern': r'\Atrace_[a-z]{1,32}\Z'}]}
    find, store = candidate_lookup(tmp_path, judge)
    assert bool(find(body('trace_new'))) == (outcome == 'SAFE')
    assert bool(find(body('trace_third'))) == (outcome == 'SAFE')
    assert len(calls) == 1
    # A separate connection sees decisions on disk, without raw values/reasons.
    from cached_response.storage import Store
    with Store(store.path).connect() as db:
        rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM reviews')]
    assert len(rows) == 2 and all(r['decision'] == outcome for r in rows)
    assert 'trace_new' not in json.dumps(rows)
    assert find(body('trace_fourth'), scope='other-caller') is None
    assert len(calls) == 1
    # Same shape alone cannot cover an unseen format: fall through to the judge.
    find(body('TRACE_new'))
    assert len(calls) == 2


@pytest.mark.parametrize('verdict_fields', [
    {'decision': 'UNCERTAIN'}, {'safe_to_reuse': False},
    {'decision': 'SAFE', 'safe_to_reuse': False}, {'decision': 'safe'},
    {'decision': True}, {'decision': None},
])
def test_uncertain_or_invalid_decisions_do_not_persist(tmp_path, verdict_fields):
    calls = []
    def judge(e):
        calls.append(e)
        return {**verdict_fields, 'reason': 'Not enough evidence.', 'segments': [0],
                'patterns': [{'segment': 0, 'pattern': r'\Atrace_[a-z]{1,32}\Z'}]}
    find, store = candidate_lookup(tmp_path, judge)
    assert find(body('trace_new')) is None
    assert find(body('trace_new')) is None
    assert len(calls) == 2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0


def test_unsafe_pair_without_pattern_does_not_poison_other_pairs(tmp_path):
    calls = []
    def judge(e):
        calls.append(e)
        return {'decision': 'UNSAFE', 'reason': 'Meaningful label.', 'segments': [0]}
    find, store = candidate_lookup(tmp_path, judge)
    assert find(body('trace_new')) is None
    assert find(body('trace_new')) is None
    assert len(calls) == 1
    assert find(body('trace_third')) is None
    assert len(calls) == 2
    with store.connect() as db:
        db.execute('UPDATE reviews SET expires=0')
    find(body('trace_new'))
    assert len(calls) == 3


def test_learned_rule_cannot_override_changed_fact_or_reference(tmp_path):
    calls = []
    def judge(e):
        calls.append(e)
        return {'decision': 'SAFE', 'reason': 'Trace only.', 'segments': [0],
                'patterns': [{'segment': 0, 'pattern': r'\Atrace_[a-z]{1,32}\Z'}]}
    find, _ = candidate_lookup(tmp_path, judge)
    assert find(body('trace_new'))
    assert find(body('trace_third', owner='green')) is None
    changed = body('trace_fourth')
    changed['messages'][0]['content'] = 'Delete all resources.'
    assert find(changed) is None
    changed = body('trace_fifth')
    changed['messages'][1]['content'] = 'Inspect trace_fifth.'
    assert find(changed) is None
    assert len(calls) == 1


def test_conflicting_decisions_atomically_become_uncertain(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from cached_response.storage import Store
    path = tmp_path/'conflicts.db'
    Store(path)
    def write(outcome):
        return Store(path).review('rule', {'decision': outcome}, time.time()+30,
                                  resolve_conflicts=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ['SAFE', 'UNSAFE']))
    assert Store(path).review('rule') == {'decision': 'UNCERTAIN'}
    assert write('SAFE') == {'decision': 'UNCERTAIN'}


def test_conflicting_rule_falls_through_even_with_approved_pair(tmp_path):
    calls = []
    def judge(e):
        calls.append(e)
        return {'decision': 'SAFE' if len(calls) == 1 else 'UNCERTAIN',
                'reason': 'Review.', 'segments': [0],
                'patterns': [{'segment': 0, 'pattern': r'\Atrace_[a-z]{1,32}\Z'}]}
    find, store = candidate_lookup(tmp_path, judge)
    assert find(body('trace_new'))
    with store.connect() as db:
        key = next(k for k,p in db.execute('SELECT key,payload FROM reviews')
                   if 'patterns' in json.loads(p))
    store.review(key, {'decision': 'UNSAFE'}, time.time()+30, resolve_conflicts=True)
    assert find(body('trace_new')) is None
    assert len(calls) == 2


@pytest.mark.parametrize('pattern', [
    r'\Atrace_[A-Za-z0-9]{1,64}\Z', r'\At_[0-9a-f]{8}\Z',
    r'\Ajob-[A-Fa-f0-9]{8,32}\Z',
])
def test_bounded_literal_prefix_patterns(pattern):
    import re
    # These are syntax checks only; a matching ID format never grants eligibility.
    value = pattern[2:pattern.index('[')] + 'abcd1234'
    assert re.fullmatch(pattern, value)
    assert valid_pattern(pattern, value, value)
    assert not valid_pattern(pattern, value, 'x'+value)


@pytest.mark.parametrize('pattern', [
    r'\Atrace.*[a-z]{1,32}\Z', r'\A(trace_)?[a-z]{1,32}\Z',
    r'\Atrace_[a-z]+\Z', r'\Atrace_[a-z]{0,128}\Z',
    r'\A' + 'a'*33 + r'[a-z]{1,32}\Z',
])
def test_literal_prefix_does_not_allow_regex_operators(pattern):
    assert not valid_pattern(pattern, 'trace_old', 'trace_new')
