"""Hand to Claude Code / Hand to Codex where the CLI isn't the owner's own computer (agent_jobs.py, docs/agents.md).

- Claude (`remote`): the brief goes as a message to the owner's own Claude Code session, `claude -p <message> --cloud
  <session id>`. That session runs on their paired computer or in Anthropic's cloud, under their own Claude sign-in.
  It can't reach this Machine, so it hands the result back through The Machine's claude.ai connector: one tool,
  `report_result`, with a ticket made here for this job only. The Network keeps only the ticket's sha256 and the
  sealed result until this Machine fetches it (`/api/fleet/agent-results`).
- Codex (`cloud`): `codex cloud exec --env <id>`, followed with `codex cloud list --json` and, once it is ready,
  `codex cloud diff`. It runs in Codex cloud, under the owner's own ChatGPT sign-in.
- Codex on the owner's own computer (`device`): the hosted Machine registers a ticket's sha256 on the Network with the
  brief sealed end to end for that one computer (`dispatch`): network_crypto.seal, for the Network keys the computer
  published, signed with this Machine's own, bound to sender, computer and ticket. The Network relays the envelope and
  can't read it. The self-hosted Machine the owner paired on their computer asks for work with its own device token
  (serve: `jobs`), opens the brief with its private key (network-keys.json, never sent anywhere) after checking it was
  signed by its owner's hosted Machine, runs it there as an ordinary local job (a thread in the Codex app on that
  computer, under the Codex sign-in it has there), and reports progress and the result into that ticket (`progress`,
  `done`). Those are sealed the other way, for the hosted Machine's keys and signed by the computer's, so the Network
  carries the result and the progress lines without reading them; the hosted Machine opens them and fetches it like a
  Claude result. Each side pins the other's keys the first time it sees them (agent_key_pins); when they change,
  nothing is sealed or opened until the computer is paired again, and the dashboard says so (rekey).

None of them keeps, reads or moves a Claude or ChatGPT credential: the CLIs hold their own, on their own computer.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import subprocess
import threading
import time

import agent_jobs as aj
import network_crypto as nc

LOG = logging.getLogger('personal-assistant')
SESSION = re.compile(r'(?:session_|cse_)[A-Za-z0-9]{8,64}')
CODEX_TASK = re.compile(r'\btask_[a-z]_[0-9a-f]{16,64}\b')
REMOTE_DAYS = 2           # a remote Claude job waits this long for its result
POLL = 20                 # seconds between fetches while a remote job waits
CODEX_POLL = 15
SERVE_POLL = 30           # seconds between a paired computer's asks for Codex jobs
READY_TTL = 600           # seconds a paired computer trusts its `codex login status`
HASH = re.compile(r'[0-9a-f]{64}')
DONE = ('ready', 'applied')
FAILED = ('error', 'failed', 'cancelled')
PIN_SCHEMA = 'CREATE TABLE IF NOT EXISTS agent_key_pins(slot TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,first_at TEXT NOT NULL)'
# What the pins keep besides the fingerprint: the keys (to seal for them later) and when other keys showed up.
PIN_COLUMNS = (('keys', 'TEXT'), ('changed_at', 'TEXT'))
# A hosted Machine's own jobs on the owner's computer: which computer it sealed for, and its own key slot (`me`), so
# what comes back opens only as from that computer to this Machine for that ticket.
SENT_SCHEMA = 'CREATE TABLE IF NOT EXISTS agent_device_sent(token_hash TEXT PRIMARY KEY,device TEXT NOT NULL,me TEXT NOT NULL,created_at TEXT NOT NULL)'
RESULT_MAX = 140_000      # bytes of a sealed result: sealed and encoded it fits the Network's request limit
REPAIR = 'Its keys changed since it was paired. Pair it again on your Account page, then try again.'


def own_keys(jobs):
    """This Machine's Network keys (network-keys.json next to its database, made here once), or None."""
    import network
    path = network.keys_path(jobs.config)
    if not path:
        return None
    try:
        return nc.load_or_make(path)
    except (OSError, nc.SealError) as error:
        LOG.warning('agent keys error=%s', type(error).__name__)
        return None


def _pins(db):
    db.execute(PIN_SCHEMA)
    have = {r[1] for r in db.execute('PRAGMA table_info(agent_key_pins)')}
    for name, kind in PIN_COLUMNS:
        if name not in have:
            db.execute(f'ALTER TABLE agent_key_pins ADD COLUMN {name} {kind}')


