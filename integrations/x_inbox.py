#!/usr/bin/env python3
"""x-inbox: X (Twitter) mentions, replies and DMs through the signed-in OpenClaw browser.

Calls X's own web API from inside a tab of the openclaw Chrome profile, so no cookies leave
the browser. The tab sits on x.com/robots.txt, so nothing opens in the UI, and nothing is
marked as read or seen. Reading never posts, likes, follows or sends anything.

Several accounts can be signed in at once (X's account switcher). To read another one the
script switches to it and always switches back to the primary account afterwards, also after
an error.

  x-inbox accounts [--json]                          (signed-in accounts)
  x-inbox mentions --handle H [--primary P] [--limit N] [--json]
  x-inbox dms --handle H [--primary P] [--limit N] [--json]
  x-inbox sync --handle H --primary P --since MS --state JSON   (JSON for the personal assistant)

Encrypted X Chat conversations aren't read; only the DM inbox is. Holds the shared browser
lease (integrations/browser-lease.py) while it runs and always closes its own tab. Used by
connectors/x.py.
"""
import argparse, fcntl, json, re, subprocess, sys, time, uuid, os
from datetime import datetime
from pathlib import Path

LEASE = Path(__file__).resolve().parent / 'browser-lease.py'
OWNER = 'x-inbox-' + str(os.getpid()) + '-' + uuid.uuid4().hex[:6]
PROFILE = os.environ.get('X_INBOX_BROWSER_PROFILE', 'openclaw')
NEUTRAL_URL = 'https://x.com/robots.txt'
# An account switch changes the cookies of every x.com tab in that browser, so x-inbox runs go
# one at a time, and a run that has to switch waits while another job holds the browser.
RUN_LOCK = Path.home() / '.local/state/x-inbox/run.lock'
HARMLESS = ('x-inbox-', 'linkedin-inbox-')  # readers that never use x.com as the owner

