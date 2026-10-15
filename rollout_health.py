#!/usr/bin/env python3
"""Read-only post-reload scheduler check. No payloads, credentials or cache mutations."""
import argparse
import json
import sqlite3
from pathlib import Path


def cached_failures(db):
    results=[]
    for job_id,state in db.execute('SELECT job_id,state_json FROM cron_jobs'):
        state=json.loads(state or '{}')
        error=str(state.get('lastError') or '')
        if state.get('lastRunStatus')=='error' and ('before_tool_call hook failed' in error or 'PluginInstanceUnavailableError' in error):
            results.append({'job_id':job_id,'last_run_ms':state.get('lastRunAtMs'),'consecutive_errors':state.get('consecutiveErrors'),
                            'repair':'Refresh the existing script toolset, run and verify, then restore and verify. See README.'})
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=str(Path.home()/'.openclaw/state/openclaw.sqlite'))
    args=parser.parse_args()
    with sqlite3.connect('file:'+str(Path(args.database).resolve())+'?mode=ro',uri=True) as db:
        failures=cached_failures(db)
    print(json.dumps({'cached_runtime_failures':failures},indent=2))
    raise SystemExit(bool(failures))