def pinned(jobs, slot, public):
    """The other side's keys, checked (box key signed by its sign key) and pinned the first time: the keys, None when
    they don't check out, False when they differ from the pinned ones (kept as changed, for the dashboard)."""
    checked = nc.check_keys(public) if public else None
    if not checked:
        return None
    with jobs.connect() as db:
        _pins(db)
        row = db.execute('SELECT fingerprint FROM agent_key_pins WHERE slot=?', (slot,)).fetchone()
        if not row:
            db.execute('INSERT INTO agent_key_pins(slot,fingerprint,first_at,keys) VALUES(?,?,?,?)',
                       (slot, checked['fingerprint'], aj.now(), json.dumps(public)))
            return public
        if row['fingerprint'] != checked['fingerprint']:
            db.execute('UPDATE agent_key_pins SET changed_at=COALESCE(changed_at,?) WHERE slot=?', (aj.now(), slot))
            LOG.warning('agent keys changed slot=%s', slot[:40])
            return False
        db.execute('UPDATE agent_key_pins SET keys=? WHERE slot=? AND keys IS NULL', (json.dumps(public), slot))
    return public


def pin_keys(jobs, slot):
    """The keys pinned for a slot, unless other keys showed up since (then None: nothing is sealed or opened)."""
    with jobs.connect() as db:
        _pins(db)
        row = db.execute('SELECT keys, changed_at FROM agent_key_pins WHERE slot=?', (slot,)).fetchone()
    if not row or row['changed_at'] or not row['keys']:
        return None
    try:
        return json.loads(row['keys'])
    except ValueError:
        return None


def rekeyed(jobs, device_ids):
    """Which of these computers showed other keys than the ones this Machine pinned: they need pairing again."""
    if not device_ids:
        return set()
    with jobs.connect() as db:
        _pins(db)
        rows = db.execute(f'SELECT slot FROM agent_key_pins WHERE changed_at IS NOT NULL AND slot IN ({",".join("?" * len(device_ids))})',
                          ['to:' + str(d) for d in device_ids]).fetchall()
    return {r['slot'][3:] for r in rows}


def result_context(device, me, digest, what='codex-result'):
    """What a sealed result (or progress line, `codex-progress`) is bound to: from that computer, to this hosted
    Machine, for this ticket."""
    return {'p': what, 'k': 'codex', 'f': device, 't': me, 'r': digest}


def job_context(sender, device, digest):
    """What a sealed brief is bound to: from this hosted Machine, for that computer, for this ticket."""
    return {'p': 'codex-job', 'k': 'codex', 'f': sender, 't': device, 'r': digest}


def runner(row, command, own, jobs):
    if row['tool'] == 'claude':
        if not own.get('session'):
            raise aj.Stopped('missing', 'Pair a Claude Code session first')
        return RemoteClaude(command, own, jobs)
    if not own.get('env'):
        raise aj.Stopped('missing', 'Choose a Codex cloud environment first')
    return CloudCodex(command, own, jobs)


def session_id(value):
    """A Claude Code session id from what the owner pasted: the id itself or its claude.ai/code link."""
    found = SESSION.search(str(value or ''))
    return found.group(0) if found else None


def brief(prompt, ticket, mcp_url):
    return (f"{prompt.rstrip()}\n\n---\nThis job comes from The Machine. When you are done, call the `report_result` tool "
            f"of The Machine connector once, with ticket `{ticket}`, a short markdown summary, the finished text in `draft` "
            f"if the job asked for a message, and links such as a pull request. If that tool is not there, say so and "
            f"ask the owner to add the connector in claude.ai (Settings, Connectors, Add custom connector: {mcp_url}).")


def _run(argv, cwd, env, timeout=120):
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    said = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', (out.stdout or '') + '\n' + (out.stderr or ''))
    return out.returncode, said


def _last_line(said, fallback):
    lines = [x.strip() for x in said.splitlines() if x.strip()]
    return (lines[-1] if lines else fallback)[:300]


