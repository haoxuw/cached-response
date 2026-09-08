import copy
import json

import pytest

from cached_response import cached_llm_response
from cached_response import evidence, structural
from cached_response.learning import STATE_KEYS, reference_view
from cached_response.normalize import dumps, normalize
from cached_response.pairwise import PairRejected


def request(target='job_ab12cd34', events=None):
    return {'model': 'test', 'messages': [
        {'role': 'system', 'content': f'Work in /workspace/{target}. Inspect current state.'},
        {'role': 'user', 'content': f'Inspect {target}.'},
        {'role': 'tool', 'content': json.dumps({'target': target, 'events': events or []})},
    ]}


def prepare(a,b,result):
    old,ob=reference_view(normalize(a,'testing'))
    new,nb=reference_view(normalize(b,'testing'))
    return structural.prepare(old,new,ob,nb,result,STATE_KEYS)


def test_evidence_roundtrip_preserves_nested_types_literal_tags_and_nulls():
    shared={'text':'schema '*1000,'nested':[1,True,1.0,None,{'references':[{'path':[],'shared':0}]}]}
    source={'old':{'tools':shared},'new':{'tools':copy.deepcopy(shared)},'document':None,'shared':shared}
    before=dumps(source)
    packed=evidence.compact(source)
    assert dumps(evidence.expand(packed))==before
    assert dumps(source)==before
    assert len(dumps(packed)) < len(before) - len(dumps(shared)) / 2


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
    assert len(calls)==2 and len(judges)==1
    assert judges[0]['old_input']==a and judges[0]['new_input']==b
    instruction,text=evidence.transport(judges[0])
    assert len(text)+len(instruction)<300000
    assert evidence.expand(json.loads(text))['old_input']==a


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
    assert ask(b)=={'action':'inspect','target':'job_ef56ab78'}
    assert len(calls)==len(judges)==1
    assert judges[0]['old_input']==a and judges[0]['new_input']==b
    assert judges[0]['reference_alignment']['added_messages']==[2]


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
    assert structural.aligned_messages(old,new)==[(0,0),(1,1),(2,2),(3,5),(4,6)]


@pytest.mark.parametrize('changed_state',[False,True])
def test_signed_call_ids_preserve_opaque_suffix(changed_state):
    a,b=request(),request('job_ef56ab78')
    suffix='opaqueProviderState'
    old='call_ab12cd34__thought__'+suffix
    new='call_ef56ab78__thought__'+(suffix+'Changed' if changed_state else suffix)
    for body,call in [(a,old),(b,new)]:
        body['messages'].insert(2,{'role':'assistant','tool_calls':[{'id':call,'function':{'name':'inspect','arguments':'{}'}}]})
        body['messages'][3]['tool_call_id']=call
    if changed_state:
        with pytest.raises(PairRejected,match='pair_provider_state_changed'):
            prepare(a,b,{'action':'inspect'})
    else:
        assert prepare(a,b,{'action':'inspect'})[0]=={'action':'inspect'}


def test_alignment_work_is_bounded():
    body=request()
    body['messages'] += [{'role':'tool','content':'result'}]*structural.MAX_MESSAGES
    with pytest.raises(PairRejected,match='history_alignment_limit'):
        prepare(body,body,'answer')
