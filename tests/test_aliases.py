"""Explicit test IDs round-trip across cached turns and opaque signatures."""
import asyncio
import copy
import json
import re

import pytest

from cached_response import cached_llm_response
from cached_response.adapters import TestAliases as AliasSession
from cached_response.config import Config
from cached_response.normalize import translate

CANON = 'task_00000001'
OLD = 'task_ab12cd34'
NEW = 'task_ef56ab78'
SIGNED = 'call_abc__thought__opaque-SIGNED-byte-string'
ARGS = '{ "task" : "task_00000001", "count": 1 }'


def contract(body):
    # Application-owned fixture: one fresh task per isolated test.
    tokens = set(re.findall(r'\btask_[0-9a-f]{8}\b', body['messages'][1]['content']))
    assert len(tokens) == 1
    token = next(iter(tokens))
    return token, {token: CANON}


def request(token=OLD):
    return {'model': 'test', 'messages': [
        {'role': 'system', 'content': f'Work in /tests/{token}. Read only.'},
        {'role': 'user', 'content': f'Inspect {token}.'},
    ]}


def signed_response():
    return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
        {'id': SIGNED, 'type': 'function', 'function': {'name': 'inspect', 'arguments': ARGS},
         'provider_specific_fields': {'thought_signature': 'opaque-SIGNED-byte-string'}}
    ]}}]}


def history(body, response):
    body = copy.deepcopy(body)
    message = copy.deepcopy(response['choices'][0]['message'])
    # A client can parse/dump tool arguments before sending its next turn.
    for call in message['tool_calls']:
        call.pop('provider_specific_fields')
        call['function']['arguments'] = json.dumps(json.loads(call['function']['arguments']))
    body['messages'] += [message, {'role': 'tool', 'tool_call_id': SIGNED, 'content': '{"status":"ready"}'}]
    return body


@pytest.mark.parametrize('asynchronous', [False, True])
def test_cached_multi_turn_aliases_restore_exact_provider_arguments(tmp_path, asynchronous):
    calls = []
    def upstream(body):
        calls.append(copy.deepcopy(body))
        assert CANON in json.dumps(body)
        if len(body['messages']) == 2:
            return signed_response()
        assert body['messages'][2]['tool_calls'][0]['function']['arguments'] == ARGS
        return {'answer': f'Ready: {CANON}'}
    async def async_upstream(body):
        return upstream(body)
    ask = cached_llm_response(mode='testing', min_words=0, path=tmp_path/'cache.db',
                              test_aliases=contract)(async_upstream if asynchronous else upstream)
    def run(body):
        return asyncio.run(ask(body)) if asynchronous else ask(body)
    for token in (OLD, NEW):
        body = request(token)
        snapshot = copy.deepcopy(body)
        response = run(body)
        assert body == snapshot
        call = response['choices'][0]['message']['tool_calls'][0]
        assert call['id'] == SIGNED
        assert call['provider_specific_fields']['thought_signature'] == 'opaque-SIGNED-byte-string'
        assert token in call['function']['arguments'] and CANON not in call['function']['arguments']
        assert run(history(body, response)) == {'answer': f'Ready: {token}'}
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['instruction', 'fact', 'setting'])
def test_aliases_do_not_hide_meaningful_changes(tmp_path, change):
    calls = []
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path/'cache.db', test_aliases=contract)
    def ask(body):
        calls.append(body)
        return {'answer': CANON}
    ask(request())
    changed = request(NEW)
    if change == 'instruction': changed['messages'][0]['content'] += ' Delete everything.'
    if change == 'fact': changed['messages'].append({'role': 'tool', 'content': 'Status: deleted'})
    if change == 'setting': changed['temperature'] = .5
    ask(changed)
    assert len(calls) == 2


def test_alias_mapping_is_persistent_and_one_to_one(tmp_path):
    from cached_response.storage import Store
    import time
    path=tmp_path/'cache.db'
    assert Store(path).bind_aliases('session', {OLD:CANON}, time.time()+60) == {OLD:CANON}
    with pytest.raises(ValueError, match='changed'):
        Store(path).bind_aliases('session', {OLD:'task_00000002'}, time.time()+60)
    with pytest.raises(ValueError, match='one-to-one'):
        Store(path).bind_aliases('session', {NEW:CANON}, time.time()+60)