class RemoteClaude:
    def __init__(self, command, own, jobs):
        self.command, self.own, self.jobs = command, own, jobs
        self.config_dir = own.get('config_dir') if isinstance(own.get('config_dir'), str) else None

    def run(self, job, prompt, resume, folder, transcript, timeout, on_progress, on_session, procs):
        session = session_id(self.own.get('session'))
        if not session:
            raise aj.Stopped('missing', 'The paired Claude Code session id is not valid')
        ticket = secrets.token_urlsafe(24)
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        try:
            made = self.jobs.network({'action': 'register', 'ticket_hash': digest, 'job': job['id']}) or {}
        except Exception as error:  # noqa: BLE001 - the Network's own message, never a secret
            raise aj.Stopped('failed', f'The Network could not take the job: {error}'[:300]) from None
        with self.jobs.connect() as db:
            db.execute('UPDATE agent_jobs SET token_hash=? WHERE id=?', (digest, job['id']))
        code, said = _run([self.command, '-p', brief(prompt, ticket, made.get('mcp_url') or 'https://app.themachine.computer/mcp'),
                           '--cloud', session], folder, aj.run_env(self.config_dir))
        with open(transcript, 'a', encoding='utf-8') as log:
            log.write(said[-20000:].replace(ticket, '<ticket>'))
        if code:
            raise aj.Stopped('failed', _last_line(said.replace(ticket, '<ticket>'), f'Claude Code stopped (exit {code})'))
        on_session(session)
        watch(self.jobs)
        return session, None  # the result comes back through the connector

    def link(self, session):
        return 'https://claude.ai/code/' + session if SESSION.fullmatch(str(session or '')) else 'https://claude.ai/code'

    def resume_watch(self, row):
        watch(self.jobs)


_watching = set()


def watch(jobs):
    """One thread per AgentJobs that fetches remote results while a remote job waits."""
    with jobs.lock:
        if id(jobs) in _watching:
            return
        _watching.add(id(jobs))
    threading.Thread(target=_poll, args=(jobs,), daemon=True, name='agent-results').start()


def _waiting(jobs):
    with jobs.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM agent_jobs WHERE (place IN ('remote','device') AND status IN ('waiting','running') "
                                            "AND token_hash IS NOT NULL) OR token_hash IN (SELECT ticket_hash FROM agent_result_receipts WHERE acked=0)")]


def _poll(jobs):
    try:
        while True:
            rows = _waiting(jobs)
            if not rows:
                return
            fetch(jobs, rows)
            time.sleep(POLL)
    finally:
        with jobs.lock:
            _watching.discard(id(jobs))


def fetch(jobs, rows=None):
    rows = _waiting(jobs) if rows is None else rows
    if not rows:
        return 0
    by_hash = {r['token_hash']: r for r in rows}
    try:
        reply = jobs.network({'action': 'fetch', 'tickets': list(by_hash)}) or {}
    except Exception as error:  # noqa: BLE001 - try again on the next round
        LOG.warning('agent results fetch error=%s', type(error).__name__)
        reply = {}
    found, took = reply.get('results') or [], []
    private = own_keys(jobs) if any(r['place'] == 'device' for r in rows) else None
    for result in found:
        row = by_hash.get(result.get('ticket_hash')) if isinstance(result, dict) else None
        if row and row['place'] == 'device':
            result = opened_result(jobs, row, private, result)
        if row and result.get('failed'):
            if stopped(jobs, row, result):
                took.append(row['token_hash'])
        elif row:
            if jobs.remote_result(row['id'], result):
                took.append(row['token_hash'])
    for going in reply.get('running') or []:  # a job on the owner's computer: which one, and how far it got
        row = by_hash.get(going.get('ticket_hash')) if isinstance(going, dict) else None
        if row and row['token_hash'] not in took:
            said = opened_progress(jobs, row, private, going)
            line = f"On {str(going.get('device') or 'your computer')[:60]}" + (f": {said}" if said else '')
            jobs.progress(row['id'], line[:200], turn=row['turns'])
    with jobs.connect() as db:
        receipts = [dict(r) for r in db.execute('SELECT ticket_hash,report FROM agent_result_receipts WHERE acked=0')]
    if receipts:
        try:
            reply = jobs.network({'action': 'ack', 'receipts': receipts}) or {}
        except Exception as error:  # noqa: BLE001 - durable receipts are retried after a restart too
            LOG.warning('agent results ack error=%s', type(error).__name__)
        else:
            with jobs.connect() as db:
                db.executemany('UPDATE agent_result_receipts SET acked=1 WHERE ticket_hash=? AND report=?',
                               [(r['ticket_hash'], r['report']) for r in reply.get('acked') or []
                                if r in receipts])
    cutoff = time.time() - REMOTE_DAYS * 86400
    for row in rows:
        started = row['started_at'] or row['created_at']
        if row['token_hash'] not in took and dt.datetime.fromisoformat(started).timestamp() < cutoff:
            jobs.finish(row['id'], 'failed', error=('No result came back. Open Codex on your computer to see where it got to.'
                                                    if row['place'] == 'device' else 'No result came back. Open it in Claude to see where it got to.'),
                        error_kind='timeout', only_if=('waiting', 'running'))
    return len(took)


