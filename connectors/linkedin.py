"""LinkedIn messages, for the account signed in to the OpenClaw browser.

Read-only through integrations/linkedin_inbox.py, which calls LinkedIn's own messaging API from a
tab of the signed-in OpenClaw browser parked on linkedin.com/robots.txt: no API key, no cookies
leaving the browser, and nothing marked as read. Sign in to LinkedIn once in that browser
(`openclaw browser --browser-profile openclaw open https://www.linkedin.com/login`). One account:

    {"id": "me-linkedin", "service": "linkedin", "transport": "linkedin-inbox", "boundary": "work:Acme",
     "poll_seconds": 3600}

LinkedIn has no API for this, so reads stay rare and in waking hours. Optional: "active_hours":
[7, 22] (local time, the default) and config "linkedin_inbox" for the path to the reader. Replies
go out only as an owner-approved `linkedin` next action (next_actions.LinkedInTransport).
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

KINDS = {'linkedin.message': 'LinkedIn'}
# For `assistant connect linkedin`: the account it adds (see docs/adding-a-source.md).
SETUP = {'label': 'LinkedIn', 'what': 'Your LinkedIn messages.',
         'account': {'id': 'me-linkedin', 'service': 'linkedin', 'transport': 'linkedin-inbox', 'boundary': 'work', 'poll_seconds': 3600},
         'note': 'Sign in to LinkedIn once in the OpenClaw browser first: openclaw browser --browser-profile openclaw open https://www.linkedin.com/login'}
READER = Path(__file__).resolve().parent.parent / 'integrations/linkedin_inbox.py'


class Source:
    def __init__(self, config, account, helpers):
        self.config, self.account, self.helpers = config, account, helpers

    def run(self, command, timeout=240):
        SourceError = self.helpers.SourceError
        if command[:1] not in (['sync'], ['verify']):
            raise SourceError('unsupported_linkedin_method')
        try:
            result = subprocess.run([self.config.get('linkedin_inbox', str(READER)), *command], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SourceError('linkedin_read_timeout', retry_after=900) from None
        except OSError as error:
            raise SourceError('linkedin_reader_missing:' + type(error).__name__) from None
        if result.returncode:
            err = result.stderr.lower()
            if 'busy' in err:
                raise SourceError('linkedin_browser_busy', retry_after=600)
            if 'signed out' in err:
                raise SourceError('linkedin_signed_out', retry_after=3600)
            raise SourceError('linkedin_read_failed', retry_after=900)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise SourceError('linkedin_invalid_json') from None

    def page(self, since_ms, state):
        data = self.run(['sync', '--since', str(int(since_ms)), '--state', json.dumps(state)])
        if not isinstance(data.get('messages'), list) or not isinstance(data.get('state'), dict):
            raise self.helpers.SourceError('linkedin_invalid_page')
        return data


def thread_url(conversation):
    match = re.search(r',([^,()]+)\)$', conversation or '')
    return 'https://www.linkedin.com/messaging/thread/' + urllib.parse.quote(match[1]) + '/' if match else 'https://www.linkedin.com/messaging/'


def event(account, raw, helpers):
    urn, conversation, at = raw.get('urn'), raw.get('conversation'), raw.get('at')
    if (not isinstance(urn, str) or not urn.startswith('urn:li:msg_message:') or not isinstance(conversation, str)
            or not conversation.startswith('urn:li:msg_conversation:') or isinstance(at, bool) or not isinstance(at, int)
            or type(raw.get('fromMe')) is not bool):
        raise helpers.SourceError('linkedin_invalid_message')
    body = raw.get('text') if isinstance(raw.get('text'), str) else ''
    if raw.get('subject'):
        body = 'Subject: ' + str(raw['subject']) + '\n\n' + body
    name = str(raw.get('with') or 'LinkedIn conversation')
    sender = 'You' if raw['fromMe'] else str(raw.get('from') or 'Unknown sender')
    metadata = {'chat_name': name, 'conversation_id': conversation, 'message_id': urn, 'from_me': raw['fromMe'],
                'from': sender, 'sender_display': sender, 'headline': raw.get('headline') or None,
                'group': raw.get('group') is True, 'categories': [str(c) for c in raw.get('categories') or []][:6],
                'source_url': thread_url(conversation), 'truncated': len(body) > 40000,
                'transport': 'linkedin_inbox_browser',
                'coverage': 'Paged LinkedIn inbox conversations and messages; read without marking them read.'}
    return {'account_id': account['id'], 'source_id': hashlib.sha256(urn.encode()).hexdigest(), 'kind': 'linkedin.message',
            'occurred_at': helpers.iso(at / 1000), 'title': name[:1000], 'body': body[:40000], 'metadata': metadata, 'deleted': False}


def events(account, data, helpers):
    return list({e['source_id']: e for e in (event(account, raw, helpers) for raw in data['messages'])}.values())


def sync(store, config, account, helpers, source=None):
    # Browser reads with no official API: keep them rare and in waking hours.
    current = dt.datetime.fromisoformat(helpers.now())
    start, end = account.get('active_hours', [7, 22])
    if not start <= current.astimezone(helpers.local_tz()).hour < end:
        return {'mode': 'incremental', 'events': 0}
    source = source or Source(config, account, helpers)
    state = store.checkpoint(account['id']) or {}
    # A scan freezes its lower bound until every conversation and message page has committed,
    # however long an outage lasts; only a completed scan moves it (with an hour of overlap).
    first = int((current - dt.timedelta(days=int(config.get('bootstrap_days', 7)))).timestamp() * 1000)
    since = state.get('scan_since_ms') or (max(0, state['completed_ms'] - 3600000) if state.get('completed_ms') else min(state.get('since_ms') or first, first))
    page = source.page(since, state.get('page') or {})
    found = events(account, page, helpers)
    latest = max([state.get('latest_ms', 0)] + [int(dt.datetime.fromisoformat(e['occurred_at']).timestamp() * 1000) for e in found])
    scan_until = state.get('scan_until_ms') or int(current.timestamp() * 1000)
    more = page.get('more') is True
    state.update(mode='incremental', latest_ms=latest, since_ms=since, read_messages=len(found), page=page['state'],
                 scan_since_ms=since if more else None, scan_until_ms=scan_until if more else None,
                 completed_ms=state.get('completed_ms', 0) if more else scan_until, coverage_complete=not more,
                 coverage='Paged inbox catch-up in progress.' if more else 'Inbox conversations and message pages caught up; spam excluded.')
    store.commit_page(account['id'], state, found)
    return {'mode': 'incremental', 'events': len(found), 'more': more}
