"""Print synthetic UI data for the browser smoke test; never reads user logs."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_accounting import ledger_rows, entry
import context_token_injector as injector
from weekly_usage import estimate_weekly

with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'rollout-chat.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in ledger_rows())+'\n')
    payload=injector.build_payload([str(path)],10,'chat')
    base=1790812800; week=604800
    quota=estimate_weekly([entry('chat',[(base+2,100,'t1'),(base+week+2,100,'t1')],
        [(base+1,base+week,0),(base+3,base+week,4),
         (base+week+1,base+2*week,2),(base+week+3,base+2*week,8)])],'chat',base+week+4)
    payload['summaries'][0]['weekly']=quota
    item=payload['detailsByThread']['chat']['assistantItems'][0]
    item['weeklyTotal']=10
    item['weeklyCurrent']=6
    print(json.dumps({'script':injector.INJECTION_SCRIPT,'payload':payload}))