def _sent(jobs, digest):
    with jobs.connect() as db:
        db.execute(SENT_SCHEMA)
        return db.execute('SELECT device, me FROM agent_device_sent WHERE token_hash=?', (digest,)).fetchone()


def _open_from_device(jobs, row, private, env, what):
    """A body the owner's computer sealed for this Machine, for this job, signed by the keys pinned for that computer;
    SealError otherwise."""
    sent = _sent(jobs, row['token_hash'])
    keys = pin_keys(jobs, 'to:' + sent['device']) if sent and private else None
    if not keys:
        raise nc.SealError('no keys pinned for that computer')
    return nc.open_sealed(env, private, keys, result_context(sent['device'], sent['me'], row['token_hash'], what), limit=RESULT_MAX)


def opened_result(jobs, row, private, result):
    """A result from the owner's computer, opened: as the Network gives a connector's result ({summary, diff, draft,
    links} or {failed, error_kind, summary}) with its report number. One that doesn't open is a failure with why;
    only a computer that could not open the job sends why in the clear (error_kind 'sealed', a fixed line)."""
    keep = {'ticket_hash': result.get('ticket_hash'), 'report': result.get('report')}
    if not result.get('sealed'):
        if result.get('failed') and result.get('error_kind') == 'sealed':
            return {**keep, 'failed': True, 'error_kind': 'sealed', 'summary': str(result.get('summary') or '')[:300]}
        return {**keep, 'failed': True, 'error_kind': 'sealed', 'summary': 'The result came back unsealed, so it was dropped.'}
    try:
        body = _open_from_device(jobs, row, private, result['sealed'], 'codex-result')
    except nc.SealError:
        LOG.warning('agent result not opened ticket=%s', row['token_hash'][:12])
        return {**keep, 'failed': True, 'error_kind': 'sealed', 'summary': 'The result did not open here, so it was dropped. ' + REPAIR}
    if not isinstance(body, dict):
        return {**keep, 'failed': True, 'error_kind': 'sealed', 'summary': 'The result came back empty.'}
    links = [u for u in body.get('links') or [] if isinstance(u, str) and u.startswith('https://')][:10]
    if body.get('failed'):
        return {**keep, 'failed': True, 'error_kind': str(body.get('error_kind') or 'failed')[:40], 'summary': str(body.get('summary') or '')[:500]}
    return {**keep, 'summary': str(body.get('summary') or '')[:aj.SUMMARY_MAX], 'diff': str(body.get('diff') or '')[:aj.DIFF_MAX],
            'draft': str(body.get('draft') or '')[:aj.SUMMARY_MAX], 'links': links}


def opened_progress(jobs, row, private, going):
    """A progress line from the owner's computer, opened, or '' (none yet, or it didn't open)."""
    if row['place'] != 'device' or not going.get('sealed'):
        return str(going.get('progress') or '')[:160] if row['place'] != 'device' else ''
    try:
        body = _open_from_device(jobs, row, private, going['sealed'], 'codex-progress')
    except nc.SealError:
        return ''
    return str(body.get('progress') or '')[:160] if isinstance(body, dict) else ''


def stopped(jobs, row, result):
    """The owner's computer said the job stopped there: the card says why, and the receipt is acknowledged once."""
    with jobs.connect() as db:
        previous = db.execute('SELECT report FROM agent_result_receipts WHERE ticket_hash=?', (row['token_hash'],)).fetchone()
    report_no = int(result.get('report') or 1)
    if previous and report_no <= previous['report']:
        return False
    kind = str(result.get('error_kind') or 'failed')[:40]
    error = str(result.get('summary') or 'It stopped on your computer.').strip()[:500]
    jobs.finish(row['id'], 'cancelled' if kind == 'cancelled' else 'failed', error=error, error_kind=kind,
                only_if=('waiting', 'running'), turn=row['turns'])
    with jobs.connect() as db:
        db.execute('INSERT INTO agent_result_receipts(ticket_hash,report,acked) VALUES(?,?,0) ON CONFLICT(ticket_hash) '
                   'DO UPDATE SET report=excluded.report,acked=0', (row['token_hash'], report_no))
    return True


