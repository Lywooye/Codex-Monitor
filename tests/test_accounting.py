import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from usage_accounting import Ledger
from weekly_usage import estimate_weekly
import context_token_inspector as inspector
import context_token_injector as injector


def row(kind, payload, second=0):
    return {'type':kind,'timestamp':f'2026-01-01T00:00:{second:02d}Z','payload':payload}


def native(response, tokens, turn='t1', second=1, thread='chat'):
    return row('token_usage_record', {'response_id':response,'thread_id':thread,'turn_id':turn,
        'usage':{'input_tokens':tokens-10,'output_tokens':10,'total_tokens':tokens}},second)


def message(role, text='A sufficiently long reply', second=0):
    return row('response_item', {'type':'message','role':role,
        'content':[{'type':'output_text','text':text}]},second)


def counter(last, total, second=1):
    return row('event_msg', {'type':'token_count','info':{
        'last_token_usage':{'input_tokens':last-10,'output_tokens':10,'total_tokens':last},
        'total_token_usage':{'input_tokens':total-10,'output_tokens':10,'total_tokens':total},
        'model_context_window':10000}},second)


def ledger_rows():
    compact = native('compress', 300, second=3)
    return [row('session_meta',{'id':'chat'}),
        row('event_msg',{'type':'task_started','turn_id':'t1'}), message('user','Do it'),
        row('response_item',{'type':'function_call','name':'read_file','call_id':'call1'}),
        native('r1',100),counter(100,100),
        row('response_item',{'type':'function_call_output','call_id':'call1','output':'secret'}),
        compact, row('compacted',{'compaction_response_id':'compress','latest_token_usage_record':compact['payload']},3),
        message('assistant',second=4),native('r2',200,second=4),counter(200,300,4),
        counter(200,300,5),row('event_msg',{'type':'task_complete','turn_id':'t1'},5)]


