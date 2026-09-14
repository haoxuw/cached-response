"""A degraded alias contract costs cache misses, never the conversation.

These pin the failure shapes measured in a real agent CI run (an eval
suite fronted by an HTTP caching proxy): a conversation entering the
cache mid-way with signed history the cache never recorded, alias
proposals whose scan order shifts between turns, boards outgrowing the
binding limit, and providers echoing the caller's raw IDs.
"""
import copy
import json
import re

import pytest

from cached_response import cache_stats, cached_llm_response
from cached_response.adapters import AliasContractUnsatisfied
from cached_response.adapters import TestAliases as AliasSession
from cached_response.config import Config

SIGNED = 'call_346990__thought__EosRCogRARFNMg+G9bvw4j1yvTBrza0H'
TASK = 't_41dfe03f'
OTHER = 't_217deef9'


def aliases(request):
    seen = []
    for message in request.get('messages') or []:
        for token in re.findall(r'\bt_[0-9a-f]{8}\b', json.dumps(message)):
            if token not in seen:
                seen.append(token)
    if not seen:
        return None
    return seen[0], {t: f'test_id_{i:08d}' for i, t in enumerate(seen)}


def conversation(*, call_id=SIGNED, args=None, task=TASK):
    return {'model': 'test', 'stream': False, 'messages': [
        {'role': 'user', 'content': f'Report the status of {task}.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': call_id, 'type': 'function',
             'function': {'name': 'kanban_show',
                          'arguments': json.dumps(args if args is not None else {'task_id': task})}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'name': 'kanban_show',
         'content': json.dumps({'task': {'id': task, 'status': 'running'}})},
        {'role': 'user', 'content': 'Summarize the status.'},
    ]}


def wrap(tmp_path, upstream):
    return cached_llm_response(mode='testing', min_words=0, path=tmp_path/'cache.db',
                               test_aliases=aliases)(upstream)


def test_unrecorded_signed_call_is_served_live_with_client_bytes(tmp_path):
    # The landmine: history holds a provider-signed call whose arguments
    # name a task ID, and that call was never recorded through this cache.
    calls = []
    def upstream(body):
        calls.append(copy.deepcopy(body))
        return {'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': 'ok'}}]}
    ask = wrap(tmp_path, upstream)
    body = conversation()
    assert ask(body)['choices'][0]['message']['content'] == 'ok'
    assert len(calls) == 1
    forwarded = calls[0]['messages'][1]['tool_calls'][0]['function']
    assert forwarded == body['messages'][1]['tool_calls'][0]['function']
    # Everything outside the signed call is still translated.
    assert 'test_id_00000000' in calls[0]['messages'][0]['content']


def test_first_mint_turn_then_signed_followup_never_raises(tmp_path):
    # Turn 1 mints the task: its reply carries a signed call echoing the
    # caller's raw ID (the reply does not round-trip), so it is served
    # rendered and left uncached. Turn 2 echoes that signed history back.
    served = []
    def upstream(body):
        served.append(copy.deepcopy(body))
        return {'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                             'message': {'role': 'assistant', 'content': '', 'tool_calls': [
                                 {'id': SIGNED, 'type': 'function',
                                  'function': {'name': 'kanban_show',
                                               'arguments': json.dumps({'task_id': TASK})}}]}}]}
    ask = wrap(tmp_path, upstream)
    turn1 = {'model': 'test', 'stream': False,
             'messages': [{'role': 'user', 'content': f'Show {TASK}.'}]}
    reply = ask(turn1)
    assert reply['choices'][0]['message']['tool_calls'][0]['id'] == SIGNED
    assert TASK in reply['choices'][0]['message']['tool_calls'][0]['function']['arguments']
    assert ask(conversation()) is not None
    assert len(served) == 2


def test_shifted_scan_order_keeps_first_bindings_and_still_hits(tmp_path):
    calls = []
    def upstream(body):
        calls.append(copy.deepcopy(body))
        return {'answer': 'done'}
    ask = wrap(tmp_path, upstream)
    listing = {'model': 'test', 'messages': [
        {'role': 'user', 'content': f'Compare {TASK} and {OTHER}.'}]}
    reordered = {'model': 'test', 'messages': [
        {'role': 'user', 'content': f'Compare {TASK} and {OTHER}.'},
        {'role': 'assistant', 'content': f'{OTHER} first, then {TASK}.'}]}
    shifted = {'model': 'test', 'messages': [
        {'role': 'assistant', 'content': f'{OTHER} first, then {TASK}.'},
        {'role': 'user', 'content': f'Compare {TASK} and {OTHER}.'}]}
    ask(listing)
    ask(reordered)  # scan still starts at the user message
    ask(shifted)    # scan now sees OTHER first: proposals conflict, none raise
    assert ask(listing) == {'answer': 'done'} and len(calls) == 3  # exact repeat hits
    assert all('test_id_00000000' in json.dumps(c) for c in calls)


def test_live_response_that_does_not_round_trip_is_served_not_cached(tmp_path):
    calls = []
    def upstream(body):
        calls.append(body)
        # Echo the caller's raw ID even though the input carried handles.
        return {'answer': f'status of {TASK} is fine'}
    ask = wrap(tmp_path, upstream)
    body = {'model': 'test', 'messages': [
        {'role': 'user', 'content': f'Report the status of {TASK}.'}]}
    assert ask(copy.deepcopy(body)) == {'answer': f'status of {TASK} is fine'}
    assert ask(copy.deepcopy(body)) == {'answer': f'status of {TASK} is fine'}
    assert len(calls) == 2  # never cached, both live