def where(jobs):
    """Where the owner's Codex jobs go, from the Network: {'codex': 'device'|'cloud'|None, 'devices': [...]}, or None
    when it can't say now (the last answer stays)."""
    own = (aj.settings(jobs.config) or {}).get('codex')
    cloud = isinstance(own, dict) and own.get('place') == 'cloud' and bool(own.get('env'))
    try:
        found = jobs.network({'action': 'where', 'cloud_ready': cloud}) or {}
    except Exception as error:  # noqa: BLE001 - asked again later
        LOG.warning('agent where error=%s', type(error).__name__)
        return None
    devices = [{'id': str(d.get('id') or '')[:80], 'label': str(d.get('label') or '')[:60], 'codex': bool(d.get('codex')),
                'online': bool(d.get('online')), 'keys_changed': bool(d.get('keys_changed'))}
               for d in found.get('devices') or [] if isinstance(d, dict)][:10]
    mine = rekeyed(jobs, [d['id'] for d in devices if d['id']])  # this Machine saw other keys for it than it pinned
    for d in devices:
        d['keys_changed'] = d['keys_changed'] or d['id'] in mine
    return {'codex': found.get('codex') if found.get('codex') in ('device', 'cloud') else None, 'devices': devices}


class DeviceCodex:
    """A hosted Machine's Codex job for the owner's own computer: handed over the Network, nothing runs here."""
    command = None

    def __init__(self, jobs):
        self.jobs = jobs

    def target(self):
        """The owner's computer to seal for (an online one that runs Codex first) and this Machine's key slot."""
        try:
            found = self.jobs.network({'action': 'where'}) or {}
        except Exception as error:  # noqa: BLE001 - the Network's own message, never a secret
            raise aj.Stopped('failed', f'The Network could not say where Codex runs: {error}'[:300]) from None
        ready = [d for d in found.get('devices') or [] if isinstance(d, dict) and d.get('codex') and d.get('id')]
        ready.sort(key=lambda d: bool(d.get('keys_changed') or d['id'] in rekeyed(self.jobs, [d['id']])))  # one paired again first
        device = next((d for d in ready if d.get('online')), ready[0] if ready else None)
        if not device:
            raise aj.Stopped('missing', 'Set up Codex on your computer first')
        label = str(device.get('label') or 'your computer')[:60]
        if device.get('keys_changed'):
            raise aj.Stopped('failed', f'{label}: {REPAIR}')
        if not device.get('keys'):
            raise aj.Stopped('missing', f'Update The Machine on {label}, then try again.')
        keys = pinned(self.jobs, 'to:' + str(device['id']), device['keys'])
        if keys is False:
            raise aj.Stopped('failed', f'{label}: {REPAIR}')
        if not keys or not found.get('me'):
            raise aj.Stopped('failed', f'The keys of {label} do not check out. Pair it again, then try again.')
        return device, keys, str(found['me'])

    def run(self, job, prompt, resume, folder, transcript, timeout, on_progress, on_session, procs):
        ticket = secrets.token_urlsafe(24)  # only its hash leaves this Machine: no one else can claim the result
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        private = own_keys(self.jobs)
        if not private:
            raise aj.Stopped('failed', 'This Machine has no keys to seal the job with.')
        device, keys, me = self.target()
        try:
            sealed = nc.seal({'brief': prompt}, private, keys, job_context(me, device['id'], digest))
        except nc.SealError:
            raise aj.Stopped('failed', 'This job is too long to hand to your computer. Shorten it, then try again.') from None
        try:
            sent = self.jobs.network({'action': 'dispatch', 'tool': 'codex', 'job': job['id'], 'ticket_hash': digest, 'to': device['id'],
                                      'sealed': sealed, 'keys': nc.public_keys(private)}) or {}
        except Exception as error:  # noqa: BLE001 - the Network's own message, never a secret
            raise aj.Stopped('failed', f'The Network could not take the job: {error}'[:300]) from None
        with self.jobs.connect() as db:
            db.execute(SENT_SCHEMA)
            db.execute('INSERT OR REPLACE INTO agent_device_sent(token_hash,device,me,created_at) VALUES(?,?,?,?)',
                       (digest, str(device['id']), me, aj.now()))
            db.execute('UPDATE agent_jobs SET token_hash=? WHERE id=?', (digest, job['id']))
        if sent.get('device') and not sent.get('online'):
            on_progress(f"Waiting for {str(sent['device'])[:60]} to come online")
        watch(self.jobs)
        return None, None  # the result comes back from that computer through the Network

    def cancel(self, row):
        if row['token_hash']:
            try:
                self.jobs.network({'action': 'cancel', 'ticket_hash': row['token_hash']})
            except Exception as error:  # noqa: BLE001 - canceled here anyway
                LOG.warning('agent cancel error=%s', type(error).__name__)

    def link(self, session):
        return None

    def resume_watch(self, row):
        watch(self.jobs)


# ------------------------------------------------------------------ the owner's own computer: Codex jobs from their hosted Machine