class AccountingTests(unittest.TestCase):
    def test_compaction_tools_and_mirrors_known_answer(self):
        ledger=Ledger();ledger.append(ledger_rows());r=ledger.result()
        self.assertEqual(r['summary']['session_total_tokens'],600)
        self.assertEqual(r['summary']['latest_turn_total_tokens'],600)
        self.assertEqual(r['summary']['session_compaction_tokens'],300)
        self.assertEqual(r['turns'][0]['model_calls'],3)
        self.assertEqual(r['turns'][0]['tool_count'],1)
        self.assertEqual(r['turns'][0]['tools_returned'],1)
        self.assertEqual(r['messages'][-1]['token_usage']['latest_turn_total_tokens'],600)

    def test_embedded_only_compaction_is_counted_once(self):
        rows=[r for r in ledger_rows() if not (r['type']=='token_usage_record' and r['payload']['response_id']=='compress')]
        ledger=Ledger();ledger.append(rows);self.assertEqual(ledger.result()['summary']['session_total_tokens'],600)

    def test_duplicate_response_and_incremental_parse(self):
        rows=ledger_rows();ledger=Ledger();ledger.append(rows[:8]);ledger.append(rows[8:]);ledger.append([native('r2',200,second=4)])
        self.assertEqual(ledger.result()['summary']['session_total_tokens'],600)

    def test_separate_turns_and_all_tool_types(self):
        rows=ledger_rows()+[row('event_msg',{'type':'task_started','turn_id':'t2'},6),
            message('user','Next',6),row('response_item',{'type':'custom_tool_call','name':'search','call_id':'call2'},7),
            native('r3',400,'t2',7),message('assistant','Another sufficiently long reply',8)]
        ledger=Ledger();ledger.append(rows);r=ledger.result()
        self.assertEqual([t['usage']['total_tokens'] for t in r['turns']],[600,400])
        self.assertEqual(r['summary']['session_tool_count'],2)

    def test_foreign_replay_is_not_owned_usage(self):
        rows=[row('session_meta',{'id':'chat','forked_from_id':'parent'})]
        rows += [native('parent1',999,turn='parent-turn',thread='parent'),counter(999,999)]
        rows += [row('event_msg',{'type':'task_started','turn_id':'t1'}),native('own1',100),counter(100,1099)]
        ledger=Ledger();ledger.append(rows)
        self.assertEqual(ledger.result()['summary']['session_total_tokens'],100)

    def test_legacy_repeated_counter_does_not_double_count(self):
        ledger=Ledger();ledger.append([message('user'),counter(100,100),counter(100,100),counter(200,300)])
        self.assertEqual(ledger.result()['summary']['session_total_tokens'],300)

    def test_index_and_detail_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rollout-chat.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in ledger_rows())+'\n')
            full=inspector.parse_session_detail(path)
            indexed=inspector.session_index(path)
            self.assertEqual(full['summary']['session_total_tokens'],indexed['summary']['session_total_tokens'])
            payload=injector.build_payload([str(path)],10,'chat')
            item=payload['detailsByThread']['chat']['assistantItems'][0]
            self.assertEqual(item['tokenUsage']['latest_turn_total_tokens'],600)
            self.assertEqual(item['toolCount'],1)
            self.assertEqual(item['compactionTokens'],300)

    def test_cached_and_reasoning_subtotals_not_added_twice(self):
        r=native('r',100)
        r['payload']['usage'].update(cached_input_tokens=40,reasoning_output_tokens=5)
        ledger=Ledger();ledger.append([row('session_meta',{'id':'chat'}),r])
        self.assertEqual(ledger.result()['summary']['session_total_tokens'],100)

    def test_missing_compaction_usage_is_visible(self):
        ledger=Ledger();ledger.append([row('compacted',{'compaction_response_id':'missing'})])
        self.assertIn('compaction_usage_unavailable',ledger.result()['summary']['warnings'])

    def test_historical_context_not_replaced_by_latest_context(self):
        rows=ledger_rows()+[row('event_msg',{'type':'task_started','turn_id':'t2'}),
            message('user','Next'),native('r3',400,'t2'),message('assistant','Second answer'),counter(400,700)]
        ledger=Ledger();ledger.append(rows)
        replies=[m for m in ledger.result()['messages'] if m['token_usage']]
        self.assertEqual([m['token_usage']['latest_context_tokens'] for m in replies],[190,390])

    def test_index_retries_partial_line_and_file_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rollout-chat.jsonl'
            path.write_text(json.dumps(row('session_meta',{'id':'chat'}))+'\n')
            line=json.dumps(native('r',100))+'\n'
            with path.open('a') as f:f.write(line[:20])
            self.assertEqual(inspector.session_index(path)['summary']['session_total_tokens'],0)
            with path.open('a') as f:f.write(line[20:])
            self.assertEqual(inspector.session_index(path)['summary']['session_total_tokens'],100)
            path.write_text(json.dumps(native('new',200))+'\n')
            self.assertEqual(inspector.session_index(path)['summary']['session_total_tokens'],200)

    def test_detail_initial_partial_line_and_replaced_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rollout-chat.jsonl'
            line=json.dumps(native('r',100))+'\n'
            path.write_text(json.dumps(row('session_meta',{'id':'chat'}))+'\n'+line[:20])
            injector.cached_session_detail(str(path),{})
            with path.open('a') as f:f.write(line[20:])
            self.assertEqual(injector.cached_session_detail(str(path),{})['summary']['session_total_tokens'],100)
            inspector.session_index(path)
            replacement=path.with_suffix('.new')
            replacement.write_text(json.dumps(row('session_meta',{'id':'chat'}))+'\n'+json.dumps(native('new',300))+'\n'+(' ' * 1000)+'\n')
            replacement.replace(path)
            self.assertEqual(injector.cached_session_detail(str(path),{})['summary']['session_total_tokens'],300)
            self.assertEqual(inspector.session_index(path)['summary']['session_total_tokens'],300)

    def test_weekly_window_found_in_either_slot(self):
        ledger=Ledger()
        for key in ['primary','secondary']:
            ledger.append([row('event_msg',{'type':'token_count','rate_limits':{key:{
                'window_minutes':10080,'used_percent':5,'resets_at':1800000000}}})])
        self.assertEqual(len(ledger.snapshots),2)


