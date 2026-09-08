import copy
import json

import pytest

from cached_response import cache_misses, cached_llm_response
from cached_response.matching import reference_view
from cached_response.normalize import normalize
from cached_response.matching import PairRejected, prepare
from cached_response.storage import get_store


def request(target='job_ab12cd34', note='No trace.', **data):
    return {'model': 'example', 'messages': [
        {'role': 'system', 'content': 'Use the tool result to inspect the requested resource.'},
        {'role': 'user', 'content': f'Inspect {target}.'},
        {'role': 'tool', 'content': json.dumps({'target': target, 'note': note, **data})},
    ]}


def prepared(before, after, result):
    old, old_bindings = reference_view(normalize(before, 'testing'))
    new, new_bindings = reference_view(normalize(after, 'testing'))
    return prepare(old, new, old_bindings, new_bindings, result)


@pytest.mark.parametrize('mode', ['testing', 'risky'])
@pytest.mark.parametrize('diagnostics', [True, False])
def test_extra_reference_requires_pair_approval_and_rebinds_response(tmp_path, mode, diagnostics):
    calls, judges = [], []
    def verifier(evidence):
        judges.append(evidence)
        return {'safe_to_reuse': True, 'reason': 'The next action reads the requested resource.'}
    @cached_llm_response(mode=mode, min_words=0, diagnostics=diagnostics,
                         path=tmp_path / 'cache.db', verifier_overrider=verifier)
    def ask(body):
        calls.append(body)
        return {'action': 'inspect', 'target': 'job_ab12cd34'}
    before = request()
    after = request('job_ef56ab78', 'Trace trace_0123abcd was recorded.')
    ask(before)
    assert ask(after) == {'action': 'inspect', 'target': 'job_ef56ab78'}
    assert len(calls) == 1
    assert len(judges) == 1
    assert judges[0]['verification_kind'] == 'input_pair'
    assert judges[0]['old_input'] == before
    assert judges[0]['new_input'] == after
    assert judges[0]['reference_alignment']['mapped_reference_count'] == 1
    assert judges[0]['reference_alignment']['unmapped_current_reference_count'] == 1
    # This approval must not create a shape rule or silently cover another pair.
    with get_store(str(tmp_path / 'cache.db')).connect() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='verified_rules'").fetchone()[0] == 0
    ask(after)
    assert len(judges) == 2
    assert len(calls) == 1


@pytest.mark.parametrize('options', [
    {'mode': 'conservative'}, {'mode': 'disabled'},
    {'mode': 'testing', 'learning': False}, {'mode': 'risky', 'learning': False},
])
def test_pair_review_mode_gate(tmp_path, options):
    calls, judges = [], []
    @cached_llm_response(min_words=0, path=tmp_path / 'cache.db',
                         verifier_overrider=lambda e: judges.append(e) or {'safe_to_reuse': True, 'reason': 'yes'},
                         **options)
    def ask(body):
        calls.append(body)
        return 'inspect'
    ask(request())
    ask(request(note='Trace trace_0123abcd was recorded.'))
    assert len(calls) == 2
    assert not judges


@pytest.mark.parametrize('verdict', [False, 'true', None])
def test_pair_rejection_or_invalid_verdict_runs_upstream(tmp_path, verdict):
    calls, judges = [], []
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path / 'cache.db',
                         verifier_overrider=lambda e: judges.append(e) or {'safe_to_reuse': verdict, 'reason': 'no'})
    def ask(body):
        calls.append(body)
        return 'inspect'
    ask(request())
    ask(request(note='Trace trace_0123abcd was recorded.'))
    assert len(calls) == 2
    assert len(judges) == 1


def test_pair_can_review_changed_multiline_tool_text(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path / 'cache.db',
                         verifier_overrider=lambda e: judges.append(e) or {'safe_to_reuse': True, 'reason': 'read current state'})
    def ask(body):
        calls.append(body)
        return 'read current state'
    ask(request(note='Output follows.'))
    ask(request(note='Output follows.\nDiagnostic line added.'))
    assert len(calls) == 1
    assert judges[0]['verification_kind'] == 'input_pair'


@pytest.mark.parametrize('mutation,reason', [
    ('instruction', 'pair_instruction_or_control_changed'),
    ('state', 'pair_provider_state_changed'),
    ('sequence', 'pair_sequence_changed'),
    ('fields', 'pair_fields_changed'),
    ('roles', 'pair_instruction_or_control_changed'),
])
def test_hard_guards_cannot_be_overridden_by_verifier(mutation, reason):
    before = request(events=[{'status': 'ready'}, {'status': 'running'}])
    after = copy.deepcopy(before)
    if mutation == 'instruction':
        after['messages'][1]['content'] = 'Delete the resource.'
    elif mutation == 'state':
        before['messages'][2]['reasoning_content'] = 'opaque old'
        after['messages'][2]['reasoning_content'] = 'opaque new'
    elif mutation in ('sequence', 'fields'):
        data = json.loads(after['messages'][2]['content'])
        if mutation == 'sequence':
            data['events'].pop()
        else:
            data['another'] = True
        after['messages'][2]['content'] = json.dumps(data)
    else:
        after['messages'][2]['role'] = 'user'
    with pytest.raises(PairRejected) as error:
        prepared(before, after, {'action': 'inspect'})
    assert error.value.reason == reason


def test_ambiguous_or_removed_output_reference_is_rejected():
    before = request(note='Prior trace_0123abcd')
    after = request(note='No trace.')
    with pytest.raises(PairRejected, match='unmapped_output_reference'):
        prepared(before, after, {'target': 'trace_0123abcd'})


