import copy
import json

import pytest

from cached_response import cached_llm_response
from cached_response import matching
from cached_response.adapters import verification_text
from cached_response.config import Config
from cached_response.normalize import dumps
from cached_response.matching import PairRejected


def request(target='job_ab12cd34', events=None):
    return {'model': 'test', 'messages': [
        {'role': 'system', 'content': f'Work in /workspace/{target}. Inspect current state.'},
        {'role': 'user', 'content': f'Inspect {target}.'},
        {'role': 'tool', 'content': json.dumps({'target': target, 'events': events or []})},
    ]}


def prepare(a,b,result):
    return matching.prepare(a,b,result,Config(metadata_paths=('messages.*.content.note',)))


def test_shared_settings_roundtrip_preserves_types_and_literal_fields():
    shared={'nested':[1,True,1.0,None], 'description':'schema '*16000}
    old={'messages':[], 'tools':shared, 'shared_input':{'literal':None}}
    new=copy.deepcopy(old)
    original={'instruction':'Review.', 'old_input':old, 'new_input':new}
    before=dumps(original)
    _,text=verification_text(original)
    packed=json.loads(text)
    assert {**packed['shared_input'], **packed['old_input']} == old
    assert dumps({**packed['shared_input'], **packed['new_input']}) == dumps(new)
    assert dumps(original)==before
    assert len(text) < len(before)-len(dumps(shared))/2


def test_large_shared_verifier_context_reaches_callback_without_dropping_inputs(tmp_path):
    calls,judges=[],[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',structural_matching=True,
                         verifier_overrider=lambda e: judges.append(e) or {'safe_to_reuse':False,'reason':'state matters'})
    def ask(body):
        calls.append(body)
        return 'read current state'
    a,b=request(),request(events=[{'state':'ready'}])
    a['tools']=b['tools']=[{'description':'tool schema '*16000}]
    ask(a);ask(b)
    assert len(calls)==2 and not judges  # Undeclared state changes never reach a judge.


@pytest.mark.parametrize('mode',['testing','risky'])
def test_additional_id_occurrences_and_history_require_approval(tmp_path,mode):
    calls,judges=[],[]
    @cached_llm_response(mode=mode,min_words=0,path=tmp_path/'cache.db',structural_matching=True,
                         verifier_overrider=lambda e:judges.append(e) or {'safe_to_reuse':True,'reason':'specific test pair'})
    def ask(body):
        calls.append(body)
        return {'action':'inspect','target':'job_ab12cd34'}
    a,b=request(),request('job_ef56ab78',events=[{'target':'job_ef56ab78','state':'ready'}])
    b['messages'].insert(2,{'role':'assistant','content':'A progress note.'})
    ask(a)
    assert ask(b)=={'action':'inspect','target':'job_ab12cd34'}
    assert len(calls)==2 and not judges


@pytest.mark.parametrize('change',['instruction','split','merge','opaque','type','inserted_opaque','unknown_role'])
def test_structural_guards_reject_before_semantic_review(change):
    a,b=request(),request('job_ef56ab78')
    result={'target':'job_ab12cd34'}
    if change=='instruction':b['messages'][1]['content']='Delete the resource.'
    elif change=='split':b['messages'][2]['content']=json.dumps({'target':'job_0123abcd','events':[]})
    elif change=='merge':
        a['messages'][2]['content']=json.dumps({'target':'job_0123abcd','events':[]})
        result={'target':'job_0123abcd'}
    elif change=='opaque':
        a['messages'][2]['reasoning_content']='opaque A'
        b['messages'][2]['reasoning_content']='opaque B'
    elif change=='inserted_opaque':b['messages'].insert(2,{'role':'assistant','reasoning_content':'opaque'})
    elif change=='type':
        a['messages'][2]['content']=json.dumps({'limit':[1]})
        b['messages'][2]['content']=json.dumps({'limit':[True]})
    else:b['messages'][2]['role']='unknown'
    with pytest.raises(PairRejected):prepare(a,b,result)


def test_structural_retrieval_preserves_caller_settings(tmp_path):
    calls,judges=[],[]
    @cached_llm_response(mode='testing',min_words=0,path=tmp_path/'cache.db',structural_matching=True,
                         verifier_overrider=lambda e:judges.append(e) or {'safe_to_reuse':True,'reason':'yes'})
    def ask(body):calls.append(body);return 'inspect'
    a,b=request(),request(events=[{'ready':True}]);b['model']='different'
    ask(a);ask(b)
    assert len(calls)==2 and not judges


@pytest.mark.parametrize('options',[{'mode':'conservative'},{'mode':'disabled'},{'mode':'testing','learning':False}])
def test_structural_mode_gate(tmp_path,options):
    calls,judges=[],[]
    @cached_llm_response(min_words=0,path=tmp_path/'cache.db',structural_matching=True,
                         verifier_overrider=lambda e:judges.append(e) or {'safe_to_reuse':True,'reason':'yes'},**options)
    def ask(body):calls.append(body);return 'inspect'
    ask(request());ask(request(events=[{'ready':True}]))
    assert len(calls)==2 and not judges


def test_tool_results_align_with_their_producing_function():
    old=[{'role':'user','content':'inspect'},
         {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'first'}}]},
         {'role':'tool','tool_call_id':'a','content':'result'},
         {'role':'assistant','tool_calls':[{'id':'b','function':{'name':'second'}}]},
         {'role':'tool','tool_call_id':'b','content':'result'}]
    new=copy.deepcopy(old)
    new[3:3]=[{'role':'assistant','tool_calls':[{'id':'c','function':{'name':'extra'}}]},
               {'role':'tool','tool_call_id':'c','content':'result'}]
    with pytest.raises(PairRejected, match='pair_sequence_changed'):
        prepare({'messages':old}, {'messages':new}, 'inspect')


@pytest.mark.parametrize('changed_state',[False,True])
def test_signed_call_ids_preserve_opaque_suffix(changed_state):
    a,b=request(),request('job_ef56ab78')
    suffix='opaqueProviderState'
    old='call_ab12cd34__thought__'+suffix
    new='call_ef56ab78__thought__'+(suffix+'Changed' if changed_state else suffix)
    for body,call in [(a,old),(b,new)]:
        body['messages'].insert(2,{'role':'assistant','tool_calls':[{'id':call,'function':{'name':'inspect','arguments':'{}'}}]})
        body['messages'][3]['tool_call_id']=call
    with pytest.raises(PairRejected):
        prepare(a,b,{'action':'inspect'})


def test_unchanged_history_needs_no_pair_review():
    body=request()
    with pytest.raises(PairRejected,match='metadata_segment_limit'):
        prepare(body,body,'answer')