def entry(thread, calls, points=()):
    return {'thread_id':thread,'account':'account','calls':[
        {'id':f'r{i}','timestamp':timestamp,'turn_id':turn,'usage':{'total_tokens':tokens}}
        for i,(timestamp,tokens,turn) in enumerate(calls)],'snapshots':[
        {'account':'account','limit_id':'codex','timestamp':timestamp,'resets_at':reset,'used_percent':pct}
        for timestamp,reset,pct in points]}


class WeeklyTests(unittest.TestCase):
    def test_two_cycles_and_concurrent_proportional_split(self):
        a=entry('a',[(15,100,'t1'),(35,100,'t2')],[(10,30,10),(20,30,14),(31,60,2),(40,60,8)])
        b=entry('b',[(15,300,'b1'),(35,100,'b2')])
        result=estimate_weekly([a,b],'a',now=45)
        self.assertEqual(result['total_pp'],4)
        self.assertEqual(result['current_pp'],3)
        self.assertEqual([p['estimated_pp'] for p in result['periods']],[1,3])
        self.assertEqual(result['coverage_percent'],100)

    def test_reset_crossing_interval_not_assumed_zero_baseline(self):
        a=entry('a',[(29,100,'t1'),(32,100,'t1')],[(10,30,10),(31,60,4),(40,60,6)])
        r=estimate_weekly([a],'a',45)
        self.assertEqual(r['total_pp'],2)
        self.assertEqual(r['uncovered_tokens'],100)
        self.assertIn('reset_boundary_unattributed',r['warnings'])

    def test_counter_drop_with_same_deadline_starts_new_segment(self):
        a=entry('a',[(15,100,'t1'),(35,100,'t1')],[(10,60,80),(20,60,90),(30,60,0),(40,60,5)])
        r=estimate_weekly([a],'a',45)
        self.assertEqual(r['total_pp'],15)
        self.assertEqual(r['current_pp'],5)
        self.assertEqual(len(r['periods']),2)

    def test_missing_and_stale_snapshots_remain_unknown(self):
        a=entry('a',[(15,100,'t1')])
        self.assertIsNone(estimate_weekly([a],'a',45)['current_pp'])
        a['snapshots']=entry('a',[],[(10,20,0),(18,20,2)])['snapshots']
        r=estimate_weekly([a],'a',45)
        self.assertIsNone(r['current_pp']);self.assertEqual(r['total_pp'],2)

    def test_copies_and_other_account_do_not_dilute_share(self):
        a=entry('a',[(15,100,'t1')],[(10,60,0),(20,60,4)])
        b=entry('b',[(15,900,'b1')]);b['account']='other'
        self.assertEqual(estimate_weekly([a,a,b],'a',45)['total_pp'],4)

    def test_lifetime_can_exceed_one_hundred_percentage_points(self):
        a=entry('a',[(15,100,'t1'),(35,100,'t1')],[(10,30,0),(20,30,90),(31,60,0),(40,60,80)])
        r=estimate_weekly([a],'a',45)
        self.assertEqual(r['total_pp'],170);self.assertEqual(r['current_pp'],80)

    def test_unattributable_delta_reported(self):
        a=entry('a',[],[(10,60,0),(20,60,4)])
        self.assertIn('quota_delta_without_local_tokens',estimate_weekly([a],'a',45)['warnings'])

    def test_missing_time_is_counted_as_gap(self):
        a=entry('a',[(None,100,'t1')],[(10,60,0),(20,60,4)])
        r=estimate_weekly([a],'a',45)
        self.assertEqual(r['uncovered_tokens'],100)
        self.assertEqual(r['coverage_percent'],0)
        self.assertIsNone(r['total_pp'])
        self.assertIsNone(r['current_pp'])

    def test_conflicting_snapshots_never_bridge(self):
        a=entry('a',[(15,100,'t1'),(25,100,'t1')],[(10,60,0),(20,60,2),(20,60,3),(30,60,5)])
        r=estimate_weekly([a],'a',45)
        self.assertEqual(r['uncovered_tokens'],200)
        self.assertIn('conflicting_snapshots',r['warnings'])


if __name__=='__main__':
    unittest.main()