JS = r"""
async () => {
  const P = __PARAMS__;
  if (location.host !== 'x.com' || document.readyState === 'loading') return {error: 'not-ready'};
  const cookie = (n) => [...document.cookie.matchAll(new RegExp('(?:^|; )' + n + '=([^;]+)', 'g'))].map(m => m[1]).pop();
  if (!cookie('ct0') || !cookie('twid')) return {error: 'signed-out'};
  const me = () => decodeURIComponent(cookie('twid') || '').replace(/"/g, '').replace('u=', '');
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const fail = (code, extra) => Object.assign(Error(code), {code, ...extra});
  // The web app's public client token, read from its own script so a rotation doesn't break us.
  const html = await (await fetch('/', {credentials: 'include'})).text();
  const main = (html.match(/https:\/\/abs\.twimg\.com\/responsive-web\/client-web[^"]*\/main\.[a-z0-9]+\.js/) || [])[0];
  const bearer = main && ((await (await fetch(main)).text()).match(/"(AAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]+)"/) || [])[1];
  if (!bearer) return {error: 'query-failed', detail: 'web client token not found'};
  const headers = () => ({authorization: 'Bearer ' + bearer, 'x-csrf-token': cookie('ct0'), 'x-twitter-active-user': 'yes', 'x-twitter-auth-type': 'OAuth2Session'});
  const call = async (path, opts = {}) => {
    for (let i = 0; ; i++) {
      const r = await fetch(path, {credentials: 'include', ...opts, headers: {...headers(), ...(opts.headers || {})}, signal: AbortSignal.timeout(15000)});
      if (r.ok) return r.json();
      // Right after an account switch the new csrf cookie takes a moment to apply.
      if ((r.status === 403 || r.status === 401) && i < 4) { await sleep(1500); continue; }
      if (r.status === 429) throw fail('rate-limited');
      if (r.status === 401) throw fail('signed-out');
      throw fail('query-failed', {status: r.status});
    }
  };
  const accounts = ((await call('/i/api/1.1/account/multi/list.json')).users || []).map(u => ({id: String(u.user_id), screen_name: u.screen_name, name: u.name}));
  if (P.mode === 'accounts') return {accounts, active: me()};
  const find = (h) => accounts.find(a => a.screen_name.toLowerCase() === String(h || '').replace('@', '').toLowerCase());
  const target = find(P.handle), primary = find(P.primary) || accounts.find(a => a.id === me());
  if (!target) return {error: 'not-signed-in', accounts: accounts.map(a => a.screen_name)};
  const switchTo = async (id) => {
    if (me() === id) return;
    await call('/i/api/1.1/account/multi/switch.json', {method: 'POST', headers: {'content-type': 'application/x-www-form-urlencoded'}, body: 'user_id=' + id});
    for (let i = 0; i < 20 && me() !== id; i++) await sleep(250);
    if (me() !== id) throw fail('switch-failed');
  };
  const newer = (a, b) => a.length !== b.length ? a.length > b.length : a > b;
  const expand = (t) => {
    let text = t.full_text || t.text || '';
    for (const u of (t.entities && t.entities.urls) || []) if (u.url && u.expanded_url) text = text.split(u.url).join(u.expanded_url);
    for (const m of (t.extended_entities || t.entities || {}).media || []) if (m.url) text = text.split(m.url).join('').trim() + ' [' + (m.type || 'media') + ']';
    return text.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim();
  };
  const out = {account: {id: target.id, screen_name: target.screen_name}, mentions: [], dms: [], state: {...(P.state || {})}};
  try {
    await switchTo(target.id);
    // Mentions and replies: the Notifications → Mentions tab. Reading doesn't mark them seen.
    let cursor = null, sinceId = String(P.state.mention_since_id || '0'), top = sinceId;
    for (let page = 0; page < 4; page++) {
      const j = await call('/i/api/2/notifications/mentions.json?tweet_mode=extended&include_ext_alt_text=true&count=40' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
      const tweets = (j.globalObjects || {}).tweets || {}, users = (j.globalObjects || {}).users || {};
      let oldest = Infinity, reachedKnown = false;
      for (const t of Object.values(tweets)) {
        const mentioned = t.in_reply_to_user_id_str === target.id || ((t.entities || {}).user_mentions || []).some(m => m.id_str === target.id);
        if (t.user_id_str === target.id || !mentioned) continue;
        const at = Date.parse(t.created_at); oldest = Math.min(oldest, at);
        if (!newer(t.id_str, sinceId)) { reachedKnown = true; continue; }
        if (at < P.since) continue;
        if (newer(t.id_str, top)) top = t.id_str;
        const u = users[t.user_id_str] || {}, parent = t.in_reply_to_status_id_str && tweets[t.in_reply_to_status_id_str];
        out.mentions.push({id: t.id_str, at, text: expand(t), lang: t.lang, conversation_id: t.conversation_id_str,
          author: {id: t.user_id_str, name: u.name || '', screen_name: u.screen_name || '', followers: u.followers_count || 0, verified: !!(u.verified || u.ext_is_blue_verified)},
          reply_to: t.in_reply_to_status_id_str ? {id: t.in_reply_to_status_id_str, screen_name: t.in_reply_to_screen_name || '', to_account: t.in_reply_to_user_id_str === target.id,
            text: parent ? expand(parent).slice(0, 600) : null} : null,
          quoted: t.quoted_status_id_str && tweets[t.quoted_status_id_str] ? expand(tweets[t.quoted_status_id_str]).slice(0, 600) : null});
      }
      const entries = JSON.stringify(j.timeline || {});
      cursor = (entries.match(/"cursor":\{"value":"([^"]+)","cursorType":"Bottom"/) || [])[1];
      // Stop once a page reaches what was read before or the start of the window.
      if (!cursor || !Object.keys(tweets).length || reachedKnown || oldest < P.since) break;
    }
    out.state.mention_since_id = top;
    // DMs: the DM inbox (not X Chat, which is end-to-end encrypted). Reading doesn't mark anything read.
    const q = 'include_groups=true&include_inbox_timelines=true&dm_users=true&include_conversation_info=true&count=50';
    let box = null;
    if (P.state.dm_cursor) {
      try { box = (await call('/i/api/1.1/dm/user_updates.json?' + q + '&cursor=' + encodeURIComponent(P.state.dm_cursor))).user_events; } catch (e) { if (e.code !== 'query-failed') throw e; }
    }
    if (!box) box = (await call('/i/api/1.1/dm/inbox_initial_state.json?' + q)).inbox_initial_state;
    const dmUsers = box.users || {}, convs = box.conversations || {};
    const person = (id) => { const u = dmUsers[id] || {}; return {id, name: u.name || '', screen_name: u.screen_name || ''}; };
    for (const e of box.entries || []) {
      const m = e.message && e.message.message_data;
      if (!m || +m.time < P.since) continue;
      const c = convs[e.message.conversation_id] || {};
      const others = (c.participants || []).map(p => p.user_id).filter(id => id !== target.id);
      out.dms.push({id: m.id, at: +m.time, conversation_id: e.message.conversation_id, from_me: m.sender_id === target.id, from: person(m.sender_id),
        with: c.name || others.map(id => person(id).name || '@' + person(id).screen_name).join(', ') || person(m.sender_id === target.id ? m.recipient_id : m.sender_id).name,
        group: c.type === 'GROUP_DM', request: c.trusted === false, text: expand(m) + (m.attachment ? (m.text ? ' ' : '') + '[attachment]' : '')});
    }
    if (box.cursor) out.state.dm_cursor = box.cursor;
  } catch (e) {
    out.error = e.code || 'query-failed'; out.status = e.status; out.detail = String(e.message || e).slice(0, 200);
  } finally {
    try { await switchTo((primary || target).id); } catch (e) { out.switch_back = 'failed'; }
  }
  return out;
}
"""