@pytest.mark.parametrize('mutate', ['unknown', 'changed_arguments', 'changed_type'])
def test_signed_history_must_restore_a_known_original(tmp_path, mutate):
    config = Config(mode='testing', path=tmp_path/'cache.db', test_aliases=contract)
    session = AliasSession(request(), 'caller', config)
    response = session.output({'kind':'json','value':signed_response()})['value']
    next_body = history(request(), response)
    call = next_body['messages'][2]['tool_calls'][0]
    if mutate == 'unknown': call['id'] += '-different-state'
    elif mutate == 'changed_arguments': call['function']['arguments'] = call['function']['arguments'].replace('"count": 1','"count": 2')
    else: call['function']['arguments'] = call['function']['arguments'].replace('"count": 1','"count": true')
    with pytest.raises(ValueError): AliasSession(next_body, 'caller', config)


def test_alias_caller_isolation(tmp_path):
    config = Config(mode='testing', path=tmp_path/'cache.db', test_aliases=contract)
    session = AliasSession(request(), 'caller', config)
    response = session.output({'kind':'json','value':signed_response()})['value']
    with pytest.raises(ValueError, match='unavailable'):
        AliasSession(history(request(),response), 'other-caller', config)


def test_translation_preserves_raw_json_layout_and_opaque_state():
    source = '{ "task": "'+OLD+'", "n": 1 }'
    result = translate(source, {OLD:CANON})
    assert result == source.replace(OLD,CANON)
    assert translate(result,{CANON:OLD}) == source
    assert translate({'thought_signature':OLD,'text':OLD},{OLD:CANON}) == {'thought_signature':OLD,'text':CANON}
    with pytest.raises(ValueError):
        translate(json.dumps({'thought_signature':OLD,'text':OLD}),{OLD:CANON})


def test_canonical_collision_rejected(tmp_path):
    config=Config(mode='testing',path=tmp_path/'cache.db',test_aliases=lambda b:('session',{OLD:CANON}))
    body=request();body['messages'][1]['content'] += ' and '+CANON
    with pytest.raises(ValueError,match='already occurs'):AliasSession(body,'caller',config)


def test_callback_cannot_mutate_original(tmp_path):
    body=request();snapshot=copy.deepcopy(body)
    def callback(copy):
        copy['messages'][0]['content']='Changed'
        return OLD,{OLD:CANON}
    AliasSession(body,'caller',Config(path=tmp_path/'cache.db',test_aliases=callback))
    assert body==snapshot


