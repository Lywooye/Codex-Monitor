"""Read-only Codex ledger. A turn is a task, not an individual model call."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
          'reasoning_output_tokens', 'total_tokens')


def usage(value=None):
    value = value or {}
    return {k: max(0, int(value.get(k) or 0)) for k in FIELDS}


def plus(values):
    result = usage()
    for value in values:
        for key in FIELDS:
            result[key] += value.get(key, 0)
    return result


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def text_of(payload):
    content = payload.get('content', [])
    if isinstance(content, str):
        return content
    return '\n\n'.join(x.get('text', '') for x in content if isinstance(x, dict)).strip()


def is_context(text):
    return text.startswith(('<environment_context>', '<permissions instructions>',
                            '# AGENTS.md instructions', '<external_codex_apps_open_page>'))


class Ledger:
    def __init__(self, keep_messages=True):
        self.meta = {}
        self.turns = {}
        self.current = None
        self.records = {}
        self.native_turns = set()
        self.compaction_ids = set()
        self.previous = None
        self.last_info = {}
        self.messages = []
        self.snapshots = []
        self.warnings = set()
        self.ordinal = 0
        self.updated_at = None
        self.keep_messages = keep_messages
        self.foreign_turns = set()
        self.legacy_prefix = usage()

    def turn(self, key=None):
        key = key or self.current or 'legacy:1'
        self.current = key
        if key not in self.turns:
            self.turns[key] = {'id': key, 'index': len(self.turns) + 1,
                               'legacy': [], 'tools': {}, 'status': 'running',
                               'started_at': None, 'completed_at': None}
        return self.turns[key]

    def record(self, payload, timestamp, compaction=False):
        owner = payload.get('thread_id')
        if owner and self.meta.get('id') and owner != self.meta['id']:
            # The following token_count is a mirror of this inherited response.
            inherited = payload.get('turn_id') or 'inherited'
            self.foreign_turns.add(inherited)
            self.turn(inherited)
            return
        response = payload.get('response_id')
        if not response:
            self.warnings.add('native_record_without_response_id')
            return
        turn_id = payload.get('turn_id') or self.current or 'unassigned'
        self.turn(turn_id)['legacy'].clear()
        self.native_turns.add(turn_id)
        previous = self.records.get(response)
        self.records[response] = {'id': response, 'turn_id': turn_id,
            'timestamp': previous['timestamp'] if previous else epoch(timestamp),
            'usage': usage(payload.get('usage'))}
        if compaction:
            self.compaction_ids.add(response)

    def append(self, rows):
        for row in rows:
            self.ordinal += 1
            p = row.get('payload') or {}
            kind = row.get('type')
            timestamp = row.get('timestamp')
            self.updated_at = timestamp or self.updated_at
            if kind == 'session_meta':
                if not self.meta:
                    self.meta = {key:p.get(key) for key in
                        ('id', 'cwd', 'creator_account_id', 'forked_from_id', 'parent_thread_id')}
                continue
            if kind == 'token_usage_record':
                self.record(p, timestamp)
                continue
            if kind == 'compacted':
                response = p.get('compaction_response_id')
                embedded = p.get('latest_token_usage_record')
                if embedded and embedded.get('thread_id') not in (None, self.meta.get('id')):
                    continue
                if response:
                    self.compaction_ids.add(response)
                if embedded and embedded.get('response_id') == response:
                    self.record(embedded, timestamp, compaction=True)
                elif not response:
                    self.warnings.add('compaction_usage_unavailable')
                continue
            if kind == 'turn_context':
                if p.get('turn_id'):
                    self.turn(p['turn_id'])
                continue
            if kind == 'event_msg':
                event = p.get('type')
                if event == 'task_started':
                    t = self.turn(p.get('turn_id'))
                    t['started_at'] = t['started_at'] or timestamp
                elif event in ('task_complete', 'turn_aborted'):
                    t = self.turn(p.get('turn_id'))
                    t['status'] = 'completed' if event == 'task_complete' else 'aborted'
                    t['completed_at'] = timestamp
                elif event == 'token_count':
                    for window in (p.get('rate_limits') or {}).values():
                        if isinstance(window, dict) and window.get('window_minutes') == 10080:
                            percent = window.get('used_percent')
                            reset = window.get('resets_at')
                            if isinstance(percent, (int, float)) and isinstance(reset, (int, float)):
                                self.snapshots.append({'timestamp': epoch(timestamp), 'used_percent': percent,
                                    'resets_at': reset, 'limit_id': (p.get('rate_limits') or {}).get('limit_id') or 'codex',
                                    'account': self.meta.get('creator_account_id') or 'unknown'})
                    info = p.get('info')
                    if not info:
                        continue
                    self.last_info = info
                    self.turn()['context_info'] = info
                    total = usage(info.get('total_token_usage'))
                    last = usage(info.get('last_token_usage'))
                    if self.previous is None:
                        delta = last
                        if total['total_tokens'] > last['total_tokens']:
                            self.warnings.add('unobserved_legacy_prefix')
                            self.legacy_prefix = {k: max(0, total[k]-last[k]) for k in FIELDS}
                    elif total['total_tokens'] > self.previous['total_tokens']:
                        delta = {k: max(0, total[k] - self.previous[k]) for k in FIELDS}
                    elif total['total_tokens'] < self.previous['total_tokens']:
                        delta = usage()
                        self.warnings.add('legacy_counter_decreased')
                    else:
                        delta = usage()
                    self.previous = total
                    if delta['total_tokens'] and self.current not in self.native_turns:
                        self.turn()['legacy'].append({'id': f'legacy:{self.ordinal}',
                            'turn_id': self.current, 'timestamp': epoch(timestamp), 'usage': delta})
                continue
            if kind != 'response_item':
                continue
            item = p.get('type')
            if item == 'message' and p.get('role') in ('user', 'assistant'):
                text = text_of(p)
                if p['role'] == 'user' and is_context(text):
                    continue
                if p['role'] == 'user' and (not self.current or self.current.startswith('legacy:')):
                    self.turn(f'legacy:{len(self.turns) + 1}')
                t = self.turn()
                if self.keep_messages:
                    self.messages.append({'role': p['role'], 'text': text,
                        'timestamp': timestamp, 'turn_id': t['id'], 'turn_index': t['index'],
                        'phase': p.get('phase'), 'token_usage': None, 'token_footer': None})
            elif item in ('function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call'):
                t = self.turn()
                call_id = p.get('call_id') or p.get('id') or f'call:{self.ordinal}'
                t['tools'].setdefault(call_id, {'name': p.get('name') or item,
                    'status': 'called', 'timestamp': timestamp})
            elif item in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
                call_id = p.get('call_id')
                for t in reversed(list(self.turns.values())):
                    if call_id in t['tools']:
                        t['tools'][call_id]['status'] = 'returned'
                        break

    def result(self):
        native_by_turn = {}
        for record in self.records.values():
            native_by_turn.setdefault(record['turn_id'], []).append(record)
        calls = []
        turns = []
        for key, t in self.turns.items():
            if key in self.foreign_turns and key not in native_by_turn:
                continue
            records = native_by_turn.get(key, [])
            if not records:
                records = t['legacy']
            if not records and not t['tools']:
                continue
            calls.extend(records)
            total = plus(r['usage'] for r in records)
            compact = plus(r['usage'] for r in records if r['id'] in self.compaction_ids)
            tools = Counter(x['name'] for x in t['tools'].values())
            turns.append({'turn_id': key, 'index': len(turns)+1, 'status': t['status'],
                'started_at': t['started_at'], 'completed_at': t['completed_at'],
                'usage': total, 'compaction_tokens': compact['total_tokens'],
                'model_calls': len(records), 'tool_count': len(t['tools']),
                'tools': [{'name': name, 'count': count,
                    'returned':sum(x['name']==name and x['status']=='returned' for x in t['tools'].values())}
                    for name, count in sorted(tools.items())],
                'tools_returned': sum(x['status']=='returned' for x in t['tools'].values()),
                'source': 'native' if key in native_by_turn else 'legacy'})
        # Native records are authoritative for a turn: token_count mirrors are not added again.
        if self.compaction_ids - self.records.keys():
            self.warnings.add('compaction_usage_unavailable')
        # A legacy cumulative prefix is known session usage, but its turn and
        # time are unknown. Keep it in totals and outside quota attribution.
        if not self.records and not self.foreign_turns and self.legacy_prefix['total_tokens']:
            calls.append({'id':'legacy:prefix', 'turn_id':None, 'timestamp':None,
                          'usage':self.legacy_prefix})
        total = plus(r['usage'] for r in calls)
        last = turns[-1] if turns else {'usage': usage(), 'compaction_tokens':0, 'tool_count':0, 'tools':[]}
        latest = usage(self.last_info.get('last_token_usage'))
        window = self.last_info.get('model_context_window')
        summary = {'thread_id': self.meta.get('id'), 'cwd': self.meta.get('cwd'),
            'updated_at': self.updated_at, 'context_window': window,
            'latest_context_tokens': latest['input_tokens'],
            'latest_context_percent': round(100*latest['input_tokens']/window,1) if window else None,
            'latest_turn_id': last.get('turn_id'), 'latest_turn_compaction_tokens':last['compaction_tokens'],
            'session_compaction_tokens':sum(t['compaction_tokens'] for t in turns),
            'latest_turn_tool_count':last['tool_count'], 'latest_turn_tools':last['tools'],
            'session_tool_count':sum(t['tool_count'] for t in turns), 'turn_count':len(turns),
            'accounting_source':'native' if self.records else 'legacy',
            'warnings':sorted(self.warnings - ({'unobserved_legacy_prefix'} if self.records else set())),
            'inherited_history_excluded':bool(self.meta.get('forked_from_id') or self.meta.get('parent_thread_id'))}
        for suffix, key in [('total_tokens','total_tokens'),('input_tokens','input_tokens'),
                            ('cached_input_tokens','cached_input_tokens'),('output_tokens','output_tokens'),
                            ('reasoning_tokens','reasoning_output_tokens')]:
            summary['session_'+suffix] = total[key]
            summary['latest_turn_'+suffix] = last['usage'][key]
        by_turn = {t['turn_id']: t for t in turns}
        messages = [dict(m, total_turns=len(turns), token_usage=None, token_footer=None) for m in self.messages]
        last_message = {}
        for m in messages:
            if m['role']=='assistant' and m['turn_id'] in by_turn:
                last_message[m['turn_id']] = m
        for key, m in last_message.items():
            t = by_turn[key]
            context_info = self.turns[key].get('context_info', {})
            context_tokens = usage(context_info.get('last_token_usage'))['input_tokens']
            context_window = context_info.get('model_context_window')
            fields = dict(summary, context_window=context_window, latest_context_tokens=context_tokens,
                          latest_context_percent=round(100*context_tokens/context_window,1) if context_window else None, latest_turn_id=key, latest_turn_compaction_tokens=t['compaction_tokens'],
                          latest_turn_tool_count=t['tool_count'], latest_turn_tools=t['tools'])
            for suffix, src in [('total_tokens','total_tokens'),('input_tokens','input_tokens'),
                                ('cached_input_tokens','cached_input_tokens'),('output_tokens','output_tokens'),
                                ('reasoning_tokens','reasoning_output_tokens')]:
                fields['latest_turn_'+suffix] = t['usage'][src]
            m.update(token_usage=fields, turn_index=t['index'])
        return {'summary':summary,'messages':messages,'turns':turns,'calls':calls,
                'snapshots':self.snapshots,'meta':self.meta}
