#!/usr/bin/env python3
"""Command line for The Machine's personal assistant.

Reads tasks, evidence, sources and review history from the running service and
can save a preparation for an accepted task. Owner decisions (accept, snooze,
done, dismiss, feedback) stay with Telegram and the dashboard; the service
refuses them from here.
"""
import argparse
import datetime as dt
import importlib.util
import json
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location('assistant', Path(__file__).resolve().with_name('assistant.py'))
assistant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assistant)

TZ = assistant.LOCAL_TZ
GROUPS = {
    'outstanding': ('pending', 'accepted', 'instructions', 'prepared'),
    'later': ('snoozed',),
    'done': ('completed', 'rejected'),
}
LABELS = {'pending': 'Needs your decision', 'accepted': 'Accepted', 'instructions': 'Changes requested',
          'prepared': 'Ready for you', 'snoozed': 'Later', 'completed': 'Done', 'rejected': 'Dismissed'}


class CliError(Exception):
    pass


def load_config(path):
    global TZ
    try:
        config = json.loads(Path(path).read_text())
        if config.get('timezone'):
            TZ = ZoneInfo(config['timezone'])
        return config
    except OSError as error:
        raise CliError(f'Cannot read config {path}: {error.strerror}') from None


def call(config, action, args=None):
    if action in assistant.OWNER_ACTIONS:
        raise CliError('Owner decisions and feedback come from Telegram or the dashboard, not the CLI.')
    request = urllib.request.Request(
        f"http://127.0.0.1:{config['port']}/api/action",
        data=json.dumps({'action': action, 'args': args or {}}).encode(),
        headers={'Authorization': 'Bearer ' + assistant.credential(config, 'api-token'),
                 'Content-Type': 'application/json', 'X-Assistant-Client': 'local-cli'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get('error')
        except ValueError:
            message = None
        raise CliError(message or f'Service answered HTTP {error.code}') from None
    except urllib.error.URLError:
        raise CliError('The assistant service is not reachable. Check: systemctl --user status personal-assistant.service') from None


def local(value, fmt='%a %d %b %H:%M'):
    if not value:
        return '-'
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(TZ).strftime(fmt)
    except ValueError:
        return str(value)


def loads(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def find_task(status, task_id):
    matches = [s for s in status['suggestions'] if s['id'].startswith(task_id)]
    if not matches:
        raise CliError(f'No task with id {task_id}. List them with: assistant tasks --all')
    if len(matches) > 1:
        raise CliError(f'Id {task_id} is ambiguous: ' + ', '.join(s['id'] for s in matches))
    return matches[0]


def out_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def wrap(text, indent='    '):
    return '\n'.join(textwrap.fill(line, 100, initial_indent=indent, subsequent_indent=indent) if line.strip() else ''
                     for line in str(text).splitlines())


def evidence_for(config, task):
    ids = loads(task.get('event_ids'), [])
    accounts = loads(task.get('account_ids'), [])
    if not ids:
        return []
    configured = [a['id'] for a in config['accounts']]
    return call(config, 'events', {'account_ids': [a for a in accounts if a in configured] or configured,
                                   'event_ids': ids, 'limit': max(len(ids), 1), 'full': True})


def print_event(event, full=False):
    meta = loads(event.get('metadata'), {})
    who = meta.get('sender_display') or meta.get('from') or ''
    print(f"  [{event['id']}] {local(event['occurred_at'])} · {event['account_id']} · {event['kind']}" + (f' · {who}' if who else ''))
    print(f"    {event.get('title') or '(no title)'}")
    if meta.get('source_url'):
        print(f"    {meta['source_url']}")
    body = event.get('body') or ''
    if not full and len(body) > 1500:
        body = body[:1500] + f'\n… ({len(event["body"])} characters; use --full for all)'
    print(wrap(body))


def cmd_tasks(config, args):
    status = call(config, 'status', {'full': True})
    groups = ['outstanding', 'later', 'done'] if args.all else [g for g in ('outstanding', 'later', 'done') if getattr(args, g)] or ['outstanding']
    wanted = {s for g in groups for s in GROUPS[g]}
    tasks = sorted((s for s in status['suggestions'] if s['status'] in wanted), key=lambda s: s['created_at'], reverse=True)
    if args.json:
        return out_json(tasks)
    if not tasks:
        print('No tasks in ' + ', '.join(groups) + '.')
        return
    for group in groups:
        rows = [s for s in tasks if s['status'] in GROUPS[group]]
        if not rows:
            continue
        print(f'{group.capitalize()} ({len(rows)})')
        for s in rows:
            extra = f" until {local(s['snoozed_until'])}" if s.get('snoozed_until') else ''
            print(f"  {s['id']}  {LABELS.get(s['status'], s['status'])}{extra} · {local(s['created_at'])}")
            print(f"    {s['title']}")
        print()


def cmd_show(config, args):
    status = call(config, 'status', {'full': True})
    task = find_task(status, args.id)
    decisions = [d for d in status['decisions'] if d['suggestion_id'] == task['id']]
    evidence = [] if args.no_evidence else evidence_for(config, task)
    if args.json:
        return out_json({'task': task, 'decisions': decisions, 'evidence': evidence})
    print(f"{task['title']}\n  {task['id']} · {LABELS.get(task['status'], task['status'])} · created {local(task['created_at'])}"
          + (f" · until {local(task['snoozed_until'])}" if task.get('snoozed_until') else ''))
    print(f"  Accounts: {', '.join(loads(task.get('account_ids'), []))}\n")
    print(wrap(task['body'], '  '))
    if task.get('latest_feedback'):
        print('\nYou asked:\n' + wrap(task['latest_feedback'], '  '))
    if decisions:
        print('\nDecisions (recent):')
        for d in sorted(decisions, key=lambda d: d['created_at']):
            print(f"  {local(d['created_at'])} {d['choice']} via {d['source']}" + (f": {d['feedback']}" if d.get('feedback') else ''))
    if evidence:
        print(f'\nEvidence ({len(evidence)}):')
        for event in evidence:
            print_event(event, args.full)
            print()


def cmd_search(config, args):
    accounts = args.account or [a['id'] for a in config['accounts']]
    events = call(config, 'events', {'account_ids': accounts, 'query': args.query, 'limit': args.limit, 'full': True})
    if args.json:
        return out_json(events)
    if not events:
        print('Nothing found.')
    for event in events:
        print_event(event, args.full)
        print()


def cmd_events(config, args):
    request = {'account_ids': args.account, 'limit': args.limit, 'full': True}
    if args.ids:
        request['event_ids'] = [int(i) for i in args.ids.split(',') if i.strip()]
        request['limit'] = max(args.limit, len(request['event_ids']))
    if args.query:
        request['query'] = args.query
    events = call(config, 'events', request)
    if args.json:
        return out_json(events)
    for event in events:
        print_event(event, args.full)
        print()


def cmd_sources(config, args):
    connectors = call(config, 'status', {'full': True})['connectors']
    if args.json:
        return out_json(connectors)
    for c in connectors:
        state = 'ERROR ' + str(c['error']) if c.get('error') else 'ok'
        print(f"  {c['id']:<26} {c.get('service', ''):<11} {state:<10} last sync {local(c.get('last_success'))} · "
              f"{c.get('signals_24h', 0)} in 24h · {c.get('pending', 0)} not reviewed · {c.get('failures_24h', 0)} failures in 24h")


def cmd_reviews(config, args):
    status = call(config, 'status', {'full': True})
    analyses = sorted(status['analyses'], key=lambda a: a['started_at'], reverse=True)[:args.limit]
    runs = status.get('review_runs',[])[:args.limit]
    if args.json:
        return out_json({'review_health': status['review_health'], 'analyses': analyses, 'runs':runs})
    health = status['review_health']
    run = health.get('worker') or {}
    state = ('waiting for Gateway recovery; retry ' + local(run.get('retry_at'))) if run.get('status') == 'recovering' else \
            ('running since ' + local(run['started_at'])) if run.get('status') in ('starting', 'running') else \
            (f"last run {run['status']} at {local(run.get('finished_at'))}" if run else 'no run yet')
    aside = f" ({health['set_aside']} set aside)" if health.get('set_aside') else ''
    print(f"Backlog: {health.get('pending')} of {health.get('total')} signals not reviewed{aside}. "
          f"Last review saved {local(health.get('last_completed'))}. Review worker: {state}.\n")
    for a in analyses:
        print(f"  {local(a['started_at'])} {a['status']:<10} {len(loads(a['items'], []))} signals · {a.get('summary') or ''}"[:160])
    if runs:
        print('\nRecent runs:')
        for r in runs:
            outcome = f"{r['checked_count']} signal versions saved" if r.get('review_saved') else f"{r.get('pending_count',0)} still waiting" if r.get('pending_count') else 'no waiting signals from this run'
            error = r.get('error') or r.get('last_error')
            phase = r.get('error_phase') or r.get('last_error_phase')
            print(f"  {local(r['started_at'])} {r['status']} · {outcome} · {r.get('duration_seconds',0)}s"+
                  (f" · {error} ({phase or 'runtime'})" if error else ''))


def cmd_feedback(config, args):
    status = call(config, 'status', {'full': True})
    titles = {s['id']: s['title'] for s in status['suggestions']}
    decisions = sorted(status['decisions'], key=lambda d: d['created_at'], reverse=True)[:args.limit]
    if args.json:
        return out_json(decisions)
    for d in decisions:
        print(f"  {local(d['created_at'])} {d['choice']:<9} via {d['source']:<9} {titles.get(d['suggestion_id'], d['suggestion_id'])}"
              + (f"\n      “{d['feedback']}”" if d.get('feedback') else ''))


def cmd_prepare(config, args):
    body = args.body if args.body is not None else (sys.stdin.read() if args.file == '-' else Path(args.file).read_text())
    task = find_task(call(config, 'status', {'full': True}), args.id)
    result = call(config, 'prepare', {'suggestion_id': task['id'], 'body': body.strip(),
                                   **({'next_action':json.loads(Path(args.next_action).read_text())} if args.next_action else {})})
    if args.json:
        return out_json(result)
    print(f"Saved as Ready for you: {task['title']}. The Telegram card is updated and you'll get one notification.")


def cmd_raw(config, args):
    out_json(call(config, args.action, json.loads(args.args) if args.args else {}))


def parser():
    p = argparse.ArgumentParser(prog='assistant', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog='Examples:\n  assistant tasks\n  assistant show 7b61\n  assistant search "Acme"\n'
                                       '  assistant prepare 7b61 --file brief.md')
    p.add_argument('--config', default=str(assistant.DEFAULT_CONFIG), help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest='command', required=True, metavar='command')

    def add(name, help_text, func):
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.add_argument('--json', action='store_true', help='machine-readable output')
        command.set_defaults(func=func)
        return command

    c = add('tasks', 'List tasks (default: Outstanding, as on the dashboard)', cmd_tasks)
    c.add_argument('--later', action='store_true', help='snoozed tasks')
    c.add_argument('--done', action='store_true', help='done and dismissed tasks')
    c.add_argument('--outstanding', action='store_true', help=argparse.SUPPRESS)
    c.add_argument('--all', action='store_true', help='every task')
    c = add('show', 'Show one task with its decisions and evidence', cmd_show)
    c.add_argument('id', help='task id or a unique prefix')
    c.add_argument('--no-evidence', action='store_true')
    c.add_argument('--full', action='store_true', help='do not shorten long evidence')
    c = add('search', 'Full-text search over collected signals', cmd_search)
    c.add_argument('query')
    c.add_argument('--account', action='append', help='limit to an account id (repeatable; default all)')
    c.add_argument('--limit', type=int, default=20)
    c.add_argument('--full', action='store_true')
    c = add('events', 'Read signals from explicit accounts, by id or query', cmd_events)
    c.add_argument('--account', action='append', required=True, help='account id (repeatable)')
    c.add_argument('--ids', help='comma-separated event ids')
    c.add_argument('--query')
    c.add_argument('--limit', type=int, default=20)
    c.add_argument('--full', action='store_true')
    add('sources', 'Connection health per source', cmd_sources)
    c = add('reviews', 'Review backlog and recent review runs', cmd_reviews)
    c.add_argument('--limit', type=int, default=10)
    c = add('feedback', 'Recent owner decisions and written feedback', cmd_feedback)
    c.add_argument('--limit', type=int, default=20)
    c = add('prepare', 'Save a short preparation (max 1000 chars) on an accepted task', cmd_prepare)
    c.add_argument('id')
    c.add_argument('--next-action', help='JSON file containing an exact email draft or original-page link; prepares only')
    source = c.add_mutually_exclusive_group(required=True)
    source.add_argument('--body')
    source.add_argument('--file', help='file with the text, or - for stdin')
    c = add('raw', 'Call any non-owner service action with JSON args', cmd_raw)
    c.add_argument('action')
    c.add_argument('args', nargs='?', help='JSON object')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.func(load_config(args.config), args)
    except CliError as error:
        sys.exit(f'assistant: {error}')
    except BrokenPipeError:
        pass


if __name__ == '__main__':
    main()