RELAY_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_relayed(ticket_hash TEXT PRIMARY KEY,job_id TEXT NOT NULL,
  reported INTEGER NOT NULL DEFAULT 0,progress TEXT,created_at TEXT NOT NULL)"""
RELAY_COLUMNS = (('sender', 'TEXT'), ('me', 'TEXT'))  # who sent it (its hosted Machine's key slot) and this computer's


def _relayed(db):
    db.execute(RELAY_SCHEMA)
    have = {r[1] for r in db.execute('PRAGMA table_info(agent_relayed)')}
    for name, kind in RELAY_COLUMNS:
        if name not in have:
            db.execute(f'ALTER TABLE agent_relayed ADD COLUMN {name} {kind}')
_ready = {'at': 0.0, 'ok': False}


def codex_ready(jobs, run=None):
    """Whether this computer can take Codex jobs: Codex is here and signed in (`codex login status`, which prints no
    secret; checked every READY_TTL). The sign-in stays Codex's own: nothing of it is read or sent."""
    own = (aj.settings(jobs.config) or {}).get('codex')
    if own is False or (isinstance(own, dict) and own.get('serve') is False):
        return False
    if time.monotonic() - _ready['at'] < READY_TTL:
        return _ready['ok']
    command = aj.find('codex', own.get('command') if isinstance(own, dict) else None)
    ok = False
    if command:
        try:
            ok = (run or _run)([command, 'login', 'status'], None, aj.run_env(), 60)[0] == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _ready.update(at=time.monotonic(), ok=ok)
    return ok


def serve(jobs, stop, run=None):
    """A paired self-hosted Machine: asks the Network for its owner's Codex jobs every SERVE_POLL seconds."""
    while not stop.is_set():
        try:
            serve_once(jobs, run)
        except Exception as error:  # noqa: BLE001 - asked again on the next round
            LOG.warning('agent serve error=%s', type(error).__name__)
        stop.wait(SERVE_POLL)


def serve_once(jobs, run=None):
    with jobs.connect() as db:
        _relayed(db)
    private = own_keys(jobs)
    ready = bool(private) and codex_ready(jobs, run)
    ask = {'action': 'jobs', 'tools': ['codex'] if ready else []}
    if private:
        ask['keys'] = nc.public_keys(private)  # published once; the Network seals nothing, the hosted Machine does
    reply = jobs.network(ask) or {}
    taken = 0
    for job in reply.get('jobs') or []:
        if ready and isinstance(job, dict) and take(jobs, job, private, str(reply.get('me') or '')):
            taken += 1
    for digest in reply.get('cancelled') or []:
        with jobs.connect() as db:
            row = db.execute('SELECT job_id FROM agent_relayed WHERE ticket_hash=?', (str(digest),)).fetchone()
        if row:
            try:
                jobs.cancel(row['job_id'])
            except ValueError:
                pass  # it ended meanwhile
    report(jobs, private)
    return taken


def opened(jobs, job, private, me):
    """The brief of a job sealed for this computer, or (None, why): it must be sealed for this computer's own key, for
    this ticket, and signed by the hosted Machine whose keys this computer pinned (pairing again starts over)."""
    digest, sender = str(job.get('ticket_hash') or ''), str(job.get('from') or '')
    if not (me and sender.startswith('m:')):
        return None, 'It came without who sent it, so it was not opened.'
    keys = pinned(jobs, 'from:' + me + ':' + sender, job.get('from_keys'))
    if keys is False:
        return None, 'The keys of your Machine changed since this computer first saw them. Pair this computer again.'
    if not keys:
        return None, 'It was not signed by your Machine, so it was not opened.'
    try:
        body = nc.open_sealed(job.get('sealed'), private, keys, job_context(sender, me, digest))
    except nc.SealError:
        return None, 'It did not open on this computer, so it was not run.'
    brief = body.get('brief') if isinstance(body, dict) else None
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 60_000:
        return None, 'It came without a job to run.'
    return brief, None