def test_streaming_http_ids_split_across_chunks(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient
    app=FastAPI();calls=[]
    @app.post('/chat')
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    async def ask(request:Request):
        body=await request.json();calls.append(body)
        assert int(request.headers['content-length']) == len(await request.body())
        async def events():
            for fragment in ('Ready: task_', '00000001'):
                yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':fragment},'finish_reason':None}]})+'\n\n'
            yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(events(),media_type='text/event-stream')
    with TestClient(app) as client:
        for token in (OLD,NEW):
            response=client.post('/chat',json=request(token))
            assert response.status_code==200 and 'Ready: '+token in response.text and CANON not in response.text
    assert len(calls)==1


def test_cache_bypass_keeps_active_alias_history(tmp_path):
    calls=[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    def ask(body):
        calls.append(body)
        return {'answer':CANON}
    for token in (OLD,NEW):
        assert ask(request(token),use_cache=False)=={'answer':token}
    assert len(calls)==2 and all(CANON in json.dumps(b) for b in calls)


def test_no_alias_contract_preserves_ordinary_result_types(tmp_path):
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=lambda b:None)
    def ask(body):return object()
    assert ask(request()) is not None


def test_concurrent_distinct_aliases_preserve_both_bindings(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from cached_response.storage import Store
    import time
    path=tmp_path/'cache.db';Store(path)
    def write(pair):return Store(path).bind_aliases('session',dict([pair]),time.time()+60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write,[(OLD,CANON),(NEW,'task_00000002')]))
    assert Store(path).review('session')=={OLD:CANON,NEW:'task_00000002'}


@pytest.mark.parametrize('field',['thought_signature','thoughtSignatures','thinking','thinking_blocks'])
def test_aliases_preserve_all_provider_state(field):
    value={field:[CANON], 'content':CANON}
    assert translate(value,{CANON:NEW})=={field:[CANON],'content':NEW}


def test_failed_render_does_not_cache_a_response(tmp_path):
    from cached_response.storage import get_store
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    def ask(body):return CANON.encode()
    with pytest.raises(ValueError,match='text or JSON'):ask(request())
    with get_store(str(tmp_path/'cache.db')).connect() as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM leases').fetchone()[0]==0


def test_concurrent_conversations_share_only_canonical_inference(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    calls=[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    def ask(body):
        calls.append(body);time.sleep(.05)
        return signed_response()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(ask,[request(OLD),request(NEW)]))
    for token,response in zip((OLD,NEW),responses):
        call=response['choices'][0]['message']['tool_calls'][0]
        assert token in call['function']['arguments'] and call['id']==SIGNED
    assert len(calls)==1


def test_incomplete_alias_stream_releases_lease_and_closes(tmp_path):
    from starlette.responses import StreamingResponse
    from cached_response.storage import get_store
    closed=[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    async def ask(body):
        async def chunks():
            try:yield 'data: {"choices":[]}\n\n'
            finally:closed.append(True)
        return StreamingResponse(chunks(),media_type='text/event-stream')
    with pytest.raises(ValueError,match='Incomplete'):asyncio.run(ask(request()))
    with get_store(str(tmp_path/'cache.db')).connect() as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM leases').fetchone()[0]==0
    assert closed==[True]


def test_aliases_preserve_upstream_error_stream(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient
    from cached_response.storage import get_store
    app = FastAPI()
    @app.post('/chat')
    @cached_llm_response(mode='testing', min_words=0, path=tmp_path/'cache.db', test_aliases=contract)
    async def ask(request: Request):
        async def chunks():
            yield b'{"error":"try again"}'
        return StreamingResponse(chunks(), status_code=503, media_type='application/json')
    with TestClient(app) as client:
        response = client.post('/chat', json=request())
    assert response.status_code == 503
    assert response.json() == {'error': 'try again'}
    with get_store(str(tmp_path/'cache.db')).connect() as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM leases').fetchone()[0] == 0


def test_historical_example_ids_are_not_active_test_handles(tmp_path):
    calls=[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    def ask(body):
        calls.append(body)
        assert 'task_1234abcd' in body['messages'][0]['content']
        return {'answer':CANON}
    for token in (OLD,NEW):
        body=request(token)
        body['messages'][0]['content'] += ' Historical example: task_1234abcd.'
        assert ask(body)=={'answer':token}
    assert len(calls)==1


def test_embedded_output_handle_cannot_leak_a_canonical_id(tmp_path):
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    def ask(body):return {'answer':'prefix_'+CANON}
    with pytest.raises(ValueError,match='embedded'):ask(request())


def test_done_marker_finishes_alias_stream_without_waiting_for_socket_close(tmp_path):
    from starlette.responses import StreamingResponse
    closed=[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',test_aliases=contract)
    async def ask(body):
        async def chunks():
            try:
                yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':CANON},'finish_reason':'stop'}]})+'\n\n'
                yield 'data: [DONE]\n\n'
                await asyncio.Event().wait()
            finally:closed.append(True)
        return StreamingResponse(chunks(),media_type='text/event-stream')
    async def run():
        response=await asyncio.wait_for(ask(request()),5)
        return b''.join([chunk async for chunk in response.body_iterator]).decode()
    assert OLD in asyncio.run(run())
    assert closed==[True]


def test_aliases_reject_ids_embedded_in_dictionary_keys():
    with pytest.raises(ValueError, match='dictionary key'):
        translate({'data': {'prefix_' + CANON: 1}}, {CANON: NEW})