class Fail(Exception):
    pass


def oc(*args, timeout=60):
    r = subprocess.run(['openclaw', 'browser', '--browser-profile', PROFILE, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise Fail('browser command failed: ' + (r.stderr or r.stdout).strip()[:300])
    return r.stdout


def evaluate(target, js, timeout_ms=90000):
    out = oc('evaluate', '--target-id', target, '--timeout-ms', str(timeout_ms), '--fn', js, timeout=timeout_ms // 1000 + 30)
    start = out.find('{') if out.lstrip().startswith('{') else out.find('\n{') + 1
    try:
        return json.loads(out[start:])
    except ValueError:
        raise Fail('unexpected browser output: ' + out.strip()[:200])


def lease(action):
    args = ['python3', str(LEASE), action, '--owner', OWNER] + ([] if action == 'release' else ['--minutes', '10'])
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def one_at_a_time(wait=180):
    RUN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = RUN_LOCK.open('a')
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.time() > deadline:
                handle.close()
                raise Fail('the OpenClaw browser is busy (another x-inbox run); try again later')
            time.sleep(2)


def other_holders():
    status = lease('status').stdout.strip()
    return [] if status == 'free' else [o for o in (part.split(' ')[0] for part in status.split(', ')) if o and o != OWNER and not o.startswith(HARMLESS)]


def with_tab(url, fn, switching=False):
    claim = lease('claim')
    if claim.returncode == 3:
        raise Fail('the OpenClaw browser is busy (' + lease('status').stdout.strip() + '); try again later')
    if claim.returncode:
        raise Fail('could not claim the browser lease: ' + (claim.stderr or claim.stdout).strip()[:200])
    label = 'x-inbox-' + uuid.uuid4().hex[:6]
    opened, target = False, label
    try:
        busy = switching and other_holders()
        if busy:
            raise Fail('the OpenClaw browser is busy (' + ', '.join(busy) + '); not switching X accounts now')
        receipt = oc('open', '--label', label, url)
        opened = True
        match = re.search(r'^(?:id|tab):\s*(\S+)', receipt, re.M)
        if match:
            target = match[1]
        return fn(target)
    finally:
        if opened:
            try:
                oc('close', target, timeout=30)
            except Exception as e:  # never leave our tab behind silently
                print('warning: could not close tab ' + label + ': ' + str(e), file=sys.stderr)
        lease('release')


def run(mode, **params):
    js = JS.replace('__PARAMS__', json.dumps({'mode': mode, 'state': {}, 'since': 0, **params}))

    def go(target):
        for _ in range(10):
            res = evaluate(target, js)
            if res.get('error') != 'not-ready':
                return res
            time.sleep(1)
        raise Fail('the X tab did not load in time; try again')
    lock = one_at_a_time()
    try:
        switching = mode == 'sync' and str(params.get('handle', '')).lower() != str(params.get('primary', '')).lower()
        res = with_tab(NEUTRAL_URL, go, switching)
    finally:
        lock.close()
    err = res.get('error')
    if err == 'signed-out':
        raise Fail('X is signed out in the OpenClaw browser; sign in there once and retry')
    if err == 'not-signed-in':
        raise Fail('@' + str(params.get('handle')) + ' is not signed in in the OpenClaw browser (signed in: ' + ', '.join(res.get('accounts') or []) + ')')
    if err == 'rate-limited':
        raise Fail('X rate limit reached; try again later')
    if err:
        raise Fail('X read failed: ' + str(res.get('detail') or err) + (' (HTTP ' + str(res['status']) + ')' if res.get('status') else ''))
    if res.get('switch_back') == 'failed':
        print('warning: could not switch the browser back to @' + str(params.get('primary')), file=sys.stderr)
    return res


def when(ms):
    d = datetime.fromtimestamp(ms / 1000).astimezone()
    today = datetime.now().astimezone().date()
    return d.strftime('%H:%M') if d.date() == today else d.strftime('%a %H:%M') if (today - d.date()).days < 7 else d.strftime('%Y-%m-%d')


def one_line(s, n):
    s = ' '.join((s or '').split())
    return s if len(s) <= n else s[:n - 1] + '…'


def week_ago():
    return int((time.time() - 7 * 86400) * 1000)


def main():
    p = argparse.ArgumentParser(description='X mentions, replies and DMs through the OpenClaw browser (read-only).')
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('accounts'); a.add_argument('--json', action='store_true')
    for name in ('mentions', 'dms'):
        s = sub.add_parser(name); s.add_argument('--handle', required=True); s.add_argument('--primary', required=True)
        s.add_argument('--limit', type=int, default=20); s.add_argument('--json', action='store_true')
    s = sub.add_parser('sync'); s.add_argument('--handle', required=True); s.add_argument('--primary', required=True)
    s.add_argument('--since', type=int, required=True); s.add_argument('--state', default='{}')
    a = p.parse_args()
    try:
        if a.cmd == 'accounts':
            res = run('accounts')
            if a.json:
                print(json.dumps(res, ensure_ascii=False))
            else:
                for x in res['accounts']:
                    print(('* ' if x['id'] == res['active'] else '  ') + '@' + x['screen_name'] + '  ' + x['name'])
        elif a.cmd == 'sync':
            print(json.dumps(run('sync', handle=a.handle, primary=a.primary, since=a.since, state=json.loads(a.state)), ensure_ascii=False))
        else:
            res = run('sync', handle=a.handle, primary=a.primary, since=week_ago())
            items = sorted(res['mentions' if a.cmd == 'mentions' else 'dms'], key=lambda x: -x['at'])[:a.limit]
            if a.json:
                print(json.dumps(items, ensure_ascii=False))
            for x in [] if a.json else items:
                who = 'You' if x.get('from_me') else (x.get('author') or x.get('from') or {}).get('name') or '?'
                where = (' in ' + x['with']) if a.cmd == 'dms' else ''
                print(f"{when(x['at']):>9}  {who}{where}: {one_line(x['text'], 120)}")
    except Fail as e:
        print('x-inbox: ' + str(e), file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print('x-inbox: the browser did not answer in time', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
