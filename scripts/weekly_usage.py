"""Account snapshot deltas apportioned by local tokens; never a billing claim."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import time


def estimate_weekly(entries, selected_thread, now=None):
    now = time.time() if now is None else now
    selected = [e for e in entries if e['thread_id'] == selected_thread]
    accounts = {e['account'] for e in selected}
    entries = [e for e in entries if e['account'] in accounts]
    if not selected:
        return {'estimated':True, 'total_pp':None, 'current_pp':None, 'periods':[],
                'warnings':['session_not_indexed'], 'coverage_percent':None}
    # Native response IDs deduplicate active/archive copies and replayed prefixes.
    calls = {}
    points = {}
    for entry in entries:
        for call in entry['calls']:
            identity = (entry['account'], entry['thread_id'], call['id'])
            calls[identity] = dict(call, thread_id=entry['thread_id'], account=entry['account'])
        for point in entry['snapshots']:
            if point['timestamp'] is None or point['limit_id'] != 'codex':
                continue
            key = (point['account'], point['timestamp'], point['resets_at'], point['used_percent'])
            points[key] = point
    by_account = defaultdict(list)
    for p in points.values():
        by_account[p['account']].append(p)
    periods = []
    covered_ids = set()
    total_tokens = sum(c['usage']['total_tokens'] for c in calls.values() if c['thread_id']==selected_thread)
    warnings = {'local_token_proportional_estimate', 'other_devices_unobserved', 'snapshot_rounding_and_delay'}
    if 'unknown' in accounts:
        warnings.add('account_identity_unavailable')
    turn_pp = defaultdict(float)
    turn_current_pp = defaultdict(float)
    current_period_keys = set()
    for account, account_points in by_account.items():
        account_points.sort(key=lambda p:(p['timestamp'], p['resets_at'], p['used_percent']))
        # Conflicting same-time observations are not safe interval endpoints.
        time_groups = defaultdict(list)
        for p in account_points:
            time_groups[p['timestamp']].append(p)
        clean = []
        for timestamp, group in sorted(time_groups.items()):
            distinct = {(p['resets_at'], p['used_percent']) for p in group}
            clean.append(group[0] if len(distinct)==1 else None)
            if len(distinct)>1:
                warnings.add('conflicting_snapshots')
        account_calls = sorted([(c['timestamp'], identity, c) for identity,c in calls.items()
                                if c['account']==account and c['timestamp'] is not None], key=lambda v:v[0])
        times = [c[0] for c in account_calls]
        period = None
        previous = None
        segment = 0
        for p in clean:
            if p is None:
                previous = None
                continue
            reset = p['resets_at']
            changed = previous is None or reset!=previous['resets_at'] or p['used_percent']<previous['used_percent']
            if changed:
                segment += 1
                period = {'resets_at':reset, 'segment':segment, 'first_observed_at':p['timestamp'],
                    'last_observed_at':p['timestamp'], 'estimated_pp':0.0,
                    'covered_tokens':0, 'observed_account_delta_pp':0.0,
                    'unattributed_account_delta_pp':0.0, 'current':False, '_turn_pp':defaultdict(float)}
                periods.append(period)
                if previous is not None:
                    warnings.add('reset_boundary_unattributed')
            else:
                delta = p['used_percent'] - previous['used_percent']
                # A sample recorded after its reset deadline is stale; never bridge it.
                if previous['timestamp'] < p['timestamp'] <= reset:
                    lo = bisect_right(times, previous['timestamp'])
                    hi = bisect_right(times, p['timestamp'])
                    interval = account_calls[lo:hi]
                    denominator = sum(c['usage']['total_tokens'] for _,_,c in interval)
                    period['observed_account_delta_pp'] += delta
                    if denominator:
                        for _,identity,c in interval:
                            if c['thread_id']!=selected_thread:
                                continue
                            tokens = c['usage']['total_tokens']
                            share = delta*tokens/denominator
                            period['estimated_pp'] += share
                            period['covered_tokens'] += tokens
                            covered_ids.add(identity)
                            turn_pp[c['turn_id']] += share
                            period['_turn_pp'][c['turn_id']] += share
                    elif delta:
                        period['unattributed_account_delta_pp'] += delta
                        warnings.add('quota_delta_without_local_tokens')
                else:
                    warnings.add('stale_quota_snapshot')
                period['last_observed_at'] = p['timestamp']
            previous = p
        if previous and previous['resets_at']>now and period:
            period['current'] = True
            for key,value in period['_turn_pp'].items():
                turn_current_pp[key] += value
            current_period_keys.add(id(period))
    covered_tokens = sum(calls[k]['usage']['total_tokens'] for k in covered_ids)
    if covered_tokens<total_tokens:
        warnings.add('uncovered_tokens')
    has_current = bool(current_period_keys)
    current_periods = [p for p in periods if p['current']]
    current_uncovered = any(c['thread_id']==selected_thread and identity not in covered_ids
        and (c['timestamp'] is None or any(p['resets_at']-604800 < c['timestamp'] <= p['resets_at']
                                        for p in current_periods))
        for identity,c in calls.items())
    current_observed = sum(p['covered_tokens'] for p in current_periods)
    if not has_current:
        warnings.add('current_cycle_snapshot_unavailable')
    for p in periods:
        p.pop('_turn_pp', None)
        p['estimated_pp'] = round(p['estimated_pp'], 6)
        p['reset_label'] = datetime.fromtimestamp(p['resets_at'], timezone.utc).isoformat()
    return {'estimated':True, 'total_pp':round(sum(p['estimated_pp'] for p in periods),6)
            if periods and (covered_tokens or not total_tokens) else None,
        'current_pp':round(sum(p['estimated_pp'] for p in current_periods),6)
            if has_current and (current_observed or not current_uncovered) else None,
        'periods':[p for p in periods if p['covered_tokens'] or p['current']],
        'coverage_percent':round(100*covered_tokens/total_tokens,1) if total_tokens else None,
        'covered_tokens':covered_tokens, 'uncovered_tokens':total_tokens-covered_tokens,
        'warnings':sorted(warnings), 'turn_estimated_pp':dict(turn_pp),
        'turn_current_pp':dict(turn_current_pp)}