def test_removed_reference_embedded_in_output_is_rejected():
    with pytest.raises(PairRejected, match='unmapped_output_reference'):
        prepared(request(note='trace_0123abcd'), request(note='No trace.'),
                 {'target': 'prefix_trace_0123abcd_suffix'})


def test_nested_equal_but_different_types_are_rejected():
    before, after = request(), request()
    before['messages'][0]['control'] = {'limit': 1}
    after['messages'][0]['control'] = {'limit': True}
    with pytest.raises(PairRejected, match='pair_type_changed'):
        prepared(before, after, {'action': 'inspect'})


def test_relationship_splits_cannot_rebind_output():
    before = request(note='Related job_ab12cd34')
    after = request('job_ef56ab78', 'Related job_0123abcd')
    with pytest.raises(PairRejected, match='unmapped_output_reference'):
        prepared(before, after, {'target': 'job_ab12cd34'})


def test_changed_message_sequence_has_explicit_diagnostic(tmp_path):
    judges = []
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path / 'cache.db',
                         verifier_overrider=lambda e: judges.append(e) or {})
    def ask(body):
        return 'inspect'
    before = request()
    after = copy.deepcopy(before)
    after['messages'].append({'role': 'tool', 'content': 'Extra result'})
    ask(before)
    ask(after)
    candidate = cache_misses(1)[0]['candidates'][0]
    assert candidate['reason'] == 'message_role_sequence_changed'
    assert candidate['checks'][0]['previous_message_count'] == 3
    assert candidate['checks'][0]['current_message_count'] == 4
    assert not judges


def test_opaque_signature_in_tool_text_never_reaches_pair_review():
    with pytest.raises(PairRejected, match='pair_provider_state_changed'):
        prepared(request(note='call_x__thought__opaqueOld'),
                 request(note='call_x__thought__opaqueNew'), {'action': 'inspect'})


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('mode', ['testing', 'risky'])
def test_builtin_pair_verifier_uses_existing_function(tmp_path, asynchronous, mode):
    import asyncio
    from cached_response.matching import INSTRUCTION
    calls, judges = [], []
    def model(body):
        if body.get('response_format') == {'type': 'json_object'}:
            judges.append(body)
            return {'safe_to_reuse': True, 'reason': 'Test verdict'}
        calls.append(body)
        return 'inspect current state'
    async def async_model(body):
        return model(body)
    ask = cached_llm_response(mode=mode, min_words=0, path=tmp_path / 'cache.db')(
        async_model if asynchronous else model)
    before, after = request(), request(note='Trace trace_0123abcd')
    if asynchronous:
        async def run():
            await ask(before)
            return await ask(after)
        answer = asyncio.run(run())
    else:
        ask(before)
        answer = ask(after)
    assert answer == 'inspect current state'
    assert len(calls) == len(judges) == 1
    assert judges[0]['messages'][0]['content'] == INSTRUCTION
    evidence = json.loads(judges[0]['messages'][1]['content'])
    assert evidence['old_input'] == before
    assert evidence['new_input'] == after
    assert evidence['proposed_changes'] == []


def test_validator_rejection_blocks_pair_review(tmp_path):
    calls, judges = [], []
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path / 'cache.db',
                         validator=lambda old, new: False,
                         verifier_overrider=lambda e: judges.append(e))
    def ask(body):
        calls.append(body)
        return 'inspect'
    ask(request())
    ask(request(note='Trace trace_0123abcd'))
    assert len(calls) == 2
    assert not judges


def test_pair_review_is_bounded_and_does_not_log_exception_text(tmp_path, monkeypatch):
    from cached_response import matching
    calls, judges = [], []
    def verifier(e):
        judges.append(e)
        raise RuntimeError('private-person@example.com')
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path / 'cache.db',
                         verifier_overrider=verifier)
    def ask(body):
        calls.append(body)
        return 'inspect'
    ask(request())
    ask(request(note='Trace trace_0123abcd'))
    assert len(judges) == 1
    assert 'private-person@example.com' not in json.dumps(cache_misses(1))
    monkeypatch.setattr(matching, 'MAX_JUDGE_CHARS', 1)
    ask(request(note='Different trace trace_5678abcd detail.'))
    assert len(judges) == 1
    assert len(calls) == 3
    assert any(c['reason'] == 'verifier_input_too_large' for c in cache_misses(1)[0]['candidates'])


def test_http_stream_pair_reuse_preserves_request_and_output_ids(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient
    calls, judges = [], []
    app = FastAPI()

    async def complete(request):
        body = await request.json()
        if body.get('response_format') == {'type': 'json_object'}:
            judges.append(body)
            return {'safe_to_reuse': True, 'reason': 'Test verdict'}
        calls.append(body)
        async def chunks():
            event = {'choices': [{'index': 0, 'delta': {'content': 'Inspect job_ab12cd34'}, 'finish_reason': 'stop'}]}
            yield 'data: ' + json.dumps(event) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(chunks(), media_type='text/event-stream')

    complete.__annotations__['request'] = Request
    app.post('/v1/chat/completions')(cached_llm_response(
        mode='testing', min_words=0, path=tmp_path / 'cache.db')(complete))
    before, after = request(), request('job_ef56ab78', 'Trace trace_0123abcd')
    before['stream'] = after['stream'] = True
    with TestClient(app) as client:
        assert client.post('/v1/chat/completions', json=before).status_code == 200
        response = client.post('/v1/chat/completions', json=after)
    assert response.status_code == 200
    assert 'Inspect job_ef56ab78' in response.text
    assert 'job_ab12cd34' not in response.text
    assert response.text.endswith('data: [DONE]\n\n')
    assert len(calls) == len(judges) == 1
    evidence = json.loads(judges[0]['messages'][1]['content'])
    assert evidence['new_input'] == after
    assert evidence['verification_kind'] == 'input_pair'