def test_cached_payload_that_does_not_round_trip_raises_for_cached_only(tmp_path):
    config = Config(mode='testing', path=tmp_path/'cache.db', test_aliases=aliases)
    session = AliasSession({'model': 'test', 'messages': [
        {'role': 'user', 'content': f'Report {TASK}.'}]}, 'caller', config)
    stray = {'kind': 'json', 'value': {'answer': f'{TASK} is fine'}}
    with pytest.raises(AliasContractUnsatisfied, match='round-trip'):
        session.output(stray)
    rendered = session.output(stray, live=True)
    assert rendered['value'] == {'answer': f'{TASK} is fine'}
    assert session.cacheable is False


def test_signed_token_with_coincidental_id_substring_is_left_alone(tmp_path):
    # Signature bytes can contain an ID-shaped substring by coincidence
    # (base64 uses non-word separators); the token must never be rewritten
    # and the request must not fail.
    calls = []
    def upstream(body):
        calls.append(copy.deepcopy(body))
        return {'answer': 'done'}
    ask = wrap(tmp_path, upstream)
    signed = f'call_1__thought__Eos+{TASK}-RARFNMg'
    body = conversation(call_id=signed, args={'task_id': TASK})
    body['messages'][2]['tool_call_id'] = signed
    assert ask(body) == {'answer': 'done'}
    assert calls[0]['messages'][1]['tool_calls'][0]['id'] == signed


def test_rejection_reasons_become_misses_not_errors(tmp_path):
    # The measured CI failure mode: alias raises surfacing as HTTP 500s.
    # Every shape above must land as a miss (or hit), never an exception.
    before = cache_stats()['requests']
    def upstream(body):
        return {'answer': 'live'}
    ask = wrap(tmp_path, upstream)
    ask(conversation())
    after = cache_stats()
    assert after['requests'] == before + 1


def wrap_signed(tmp_path, upstream):
    """The proxy's configuration: aliases plus signed-call handle keying."""
    return cached_llm_response(mode='testing', min_words=0, path=tmp_path/'cache.db',
                               test_aliases=aliases, signed_call_handles=True)(upstream)


def signed_history(task, call_id):
    """A turn echoing a signed tool call whose arguments name the task."""
    return {'model': 'test', 'stream': False, 'messages': [
        {'role': 'user', 'content': f'Report the status of {task}.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': call_id, 'type': 'function',
             'function': {'name': 'kanban_show',
                          'arguments': json.dumps({'task_id': task})}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'name': 'kanban_show',
         'content': json.dumps({'task': {'id': task, 'status': 'running'}})},
        {'role': 'user', 'content': 'Summarize the status.'},
    ]}


def test_two_runs_share_a_hit_despite_fresh_ids_and_fresh_signatures(tmp_path):
    """The cross-run case: nothing is carried over but the cache itself.

    Two runs of one conversation mint their own task id AND receive their own
    thought signature, so neither the ids nor the signed tokens repeat and
    neither run recorded the other's signed call. The key is the translated
    form, so they still agree; the provider still receives each run's own
    signed bytes.
    """
    sent = []

    def upstream(body):
        sent.append(copy.deepcopy(body))
        return {'answer': 'ok'}

    ask = wrap_signed(tmp_path, upstream)
    first = signed_history(TASK, SIGNED)
    second = signed_history(OTHER, 'call_999999__thought__adifferentsignature')
    assert ask(copy.deepcopy(first)) == {'answer': 'ok'}
    assert ask(copy.deepcopy(second)) == {'answer': 'ok'}
    assert len(sent) == 1, 'the second run should have replayed the first'
    # The provider saw run 1's exact signed bytes, not a translated form.
    forwarded = sent[0]['messages'][1]['tool_calls'][0]
    assert forwarded['id'] == SIGNED
    assert json.loads(forwarded['function']['arguments'])['task_id'] == TASK


def test_the_provider_still_receives_its_own_signed_bytes_on_a_miss(tmp_path):
    """Keying on the translated form must not change what is sent."""
    sent = []

    def upstream(body):
        sent.append(copy.deepcopy(body))
        return {'answer': 'ok'}

    ask = wrap_signed(tmp_path, upstream)
    body = signed_history(TASK, SIGNED)
    ask(copy.deepcopy(body))
    call = sent[0]['messages'][1]['tool_calls'][0]
    assert call['function']['arguments'] == json.dumps({'task_id': TASK})
    assert 'test_id_' not in call['function']['arguments']
    # Everything outside the signed call is still translated for the model.
    assert 'test_id_00000000' in sent[0]['messages'][0]['content']


def test_a_changed_signed_argument_still_misses(tmp_path):
    """Canonicalizing declared handles must not blur a real argument change."""
    sent = []

    def upstream(body):
        sent.append(copy.deepcopy(body))
        return {'answer': 'ok'}

    ask = wrap_signed(tmp_path, upstream)
    first = signed_history(TASK, SIGNED)
    second = signed_history(OTHER, 'call_999999__thought__adifferentsignature')
    second['messages'][1]['tool_calls'][0]['function']['arguments'] = json.dumps(
        {'task_id': OTHER, 'include_events': True})
    ask(copy.deepcopy(first))
    ask(copy.deepcopy(second))
    assert len(sent) == 2