def take(jobs, job, private=None, me=''):
    """One job from the owner's hosted Machine: an ordinary local Codex job here, in a folder of its own. A brief that
    doesn't open (or isn't signed by the owner's Machine) is never run: that side hears why."""
    digest = str(job.get('ticket_hash') or '')
    if job.get('tool') != 'codex' or not HASH.fullmatch(digest):
        return False
    brief, why = opened(jobs, job, private, me) if private else (None, 'This computer has no keys to open it.')
    if why:
        LOG.warning('agent job not opened ticket=%s', digest[:12])
        try:
            jobs.network({'action': 'done', 'ticket_hash': digest, 'failed': True, 'error_kind': 'sealed', 'summary': why})
        except Exception as error:  # noqa: BLE001 - that side times out instead
            LOG.warning('agent report error=%s', type(error).__name__)
        return False
    job_id, at = 'aj_' + secrets.token_hex(8), aj.now()
    with jobs.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM agent_relayed WHERE ticket_hash=?', (digest,)).fetchone():
            return False
        db.execute('INSERT INTO agent_jobs(id,suggestion_id,tool,place,status,brief,asks,turns,token_hash,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (job_id, aj.RELAYED + digest[:16], 'codex', 'local', 'sent', brief, '[]', 1, digest, at, at))
        db.execute('INSERT INTO agent_relayed(ticket_hash,job_id,created_at,sender,me) VALUES(?,?,?,?,?)',
                   (digest, job_id, at, str(job.get('from') or ''), me))
    try:
        jobs.prepare_folder(job_id, brief, {})
    except OSError as error:
        jobs.finish(job_id, 'failed', error='I could not make the job folder on this computer', error_kind='folder')
        LOG.error('agent job folder error=%s', type(error).__name__)
        return True
    jobs.launch(job_id, brief, resume=None)
    return True


CANT_SEAL = "This computer can't seal the result for your Machine, so it kept it here. Pair this computer again."


def report(jobs, private=None):
    """Tells the Network how each job from the hosted Machine is doing: its progress line while it runs, then its
    result (summary, changes, a rewritten message) or why it stopped, once. Both sealed for the hosted Machine that
    sent the job (the keys this computer pinned for it), so the Network relays them without reading them."""
    private = private or own_keys(jobs)
    with jobs.connect() as db:
        _relayed(db)
        rows = [dict(r) for r in db.execute('SELECT r.ticket_hash, r.progress AS sent, r.sender, r.me, j.* FROM agent_relayed r '
                                            'JOIN agent_jobs j ON j.id=r.job_id WHERE r.reported=0')]
    for row in rows:
        try:
            if row['status'] in ('result', 'failed', 'cancelled'):
                body = sealed_for(jobs, row, private, done_body(jobs, row), 'codex-result')
                jobs.network({'action': 'done', 'ticket_hash': row['ticket_hash'],
                              **({'sealed': body} if body else {'failed': True, 'error_kind': 'sealed', 'summary': CANT_SEAL})})
                with jobs.connect() as db:
                    db.execute('UPDATE agent_relayed SET reported=1 WHERE ticket_hash=?', (row['ticket_hash'],))
            elif row['status'] == 'running' and (row['progress'] or 'Running') != row['sent']:
                line = row['progress'] or 'Running'
                body = sealed_for(jobs, row, private, {'progress': line[:200]}, 'codex-progress')
                if body:
                    jobs.network({'action': 'progress', 'ticket_hash': row['ticket_hash'], 'sealed': body})
                with jobs.connect() as db:
                    db.execute('UPDATE agent_relayed SET progress=? WHERE ticket_hash=?', (line, row['ticket_hash']))
        except Exception as error:  # noqa: BLE001 - told on the next round; a ticket the Network forgot is not retried
            if 'not a job this computer took' in str(error):
                with jobs.connect() as db:
                    db.execute('UPDATE agent_relayed SET reported=1 WHERE ticket_hash=?', (row['ticket_hash'],))
            LOG.warning('agent report error=%s', type(error).__name__)


def sealed_for(jobs, row, private, body, what):
    """`body` sealed for the hosted Machine that sent this job, signed by this computer, or None when it can't be: no
    keys here, or that Machine's keys changed since this computer pinned them."""
    sender, me = str(row.get('sender') or ''), str(row.get('me') or '')
    keys = pin_keys(jobs, 'from:' + me + ':' + sender) if private and sender and me else None
    if not keys:
        return None
    try:
        return nc.seal(body, private, keys, result_context(me, sender, row['ticket_hash'], what), limit=RESULT_MAX)
    except nc.SealError:
        return None


def done_body(jobs, row):
    """What goes back for a finished job, before it is sealed: why it stopped, or its summary, changes and draft. The
    diff is cut to what fits (RESULT_MAX); the whole of it stays on this computer."""
    if row['status'] != 'result':
        return {'failed': True, 'error_kind': row['error_kind'] or row['status'],
                'summary': (row['error'] or 'It stopped on this computer.')[:500]}
    draft = ''
    found = jobs.work(row['id']) / aj.DRAFT_FILE
    if aj.DRAFT_FILE in [f.get('path') for f in json.loads(row['files'] or '[]')]:
        try:
            if not found.is_symlink() and found.stat().st_size <= 50_000:
                draft = found.read_text(errors='replace').strip()
        except OSError:
            draft = ''
    links = [u for u in dict.fromkeys(aj.URL.findall(row['summary'] or '')) if u.startswith('https://')][:10]
    body = {'summary': (row['summary'] or 'Done, without a summary.')[:aj.SUMMARY_MAX], 'diff': '', 'draft': draft, 'links': links}
    room = RESULT_MAX - len(json.dumps(body).encode()) - 200
    diff = (row['diff'] or '')[:aj.DIFF_MAX]
    while diff and len(json.dumps(diff).encode()) > room:
        diff = diff[:max(0, int(len(diff) * 0.9) - 100)]
    body['diff'] = diff
    return body


class CloudCodex:
    def __init__(self, command, own, jobs):
        self.command, self.own, self.jobs = command, own, jobs
        self.env = str(own.get('env'))

    def run(self, job, prompt, resume, folder, transcript, timeout, on_progress, on_session, procs):
        branch = self.own.get('branch')
        argv = [self.command, 'cloud', 'exec', '--env', self.env, *(['--branch', str(branch)] if branch else []), prompt]
        code, said = _run(argv, folder, aj.run_env())
        with open(transcript, 'a', encoding='utf-8') as log:
            log.write(said[-20000:])
        task = CODEX_TASK.search(said)
        if code or not task:
            raise aj.Stopped('failed', _last_line(said, f'Codex cloud stopped (exit {code})'))
        on_session(task.group(0))
        return self.follow(job, task.group(0), time.time(), timeout, on_progress, procs)

    def task(self, task_id):
        code, said = _run([self.command, 'cloud', 'list', '--json', '--env', self.env, '--limit', '20'], None, aj.run_env(), 60)
        try:
            tasks = json.loads(said.strip().splitlines()[0] if said.strip() else '{}').get('tasks') or []
        except (ValueError, AttributeError, IndexError):
            tasks = []
        return next((t for t in tasks if isinstance(t, dict) and t.get('id') == task_id), None)

    def follow(self, job, task_id, started, timeout, on_progress, procs):
        handle = aj.Handle(lambda: None)
        procs[job['id']] = handle
        last = None
        try:
            while True:
                if handle.cancelled:
                    raise aj.Stopped('cancelled', 'Canceled here. It may still finish in Codex cloud.')
                if time.time() - started > max(timeout, 3600):
                    raise aj.Stopped('timeout', 'Still running in Codex cloud. Open it there.')
                found = self.task(task_id) or {}
                status = str(found.get('status') or '').lower()
                if status and status != last:
                    on_progress({'pending': 'Running in Codex cloud'}.get(status, 'Codex cloud: ' + status)[:160])
                    last = status
                if status in FAILED:
                    raise aj.Stopped('failed', 'Codex cloud stopped. Open it there to see why.')
                if status in DONE:
                    code, diff = _run([self.command, 'cloud', 'diff', task_id], None, aj.run_env(), 120)
                    diff = diff.strip() + '\n' if not code and diff.strip() else ''
                    files = sorted(set(re.findall(r'^\+\+\+ b/(.+)$', diff, re.M)))[:aj.FILES_MAX]
                    title = str(found.get('title') or 'Done in Codex cloud').strip()
                    summary = f"{title}\n\n{len(files)} file{'s' if len(files) != 1 else ''} changed. Open it in Codex cloud to make a pull request."
                    self.jobs.result(job['id'], task_id, summary[:aj.SUMMARY_MAX], files=[{'path': f, 'size': 0} for f in files],
                                     diff=diff[:aj.DIFF_MAX])
                    return task_id, None  # the result is in already
                time.sleep(CODEX_POLL)
        finally:
            procs.pop(job['id'], None)

    def link(self, task_id):
        return 'https://chatgpt.com/codex/tasks/' + task_id if CODEX_TASK.fullmatch(str(task_id or '')) else 'https://chatgpt.com/codex'

    def resume_watch(self, row):
        if row['session_id'] and CODEX_TASK.fullmatch(row['session_id']):
            def go():
                try:
                    since = dt.datetime.fromisoformat(row['started_at']).timestamp() if row['started_at'] else time.time()
                    self.follow(row, row['session_id'], since, self.jobs.timeout, lambda line: self.jobs.progress(row['id'], line), self.jobs.procs)
                except aj.Stopped as stop:
                    self.jobs.finish(row['id'], 'cancelled' if stop.kind == 'cancelled' else 'failed', error=str(stop)[:500], error_kind=stop.kind)
            threading.Thread(target=go, daemon=True, name='agent-job').start()
