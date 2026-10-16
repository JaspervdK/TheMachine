"""X (Twitter): replies and mentions, and DMs, for one or more accounts signed in to the OpenClaw browser.

Read-only through integrations/x_inbox.py, which makes the same requests x.com does from a tab
of the signed-in OpenClaw browser (not X's developer API): no API key, no cookies leaving the
browser, and nothing marked as seen or read. Sign in to X once in that browser (`openclaw browser --browser-profile openclaw
open https://x.com/login`); add more accounts with X's own account switcher. One config account
per X account:

    {"id": "me-x", "service": "x", "transport": "x-inbox", "boundary": "work",
     "handle": "yourhandle", "primary": "yourhandle", "poll_seconds": 900}

`primary` is the account the browser should normally be on. To read another signed-in account
x-inbox switches to it and always switches back; since a switch changes the cookies of every
x.com tab, runs go one at a time and only switch while no other job holds the browser lease.
Optional: "active_hours": [7, 23] (local time, the default) and config "x_inbox" for the path
to the reader. Encrypted X Chat isn't read.
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

KINDS = {'x.mention': 'X', 'x.dm': 'X message'}
READER = Path(__file__).resolve().parent.parent / 'integrations/x_inbox.py'


class Source:
    def __init__(self, config, account, helpers):
        self.config, self.account, self.helpers = config, account, helpers

    def page(self, since_ms, state):
        SourceError, account = self.helpers.SourceError, self.account
        try:
            done = subprocess.run([self.config.get('x_inbox', str(READER)), 'sync', '--handle', account['handle'],
                                   '--primary', account.get('primary', account['handle']), '--since', str(int(since_ms)),
                                   '--state', json.dumps(state)], capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            raise SourceError('x_read_timeout', retry_after=900) from None
        except OSError as error:
            raise SourceError('x_reader_missing:' + type(error).__name__) from None
        if done.returncode:
            err = done.stderr.lower()
            if 'busy' in err:
                raise SourceError('x_browser_busy', retry_after=600)
            if 'signed out' in err or 'not signed in' in err:
                raise SourceError('x_signed_out', retry_after=3600)
            if 'rate limit' in err:
                raise SourceError('x_rate_limited', retry_after=1800)
            raise SourceError('x_read_failed', retry_after=900)
        try:
            data = json.loads(done.stdout)
        except ValueError:
            raise SourceError('x_invalid_json') from None
        if not isinstance(data.get('mentions'), list) or not isinstance(data.get('dms'), list) or not isinstance(data.get('state'), dict):
            raise SourceError('x_invalid_page')
        return data


def person(p):
    return (str(p.get('name') or '') + ' (@' + str(p.get('screen_name') or '?') + ')').strip()


def events(account, data, helpers):
    SourceError = helpers.SourceError
    handle = (data.get('account') or {}).get('screen_name') or account['handle']
    at = lambda ms: helpers.iso(ms / 1000)
    out = []
    for t in data['mentions']:
        if not re.fullmatch(r'\d{1,25}', str(t.get('id') or '')) or type(t.get('at')) is not int:
            raise SourceError('x_invalid_mention')
        author, reply = t.get('author') or {}, t.get('reply_to') or None
        who = person(author)
        body = str(t.get('text') or '')
        if t.get('quoted'):
            body += '\n\n[Quoting: “' + str(t['quoted']) + '”]'
        if reply and reply.get('text'):
            body += '\n\n[In reply to @' + str(reply.get('screen_name') or '?') + ': “' + str(reply['text']) + '”]'
        out.append({'account_id': account['id'], 'source_id': hashlib.sha256(('x:' + t['id']).encode()).hexdigest(), 'kind': 'x.mention',
                    'occurred_at': at(t['at']), 'title': (who + (' replied to @' if reply and reply.get('to_account') else ' mentioned @') + handle)[:1000],
                    'body': body[:40000], 'deleted': False,
                    'metadata': {'from': who, 'sender_display': who, 'screen_name': author.get('screen_name'), 'author_id': author.get('id'),
                                 'followers': author.get('followers'), 'verified': author.get('verified') is True, 'account_handle': handle,
                                 'reply': bool(reply), 'reply_to_account': bool(reply and reply.get('to_account')),
                                 'in_reply_to_id': reply and reply.get('id'), 'in_reply_to': reply and reply.get('screen_name'),
                                 'conversation_id': t.get('conversation_id'), 'tweet_id': t['id'], 'lang': t.get('lang'),
                                 'source_url': 'https://x.com/' + urllib.parse.quote(str(author.get('screen_name') or 'i')) + '/status/' + t['id']}})
    for m in data['dms']:
        if not re.fullmatch(r'\d{1,25}', str(m.get('id') or '')) or type(m.get('at')) is not int or type(m.get('from_me')) is not bool:
            raise SourceError('x_invalid_message')
        sender = m.get('from') or {}
        who = 'You' if m['from_me'] else person(sender)
        name = str(m.get('with') or 'X conversation')
        out.append({'account_id': account['id'], 'source_id': hashlib.sha256(('x-dm:' + m['id']).encode()).hexdigest(), 'kind': 'x.dm',
                    'occurred_at': at(m['at']), 'title': name[:1000], 'body': str(m.get('text') or '')[:40000], 'deleted': False,
                    'metadata': {'chat_name': name, 'conversation_id': m.get('conversation_id'), 'message_id': m['id'], 'from_me': m['from_me'],
                                 'from': who, 'sender_display': who, 'sender_id': sender.get('id'), 'screen_name': sender.get('screen_name'),
                                 'group': m.get('group') is True, 'request': m.get('request') is True, 'account_handle': handle,
                                 'source_url': 'https://x.com/messages/' + urllib.parse.quote(str(m.get('conversation_id') or ''))}})
    return list({e['source_id']: e for e in out}.values())


def sync(store, config, account, helpers, source=None):
    # Browser reads: keep them to waking hours.
    current = dt.datetime.fromisoformat(helpers.now())
    start, end = account.get('active_hours', [7, 23])
    if not start <= current.astimezone(helpers.local_tz()).hour < end:
        return {'mode': 'incremental', 'events': 0}
    source = source or Source(config, account, helpers)
    state = store.checkpoint(account['id']) or {}
    since = state.get('since_ms') or int((current - dt.timedelta(days=int(config.get('bootstrap_days', 7)))).timestamp() * 1000)
    data = source.page(since, state.get('page') or {})
    found = events(account, data, helpers)
    state.update(mode='incremental', since_ms=since, page=data['state'], read_items=len(found), coverage_complete=True,
                 coverage='Mentions, replies and DM inbox caught up for @' + account['handle'] + '.')
    store.commit_page(account['id'], state, found)
    return {'mode': 'incremental', 'events': len(found)}
