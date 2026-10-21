"""Skills (docs/skills.md): how-tos the owner's agent follows, kept as OpenClaw skills.

A skill is a name, a one-line description (when to use it) and instructions. The Machine keeps them in its database
and writes each one that is on to `<workspace>/skills/the-machine/<slug>/SKILL.md`, where OpenClaw finds it like any
workspace skill and refreshes its skill list by itself. That folder is The Machine's own: what is there and not on
here is removed, and nothing outside it is touched.

Where a skill comes from:
- mine: written (or started from an example) by the owner on the dashboard.
- proposed: suggested by the agent from repeated work (`assistant skills-propose`); waits for the owner's Approve.
- shared: sent by a teammate or friend over the Network (the Fleet's `skill` question); waits for the owner's Install.

Safety: a skill is instructions, nothing else. Only name, description and instructions travel and get written; no
OpenClaw metadata (no environment, install steps, tool dispatch or allowed tools), no scripts. So a skill can't widen
what this Machine may do: it runs under this Machine's own rules and approvals, like anything else the agent does. A
shared or proposed skill is installed only by the owner's own tap on the dashboard, after reading it; an update from
the same author is a new version to read and install again. Text from someone else is shown quoted and marked with
who sent it."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger('personal-assistant')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,slug TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,body TEXT NOT NULL,
  status TEXT NOT NULL,origin TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,sha TEXT NOT NULL,author TEXT,fleet TEXT,member_id TEXT,
  ask_id TEXT,their_slug TEXT,replaces TEXT,note TEXT,shared TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS skills_status ON skills(status);
CREATE TABLE IF NOT EXISTS skill_hints(id TEXT PRIMARY KEY,suggestion_id TEXT NOT NULL,skill TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
  summary TEXT,why TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
'''
STATUSES = ('on', 'off', 'proposed', 'incoming')
FOLDER = 'the-machine'          # under <workspace>/skills
MARKER = '.managed-by-the-machine'
NAME_CHARS, DESCRIPTION_CHARS, BODY_CHARS, NOTE_CHARS = 60, 300, 16000, 300
MAX_SKILLS, OPEN_PROPOSALS, OPEN_PER_SENDER = 100, 3, 5
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
SHA = re.compile(r'[0-9a-f]{64}')
FRONTMATTER = re.compile(r'\A---\s*\n(.*?)\n---\s*\n?', re.S)

EXAMPLES = {
    'customer-inquiries': {
        'name': 'Research and answer customer inquiries',
        'description': 'When a customer or prospect asks a question by email or chat: research it and draft a clear, '
                       'correct answer for the owner to send.',
        'body': '''## When to use
A customer or prospect asks something: how a feature works, whether something is possible, what something costs, why
something went wrong.

## Steps
1. Read the whole thread first, and earlier conversations with the same person or company. Note what they already
   tried and what they were told before, so the answer doesn't repeat or contradict it.
2. Find the answer in this order: the product's own help pages and docs, earlier answers to similar questions in the
   owner's sent mail and chat, then the public web. Prefer the newest source; note where each fact comes from.
3. If something can't be confirmed, say so plainly instead of guessing. Never promise dates, prices, discounts or
   features that no source confirms.
4. Draft the answer in the customer's language and tone, short: the direct answer first, then the steps or link, then
   one line offering more help. Use their name; no filler.
5. Add a note for the owner (not for the customer): the sources used and anything uncertain.

## Never
- Send by itself: the owner reviews and sends every answer.
- Share another customer's details, internal notes or prices that aren't public.
''',
    },
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def ensure(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def one_line(value, chars):
    """Text on one line: control and invisible formatting characters out, whitespace collapsed, at most chars."""
    if not isinstance(value, str):
        return ''
    value = ''.join(' ' if ord(c) < 32 or c in '  ' else c for c in value if c not in '​‎‏‪‫‬‭‮⁦⁧⁨⁩﻿')
    return ' '.join(value.split())[:chars]


def clean_body(value):
    """Instructions as markdown: line breaks kept, other control characters and a leading YAML header out."""
    if not isinstance(value, str):
        return ''
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(c for c in value if c in '\n\t' or (ord(c) >= 32 and c not in '‪‫‬‭‮⁦⁧⁨⁩﻿'))
    value = FRONTMATTER.sub('', value, count=1)
    return value.strip()


def slug_of(name):
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:48].strip('-')
    return slug or 'skill'


def digest(name, description, body):
    return hashlib.sha256(json.dumps([name, description, body], ensure_ascii=False).encode()).hexdigest()


def from_skill_md(text):
    """Name, description and instructions from a pasted SKILL.md; any other frontmatter (metadata, env, tools) is dropped."""
    found = FRONTMATTER.match(text or '')
    fields = {}
    if found:
        for line in found.group(1).splitlines():
            key, sep, value = line.partition(':')
            if sep and key.strip() in ('name', 'description'):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                fields[key.strip()] = value
    return fields.get('name', ''), fields.get('description', ''), clean_body(text)


def validate(name, description, body):
    name, description, body = one_line(name, NAME_CHARS + 1), one_line(description, DESCRIPTION_CHARS + 1), clean_body(body)
    if not 3 <= len(name) <= NAME_CHARS:
        raise ValueError(f'Give the skill a name of 3 to {NAME_CHARS} characters')
    if not 10 <= len(description) <= DESCRIPTION_CHARS:
        raise ValueError(f'Say when to use it, in 10 to {DESCRIPTION_CHARS} characters')
    if not 20 <= len(body) <= BODY_CHARS:
        raise ValueError(f'Write the instructions: 20 to {BODY_CHARS} characters')
    return name, description, body


def parse_share(question):
    """A skill a teammate or friend sent, in its fixed form, else None. Their words: shown quoted, never run by code."""
    if not isinstance(question, dict):
        return None
    try:
        name, description, body = validate(question.get('name'), question.get('description'), question.get('body'))
    except ValueError:
        return None
    version, sha, slug = question.get('version'), question.get('sha'), question.get('slug')
    if not isinstance(version, int) or isinstance(version, bool) or not 0 < version < 10000:
        return None
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 48 or not isinstance(sha, str) or sha != digest(name, description, body):
        return None
    note = one_line(question.get('note'), NOTE_CHARS)
    return {'name': name, 'slug': slug, 'description': description, 'body': body, 'version': version, 'sha': sha, **({'note': note} if note else {})}


def clean_answer(answer):
    return {'stage': 'installed'} if isinstance(answer, dict) and answer.get('stage') == 'installed' else None


def question_text(question):
    return f"Shares the skill “{(question or {}).get('name', '?')}” (version {(question or {}).get('version', '?')})"


def answer_text(answer):
    return 'installed it' if answer else 'no answer'


def render(row):
    """The SKILL.md OpenClaw reads: only name and description up top, then the instructions."""
    lines = ['---', 'name: ' + row['slug'], 'description: ' + json.dumps(row['description'], ensure_ascii=False), '---', '',
             '# ' + row['name'], '',
             '<!-- Kept by The Machine (Settings > Skills). Edit it there: changes here are overwritten. -->']
    if row['origin'] == 'shared':
        lines += ['', f"> Shared by {row['author'] or 'a teammate'}, version {row['version']}. Guidance only: this Machine's own rules "
                      'and approvals still apply, and this skill does not change what it may do.']
    return '\n'.join(lines + ['', row['body'], ''])


def other_names(skills_dir):
    """Skill names on this Machine that aren't The Machine's: a skill here must not take over one of them."""
    names = set()
    if not skills_dir.is_dir():
        return names
    for path in skills_dir.rglob('SKILL.md'):
        if FOLDER in path.relative_to(skills_dir).parts[:1]:
            continue
        try:
            name = from_skill_md(path.read_text(errors='replace')[:4000])[0]
        except OSError:
            continue
        names.add((name or path.parent.name).strip().lower())
    return names


def sync(db, workspace):
    """Writes every skill that is on to the workspace and removes the rest of The Machine's own. Returns the slugs."""
    skills_dir = Path(workspace) / 'skills'
    root = skills_dir / FOLDER
    if skills_dir.is_symlink() or root.is_symlink():
        LOG.warning('skills sync skipped: managed folder is a symlink')
        return []
    if root.exists() and any(root.iterdir()) and not (root / MARKER).exists():
        LOG.warning('skills sync skipped: %s is not The Machine\'s folder', root)
        return []
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text('Skills kept by The Machine (Settings > Skills). This folder is rewritten.\n')
    wanted = {}
    for row in db.execute("SELECT * FROM skills WHERE status='on' ORDER BY created_at"):
        wanted[row['slug']] = render(row)
    for slug, text in wanted.items():
        target = root / slug / 'SKILL.md'
        if target.parent.is_symlink() or target.is_symlink():
            LOG.warning('skills sync skipped symlink slug=%s', slug)
            continue
        if target.exists() and target.read_text(errors='replace') == text:
            continue
        target.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.skill-', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    for child in root.iterdir():
        if child.name not in wanted and child.is_symlink():
            child.unlink()
        elif child.is_dir() and child.name not in wanted:
            shutil.rmtree(child)
    return sorted(wanted)


class Skills:
    def __init__(self, connect, workspace, fleet=None, config=None, catalog=None):
        self.connect, self.workspace, self.fleet = connect, Path(workspace), fleet
        # What OpenClaw itself has (read-only here); without a config (tests, tools) only The Machine's own.
        self.catalog = catalog if catalog is not None else Catalog(config) if config is not None else None
        self.hints = Hints(connect, self.catalog)
        with self.connect() as db:
            ensure(db)

    def sync(self):
        try:
            with self.connect() as db:
                written = sync(db, self.workspace)
        except OSError as error:
            LOG.error('skills sync error=%s', type(error).__name__)
            return []
        if self.catalog:
            self.catalog.at = 0  # OpenClaw's list changes with these: read it again when next asked
        return written

    def mine(self):
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT slug FROM skills WHERE status='on'")}

    # -- everything OpenClaw has (read-only, see Catalog)
    def installed(self, refresh=False):
        if not self.catalog:
            return {'installed': [], 'checked_at': None, 'error': 'OpenClaw isn\'t set up here'}
        return self.catalog.view(self.mine(), refresh)

    def installed_get(self, name):
        if not self.catalog:
            raise ValueError('OpenClaw isn\'t set up here')
        if str(name) in self.mine():
            raise ValueError('Open it from your own skills')
        return self.catalog.read(str(name))

    def installed_toggle(self, name, on):
        if not self.catalog:
            raise ValueError('OpenClaw isn\'t set up here')
        if str(name) in self.mine():
            raise ValueError('Turn your own skills on or off above')
        self.catalog.toggle(str(name), bool(on))
        if on:
            self.hints.close(str(name))
        return self.installed()

    def hub_search(self, query):
        if not self.catalog:
            raise ValueError('OpenClaw isn\'t set up here')
        return {'results': self.catalog.search(query)}

    def hub_get(self, ref):
        if not self.catalog:
            raise ValueError('OpenClaw isn\'t set up here')
        return self.catalog.hub(ref)

    def hub_install(self, ref, version, sha):
        if not self.catalog:
            raise ValueError('OpenClaw isn\'t set up here')
        shown = self.catalog.install(str(ref), str(version), str(sha))
        self.hints.close(shown['ref'])
        LOG.info('skills installed from clawhub ref=%s version=%s', shown['ref'], shown['version'])
        return {'installed_skill': shown['slug'], **({'differs': True} if shown.get('differs') else {}), **self.installed()}

    def hint(self, suggestion_id, hint):
        return self.hints.add(suggestion_id, hint, self.mine())

    def hint_dismiss(self, hint_id):
        self.hints.dismiss(hint_id)
        return self.view()

    def hint_prompt(self, clawhub=False):
        """For the agent's review (installed skills only) and preparation turns (also ClawHub, which it can search):
        how to point the owner to a skill that would help, while there is room this week. '' when there isn't."""
        if not self.catalog or not self.hints.room():
            return ''
        listed = self.hints.candidates(self.mine())
        if not listed and not clawhub:
            return ''
        where = ('one that isn\'t ready here' + (' (below)' if listed else '') + ' or one on ClawHub (`openclaw skills search <what it does>`)'
                 if clawhub else 'one of the skills below that aren\'t ready here')
        return (f'Skills: if {where} would clearly help with this kind of work, add '
                'skill_hint:{"skill":"<its name' + (', or @owner/slug from ClawHub' if clawhub else '') + '>","why":"<one line>"} to '
                + ('prepare' if clawhub else 'suggest') + '. Judge it by what the card is about and only when it clearly helps: the owner sees it as a quiet '
                'line on the card and decides, so never install or turn on a skill yourself.' + ('\n' + listed if listed else ''))

    def _slug(self, db, name, keep_id=None):
        """A free slug for a name: not another skill's here, not a skill OpenClaw already has from elsewhere."""
        base, taken = slug_of(name), other_names(self.workspace / 'skills')
        taken |= {r['slug'] for r in db.execute("SELECT id,slug FROM skills WHERE status IN ('on','off')") if r['id'] != keep_id}
        slug, n = base, 2
        while slug in taken:
            slug, n = f'{base[:44]}-{n}', n + 1
        return slug

    def row(self, db, skill_id):
        row = db.execute('SELECT * FROM skills WHERE id=?', (str(skill_id or ''),)).fetchone()
        if not row:
            raise ValueError('That skill is gone')
        return row

    # -- the owner's own
    def save(self, skill_id, name, description, body):
        if isinstance(body, str) and not (name or description) and FRONTMATTER.match(body):
            name, description, body = from_skill_md(body)
        name, description, body = validate(name, description, body)
        stamp, sha = now(), digest(name, description, body)
        with self.connect() as db:
            if skill_id:
                row = self.row(db, skill_id)
                if row['status'] not in ('on', 'off'):
                    raise ValueError('Install or approve it first')
                if row['sha'] == sha:
                    return self.view()
                slug = row['slug'] if row['name'] == name else self._slug(db, name, keep_id=row['id'])
                db.execute('UPDATE skills SET name=?,slug=?,description=?,body=?,sha=?,version=version+1,updated_at=? WHERE id=?',
                           (name, slug, description, body, sha, stamp, row['id']))
            else:
                if db.execute("SELECT COUNT(*) FROM skills WHERE status IN ('on','off')").fetchone()[0] >= MAX_SKILLS:
                    raise ValueError(f'At most {MAX_SKILLS} skills')
                db.execute("INSERT INTO skills(id,slug,name,description,body,status,origin,sha,created_at,updated_at) VALUES(?,?,?,?,?,'on','mine',?,?,?)",
                           ('sk_' + secrets.token_hex(8), self._slug(db, name), name, description, body, sha, stamp, stamp))
        self.sync()
        return self.view()

    def toggle(self, skill_id, on):
        with self.connect() as db:
            row = self.row(db, skill_id)
            if row['status'] not in ('on', 'off'):
                raise ValueError('Install or approve it first')
            db.execute('UPDATE skills SET status=?,updated_at=? WHERE id=?', ('on' if on else 'off', now(), row['id']))
        self.sync()
        return self.view()

    def delete(self, skill_id):
        with self.connect() as db:
            row = self.row(db, skill_id)
            if row['status'] == 'incoming':
                raise ValueError('Decline it instead')
            db.execute('DELETE FROM skills WHERE id=?', (row['id'],))
        self.sync()
        return self.view()

    def get(self, skill_id):
        with self.connect() as db:
            row = self.row(db, skill_id)
            current = db.execute('SELECT body FROM skills WHERE id=?', (row['replaces'],)).fetchone() if row['replaces'] else None
        return {'id': row['id'], 'name': row['name'], 'description': row['description'], 'body': row['body'],
                **({'previous_body': current['body']} if current else {})}

    def example(self, key='customer-inquiries'):
        found = EXAMPLES.get(key)
        if not found:
            raise ValueError('No such example')
        return dict(found)

    # -- proposed by the agent
    def propose(self, name, description, body, why=''):
        """The agent noticed repeated work: a skill for the owner to approve. Never on before that."""
        name, description, body = validate(name, description, body)
        stamp, sha = now(), digest(name, description, body)
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM skills WHERE status='proposed'").fetchone()[0] >= OPEN_PROPOSALS:
                raise ValueError(f'{OPEN_PROPOSALS} proposed skills already wait for the owner')
            if db.execute('SELECT 1 FROM skills WHERE sha=? OR (name=? AND status IN (\'on\',\'off\',\'proposed\'))', (sha, name)).fetchone():
                return {'ok': True, 'duplicate': True}
            skill_id = 'sk_' + secrets.token_hex(8)
            db.execute("INSERT INTO skills(id,slug,name,description,body,status,origin,sha,note,created_at,updated_at) VALUES(?,?,?,?,?,'proposed','proposed',?,?,?,?)",
                       (skill_id, slug_of(name), name, description, body, sha, one_line(why, NOTE_CHARS) or None, stamp, stamp))
        return {'ok': True, 'id': skill_id}

    # -- shared over the Network
    def targets(self):
        """Where a skill can go: each Fleet (everyone in it, or one teammate) and each friend."""
        fleet = self.fleet
        if not fleet:
            return []
        out = []
        try:
            for ref, found in fleet.fleets().items():
                if 'skill' not in (found.get('asks') or ()):  # the Network carries skills to this group only once it says so
                    continue
                people = [{'member_id': m['member_id'], 'name': fleet_module().label(m) or 'Someone'} for m in fleet.teammates(ref)]
                if people:
                    out.append({'fleet': ref, 'kind': found.get('kind'), 'name': found.get('name') or fleet_module().FLEET_NAMES.get(found.get('kind'), 'Fleet'),
                                'members': people})
        except (ValueError, KeyError, TypeError):
            return []
        return out

    def share(self, skill_id, fleet_ref, to=None, note=''):
        if not self.fleet:
            raise ValueError('Join a Fleet or add a friend first')
        target = next((t for t in self.targets() if t['fleet'] == fleet_ref), None)
        if not target:
            raise ValueError('Pick a Fleet or a friend')
        if to is not None:
            to = [m for m in to if any(p['member_id'] == m for p in target['members'])] if isinstance(to, list) else []
            if not to:
                raise ValueError('Pick who gets it')
        if target['kind'] == 'friends' and (not to or len(to) != 1):
            raise ValueError('Pick one friend')
        with self.connect() as db:
            row = self.row(db, skill_id)
            if row['status'] not in ('on', 'off'):
                raise ValueError('Install or approve it first')
        question = {'name': row['name'], 'slug': row['slug'], 'description': row['description'], 'body': row['body'],
                    'version': row['version'], 'sha': row['sha'], **({'note': one_line(note, NOTE_CHARS)} if one_line(note, NOTE_CHARS) else {})}
        sent = self.fleet.ask('skill', question, context=f"skill:{row['id']}:{row['version']}", fleet=fleet_ref, to=to)
        with self.connect() as db:
            shared = json.loads(self.row(db, skill_id)['shared'] or '[]')
            shared = [s for s in shared if s.get('fleet') != fleet_ref or s.get('to') != to][-19:]
            shared.append({'fleet': fleet_ref, 'to': to, 'version': row['version'], 'at': now()})
            db.execute('UPDATE skills SET shared=? WHERE id=?', (json.dumps(shared), row['id']))
        return {'sent': len(sent.get('ids', {})), **self.view()}

    def take_share(self, ask, teammate, fleet_ref, name):
        """A skill from a teammate or friend: kept for the owner to read, then install or decline. Returns the status
        the sender's Machine is told."""
        question = parse_share(ask.get('question'))
        if not teammate or not question:
            return 'declined'
        stamp = now()
        with self.connect() as db:
            previous = db.execute('SELECT status FROM skills WHERE ask_id=? AND fleet=? AND member_id=?',
                                  (str(ask.get('id') or ''), fleet_ref, teammate['member_id'])).fetchone()
            if previous:
                return 'waiting' if previous['status'] == 'incoming' else 'answered'
            if db.execute("SELECT COUNT(*) FROM skills WHERE status='incoming' AND fleet=? AND member_id=?",
                          (fleet_ref, teammate['member_id'])).fetchone()[0] >= OPEN_PER_SENDER:
                return 'declined'
            installed = db.execute("SELECT * FROM skills WHERE origin='shared' AND status IN ('on','off') AND fleet=? AND member_id=? AND their_slug=? "
                                   'ORDER BY updated_at DESC', (fleet_ref, teammate['member_id'], question['slug'])).fetchone()
            if installed and installed['sha'] == question['sha']:
                return 'answered'
            db.execute("DELETE FROM skills WHERE status='incoming' AND fleet=? AND member_id=? AND their_slug=?",
                       (fleet_ref, teammate['member_id'], question['slug']))
            db.execute("INSERT OR IGNORE INTO skills(id,slug,name,description,body,status,origin,version,sha,author,fleet,member_id,ask_id,their_slug,replaces,note,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,'incoming','shared',?,?,?,?,?,?,?,?,?,?,?)",
                       ('sk_' + secrets.token_hex(8), question['slug'], question['name'], question['description'], question['body'],
                        question['version'], question['sha'], one_line(name, 80) or 'A teammate', fleet_ref, teammate['member_id'], str(ask.get('id') or ''), question['slug'],
                        installed['id'] if installed else None, question.get('note'), stamp, stamp))
        return 'waiting'

    def decide(self, skill_id, choice):
        """The owner's Install / Decline on a shared skill, or Approve / Dismiss on a proposed one."""
        with self.connect() as db:
            row = self.row(db, skill_id)
            if row['status'] not in ('incoming', 'proposed'):
                raise ValueError('Already decided')
            if choice in ('install', 'approve'):
                if row['replaces'] and db.execute('SELECT 1 FROM skills WHERE id=?', (row['replaces'],)).fetchone():
                    db.execute('UPDATE skills SET name=?,description=?,body=?,sha=?,version=?,author=?,updated_at=? WHERE id=?',
                               (row['name'], row['description'], row['body'], row['sha'], row['version'], row['author'], now(), row['replaces']))
                    db.execute('DELETE FROM skills WHERE id=?', (row['id'],))
                else:
                    if db.execute("SELECT COUNT(*) FROM skills WHERE status IN ('on','off')").fetchone()[0] >= MAX_SKILLS:
                        raise ValueError(f'At most {MAX_SKILLS} skills')
                    db.execute("UPDATE skills SET status='on',slug=?,updated_at=? WHERE id=?", (self._slug(db, row['name']), now(), row['id']))
            elif choice in ('decline', 'dismiss'):
                db.execute('DELETE FROM skills WHERE id=?', (row['id'],))
            else:
                raise ValueError('Choose install or decline')
        if row['status'] == 'incoming' and row['ask_id'] and self.fleet:
            installed = choice in ('install', 'approve')
            try:
                self.fleet.relay.call('POST', '/answer', {'id': row['ask_id'], 'status': 'answered' if installed else 'declined',
                                                          **({'answer': {'stage': 'installed'}} if installed else {})})
            except (ValueError, OSError) as error:
                LOG.warning('skill answer error=%s', type(error).__name__)
        self.sync()
        return self.view()

    def shares_out(self, db, skill_id):
        rows = db.execute("SELECT name,status,answer,context FROM fleet_out WHERE kind='skill' AND context LIKE ? ORDER BY at DESC LIMIT 20",
                          (f'skill:{skill_id}:%',)).fetchall() if self.fleet else []
        label = {'asked': 'sent', 'waiting': 'reading it', 'answered': 'installed', 'declined': 'declined', 'expired': 'no answer'}
        seen, out = set(), []
        for r in rows:
            if r['name'] in seen:
                continue
            seen.add(r['name'])
            out.append({'name': r['name'], 'status': label.get(r['status'], r['status']), 'version': int(r['context'].rsplit(':', 1)[-1] or 0)})
        return out

    def view(self):
        with self.connect() as db:
            ensure(db)
            rows = db.execute("SELECT * FROM skills ORDER BY CASE status WHEN 'incoming' THEN 0 WHEN 'proposed' THEN 1 ELSE 2 END, name COLLATE NOCASE").fetchall()
            items = []
            for r in rows:
                item = {'id': r['id'], 'name': r['name'], 'description': r['description'], 'status': r['status'], 'origin': r['origin'],
                        'version': r['version'], 'author': r['author'] or '', 'note': r['note'] or '', 'update': bool(r['replaces']),
                        'chars': len(r['body']), 'updated_at': r['updated_at']}
                if r['status'] in ('on', 'off'):
                    item['shares'] = self.shares_out(db, r['id'])
                    item['changed'] = any(s['version'] < r['version'] for s in item['shares'])
                items.append(item)
        waiting = sum(1 for i in items if i['status'] in ('incoming', 'proposed'))
        return {'skills': items, 'waiting': waiting, 'targets': self.targets(), 'folder': f'skills/{FOLDER}', 'hints': self.hints.open(),
                'openclaw': bool(self.catalog)}


# -- Everything OpenClaw has (docs/skills.md): read from OpenClaw itself, never written here --------------------------
# The Machine's own skills are the ones above. OpenClaw also has bundled skills, ones in the workspace, learned ones and
# ones from ClawHub. Those are shown read-only: their files are never edited or removed from here. Turning one on or off
# is OpenClaw's own setting (skills.entries.<key>.enabled, which OpenClaw applies without a restart), and installing
# one from ClawHub is OpenClaw's own `openclaw skills install`, only on the owner's tap after reading it.
CATALOG_SECONDS, SEARCH_SECONDS = 1800, 600
HINTS_PER_WEEK = 3
REF = re.compile(r'@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([a-z0-9][a-z0-9-]{0,79})')
VERSION = re.compile(r'[0-9A-Za-z][0-9A-Za-z.+-]{0,39}')
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
TEXT_FILES = ('.md', '.txt', '.json', '.yaml', '.yml', '.csv', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')
OS_NAMES = {'darwin': 'macOS', 'linux': 'Linux', 'win32': 'Windows'}
SOURCES = {'openclaw-workspace': 'Workspace', 'openclaw-bundled': 'Bundled with OpenClaw', 'openclaw-workshop': 'Learned by OpenClaw',
           'openclaw-custodian': 'OpenClaw upkeep', 'openclaw-managed': 'Shared on this computer', 'openclaw-extra': 'From a plugin'}


def needs_text(missing, key=''):
    """What a skill still needs, in a few plain words; '' when nothing."""
    missing = missing if isinstance(missing, dict) else {}
    out = []
    bins = [str(b) for b in missing.get('bins') or []]
    if bins:
        out.append('the ' + ', '.join(bins[:3]) + (' commands' if len(bins) > 1 else ' command'))
    any_bins = [str(b) for b in missing.get('anyBins') or []]
    if any_bins:
        out.append('one of ' + ', '.join(any_bins[:3]))
    env = [str(e) for e in missing.get('env') or []]
    if env:
        out.append(', '.join(env[:3]))
    config = [str(c) for c in missing.get('config') or [] if str(c) != f'skills.entries.{key}.enabled']
    if config:
        out.append('the setting ' + ', '.join(config[:2]))
    return ('Needs ' + ' and '.join(out)) if out else ''


def os_only(missing):
    systems = [OS_NAMES.get(str(o), str(o)) for o in (missing or {}).get('os') or []] if isinstance(missing, dict) else []
    return (' or '.join(systems[:3]) + ' only') if systems else ''


def installed_item(raw, mine):
    """One skill OpenClaw lists, as the dashboard shows it."""
    name = one_line(raw.get('name'), 80)
    source = one_line(raw.get('source'), 60)
    missing = raw.get('missing') or {}
    only = os_only(missing)
    if name in mine:
        group = 'yours'
    elif source == 'openclaw-workspace':
        group = 'workspace'
    elif source == 'openclaw-bundled' or raw.get('bundled') is True:
        group = 'bundled'
    else:
        group = 'other'
    off = raw.get('disabled') is True
    blocked = raw.get('blockedByAllowlist') is True or raw.get('blockedByAgentFilter') is True
    ready = raw.get('eligible') is True and not off and not blocked
    state = 'ready' if ready else 'off' if off else 'blocked' if blocked else 'setup'
    need = only or needs_text(missing, name) or ('Not allowed for this agent' if blocked else '')
    return {'name': name, 'description': one_line(raw.get('description'), DESCRIPTION_CHARS), 'emoji': one_line(raw.get('emoji'), 4),
            'group': group, 'source': SOURCES.get(source, source.replace('openclaw-', '').replace('-', ' ').capitalize() or 'Other'),
            'state': state, 'on': not off, 'needs': need, 'toggle': group != 'yours' and not only}


class Catalog:
    """What OpenClaw itself sees (`openclaw skills list --json`), kept for half an hour and refreshed on demand, plus
    ClawHub search, reading a ClawHub skill before installing it, and OpenClaw's own install and on/off setting."""

    def __init__(self, config=None, run=subprocess.run, fetch=None, clock=time.time):
        config = config or {}
        self.binary = str(config.get('openclaw') or 'openclaw')
        self.registry = str(config.get('clawhub_registry') or 'https://clawhub.ai').rstrip('/')
        self.run, self.clock = run, clock
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.raw, self.at, self.error, self.searches = None, 0.0, '', {}

    def _cli(self, args, timeout=60):
        try:
            done = self.run([self.binary, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('OpenClaw did not answer' + (' in time' if isinstance(error, subprocess.TimeoutExpired) else '')) from None
        return done

    @staticmethod
    def _reason(done):
        lines = [ANSI.sub('', line).strip() for line in f'{done.stderr or ""}\n{done.stdout or ""}'.splitlines()]
        lines = [line for line in lines if line and not line.startswith(('OpenClaw ', '🦞'))]
        return one_line(lines[-1] if lines else 'OpenClaw said no', 300)

    def _json(self, args, timeout=60):
        done = self._cli([*args, '--json'], timeout)
        try:
            data = json.loads(done.stdout or '')
        except ValueError:
            raise ValueError(self._reason(done) if done.returncode else 'OpenClaw gave an answer The Machine could not read') from None
        return data

    def load(self):
        """Asks OpenClaw for every skill it has (about two seconds). Keeps the last good list when it fails."""
        try:
            data = self._json(['skills', 'list'])
            found = data.get('skills') if isinstance(data, dict) else None
            if not isinstance(found, list):
                raise ValueError('OpenClaw gave an answer The Machine could not read')
            with self.lock:
                self.raw = [r for r in found if isinstance(r, dict) and isinstance(r.get('name'), str)][:500]
                self.at, self.error = self.clock(), ''
        except ValueError as error:
            LOG.warning('skills catalog error=%s', error)
            with self.lock:
                self.at, self.error = self.clock(), str(error)
        return self.raw

    def peek(self):
        """The list as last read, without waiting (the service's worker reads it every half hour)."""
        return self.raw

    def worker(self, stop):
        """Reads OpenClaw's list every half hour, a minute after the service starts; cheap (one CLI call)."""
        if stop.wait(60):
            return
        while True:
            if self.clock() - self.at >= CATALOG_SECONDS - 5:
                self.load()
            if stop.wait(60):
                return

    def installed(self, refresh=False):
        if refresh or self.raw is None or self.clock() - self.at > CATALOG_SECONDS:
            self.load()
        return self.raw or []

    def find(self, name):
        return next((r for r in self.installed() if r.get('name') == name), None)

    def view(self, mine, refresh=False):
        items = [installed_item(r, mine) for r in self.installed(refresh)]
        order = {'yours': 0, 'workspace': 1, 'bundled': 2, 'other': 3}
        items.sort(key=lambda i: (order[i['group']], i['name'].lower()))
        return {'installed': items, 'checked_at': dt.datetime.fromtimestamp(self.at, dt.timezone.utc).isoformat(timespec='seconds') if self.at else None,
                'error': self.error}

    def read(self, name):
        """One installed skill's SKILL.md, to read. Only read; nothing is written."""
        if not self.find(name):
            raise ValueError('OpenClaw doesn\'t have that skill')
        info = self._json(['skills', 'info', name])
        path = Path(str(info.get('filePath') or '')) if isinstance(info, dict) else Path()
        body = ''
        if path.name == 'SKILL.md' and path.is_file() and path.stat().st_size <= 256 * 1024:
            body = path.read_text(errors='replace')
        steps = [one_line(s.get('label') or s.get('kind'), 120) for s in (info.get('install') or []) if isinstance(s, dict)][:5]
        item = installed_item({**self.find(name)}, set())
        return {'name': name, 'description': item['description'], 'source': item['source'], 'state': item['state'], 'needs': item['needs'],
                'body': clean_body(body)[:BODY_CHARS * 2] or item['description'], 'install_steps': [s for s in steps if s],
                'homepage': info.get('homepage') if str(info.get('homepage') or '').startswith('https://') else ''}

    def toggle(self, name, on):
        """OpenClaw's own on/off for a skill (skills.entries.<key>.enabled): applied without a Gateway restart."""
        if not self.find(name):
            raise ValueError('OpenClaw doesn\'t have that skill')
        info = self._json(['skills', 'info', name])
        key = str(info.get('skillKey') or name) if isinstance(info, dict) else name
        if not KEY.fullmatch(key):
            raise ValueError('That skill can\'t be turned on or off from here')
        done = self._cli(['config', 'set', f'skills.entries.{key}.enabled', 'true' if on else 'false', '--strict-json'])
        if done.returncode:
            raise ValueError(self._reason(done))
        self.load()

    # -- ClawHub: third-party skills
    def search(self, query):
        query = one_line(query, 80)
        if len(query) < 2:
            raise ValueError('Type what the skill should help with')
        cached = self.searches.get(query.lower())
        if cached and self.clock() - cached[0] < SEARCH_SECONDS:
            return cached[1]
        data = self._json(['skills', 'search', *query.split()[:8], '--limit', '12'], timeout=60)
        have = {r.get('name') for r in self.installed()}
        out = []
        for r in (data.get('results') if isinstance(data, dict) else None) or []:
            found = REF.fullmatch(str(r.get('installRef') or '')) if isinstance(r, dict) else None
            if not found:  # only skills ClawHub itself hosts and scans (@owner/slug)
                continue
            native = (r.get('native') or {}).get('skill') or {}
            trust = r.get('trust') or {}
            out.append({'ref': found.group(0), 'owner': found.group(1), 'slug': found.group(2),
                        'name': one_line(r.get('displayName') or found.group(2), 80), 'summary': one_line(r.get('summary'), DESCRIPTION_CHARS),
                        'downloads': r['downloads'] if isinstance(r.get('downloads'), int) else 0,
                        'official': r.get('official') is True, 'suspicious': native.get('isSuspicious') is True,
                        'installable': trust.get('installability', 'installable') == 'installable', 'installed': found.group(2) in have,
                        'url': f'{self.registry}/{found.group(1)}/skills/{found.group(2)}'})
        self.searches = {k: v for k, v in self.searches.items() if self.clock() - v[0] < SEARCH_SECONDS}
        self.searches[query.lower()] = (self.clock(), out)
        return out

    def _fetch(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': 'the-machine', 'Accept': 'application/json, text/plain'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(2 * 1024 * 1024 + 1)

    def _get(self, route, **params):
        url = self.registry + route + ('?' + urllib.parse.urlencode(params) if params else '')
        try:
            return self.fetch(url)
        except (OSError, urllib.error.URLError, ValueError):
            raise ValueError('ClawHub did not answer; try again in a minute') from None

    def hub(self, ref):
        """A ClawHub skill, read before installing: its SKILL.md as published, its files (so scripts show), whether it
        has install steps or asks for keys, and ClawHub's own scan. The sha is what an install must still match."""
        found = REF.fullmatch(str(ref or ''))
        if not found:
            raise ValueError('That isn\'t a ClawHub skill')
        owner, slug = found.group(1), found.group(2)
        try:
            detail = json.loads(self._get(f'/api/v1/skills/{slug}', ownerHandle=owner))
        except ValueError:
            raise ValueError('ClawHub doesn\'t have that skill') from None
        skill = detail.get('skill') if isinstance(detail, dict) else None
        if not isinstance(skill, dict) or skill.get('slug') != slug or ((detail.get('owner') or {}).get('handle') not in (None, owner)):
            raise ValueError('ClawHub doesn\'t have that skill')
        version = str((detail.get('latestVersion') or {}).get('version') or '')
        if not VERSION.fullmatch(version):
            raise ValueError('That skill has no release to install')
        try:
            release = json.loads(self._get(f'/api/v1/skills/{slug}/versions/{version}', ownerHandle=owner)).get('version') or {}
        except (ValueError, AttributeError):
            release = {}
        raw = self._get(f'/api/v1/skills/{slug}/file', path='SKILL.md', version=version, ownerHandle=owner)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('That skill is too large to read here')
        listed = next((f.get('sha256') for f in release.get('files') or [] if isinstance(f, dict) and f.get('path') == 'SKILL.md'), None)
        if listed and listed != hashlib.sha256(raw).hexdigest():
            raise ValueError('ClawHub sent a SKILL.md that doesn\'t match its release; not shown')
        text = raw.decode('utf-8', errors='replace')
        moderation = detail.get('moderation')
        files = [{'path': one_line(f.get('path'), 200), 'size': int(f.get('size') or 0)} for f in release.get('files') or [] if isinstance(f, dict)][:200]
        code = [f['path'] for f in files if not f['path'].lower().endswith(TEXT_FILES) or f['path'].split('/')[0] in ('scripts', 'bin')]
        top = FRONTMATTER.match(text)
        head = top.group(1) if top else ''
        security = release.get('security') if isinstance(release.get('security'), dict) else {}
        llm = ((security.get('scanners') or {}).get('llm') or {}) if isinstance(security.get('scanners'), dict) else {}
        return {'ref': found.group(0), 'owner': owner, 'slug': slug, 'name': one_line(skill.get('displayName') or slug, 80),
                'summary': one_line(skill.get('summary'), DESCRIPTION_CHARS), 'version': version, 'body': text[:64000],
                'sha': hashlib.sha256(raw).hexdigest(), 'files': files, 'code': code[:20],
                'install_steps': bool(re.search(r'["\']?install["\']?\s*:', head)),
                'asks_keys': bool(re.search(r'["\']?(?:env|primaryEnv|apiKey)["\']?\s*:', head)),
                'scan': one_line(security.get('status'), 40), 'scan_warnings': security.get('hasWarnings') is True,
                'scan_summary': one_line(llm.get('summary'), 400),
                'flagged': isinstance(moderation, dict) and (moderation.get('isSuspicious') is True or moderation.get('isMalwareBlocked') is True
                                                              or moderation.get('verdict') not in (None, 'clean')),
                'url': f'{self.registry}/{owner}/skills/{slug}'}

    def install(self, ref, version, sha):
        """OpenClaw's own install of exactly the release the owner read. Never forced, never past a policy warning."""
        shown = self.hub(ref)
        if shown['version'] != version or shown['sha'] != sha:
            raise ValueError('It changed since you read it; read it again first')
        if shown['slug'] in {r.get('name') for r in self.installed()}:
            raise ValueError('A skill with that name is already here')
        done = self._cli(['skills', 'install', shown['ref'], '--version', version], timeout=240)
        if done.returncode:
            raise ValueError(self._reason(done))
        self.load()
        try:  # what OpenClaw installed is what the owner read
            info = self._json(['skills', 'info', shown['slug']])
            got = ((info.get('clawhub') or {}).get('skillFile') or {}).get('sha256') if isinstance(info, dict) else None
        except ValueError:
            got = None
        if got and got != sha:
            LOG.warning('skills install sha differs ref=%s', shown['ref'])
            shown['differs'] = True
        return shown


class Hints:
    """"There's a skill for this": the agent names a skill that would clearly help with a card, judged from what the
    card is about. At most HINTS_PER_WEEK a week, never one the owner dismissed, never for a skill that is ready
    already (the agent uses those itself). A ClawHub one is only ever shown; installing it is the owner's tap."""

    def __init__(self, connect, catalog):
        self.connect, self.catalog = connect, catalog

    def room(self, db=None):
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec='seconds')
        if db is None:
            with self.connect() as db:
                return self.room(db)
        return db.execute('SELECT COUNT(*) FROM skill_hints WHERE created_at>?', (since,)).fetchone()[0] < HINTS_PER_WEEK

    def candidates(self, mine, limit=25):
        """Installed skills that aren't ready here, for the agent to weigh; '' when none or no room this week."""
        raw = self.catalog.peek() if self.catalog else None
        if not raw or not self.room():
            return ''
        with self.connect() as db:
            dismissed = {r[0] for r in db.execute("SELECT skill FROM skill_hints WHERE status='dismissed'")}
        lines = []
        for r in raw:
            item = installed_item(r, mine)
            if item['group'] == 'yours' or item['state'] == 'ready' or not item['toggle'] or item['name'] in dismissed:
                continue
            lines.append(f"- {item['name']}: {item['description'][:110]}" + (f" ({item['needs']})" if item['needs'] else ' (off)'))
        return '\n'.join(lines[:limit])

    def add(self, suggestion_id, hint, mine):
        """The agent's skill_hint on a card. Returns why not instead of failing the card."""
        if not isinstance(hint, dict):
            return {'ok': False, 'reason': 'skill_hint is {skill, why}'}
        skill, why = one_line(hint.get('skill'), 120).lstrip('$/'), one_line(hint.get('why'), 200)
        if len(why) < 5:
            return {'ok': False, 'reason': 'Say in one line why it helps (why)'}
        found = REF.fullmatch(skill)
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM suggestions WHERE id=?', (str(suggestion_id),)).fetchone():
                return {'ok': False, 'reason': 'Unknown suggestion'}
            if db.execute("SELECT 1 FROM skill_hints WHERE skill=? AND (status='dismissed' OR status='open')", (skill,)).fetchone():
                return {'ok': False, 'reason': 'Already suggested or dismissed by the owner'}
            if not self.room(db):
                return {'ok': False, 'reason': f'At most {HINTS_PER_WEEK} skill hints a week; this one is not shown'}
        if not self.catalog:
            return {'ok': False, 'reason': 'Skills are not available here'}
        try:
            if found:
                if found.group(2) in {r.get('name') for r in self.catalog.installed()}:
                    return {'ok': False, 'reason': 'It is installed already'}
                shown = self.catalog.hub(skill)
                kind, name, summary = 'clawhub', shown['name'], shown['summary']
            else:
                raw = self.catalog.find(skill)
                if not raw:
                    return {'ok': False, 'reason': 'No installed skill has that name; a ClawHub one is @owner/slug'}
                item = installed_item(raw, mine)
                if item['group'] == 'yours' or item['state'] == 'ready':
                    return {'ok': False, 'reason': 'It is ready already: use it yourself'}
                if not item['toggle']:
                    return {'ok': False, 'reason': 'It can\'t work on this computer'}
                kind, name, summary = 'installed', item['name'], item['description']
        except ValueError as error:
            return {'ok': False, 'reason': str(error)}
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO skill_hints(id,suggestion_id,skill,kind,name,summary,why,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
                       ('sh_' + secrets.token_hex(8), str(suggestion_id), skill, kind, name, summary, why, stamp, stamp))
        return {'ok': True, 'shown': 'on the card, as a quiet line the owner can dismiss'}

    def close(self, skill, status='done'):
        with self.connect() as db:
            db.execute("UPDATE skill_hints SET status=?,updated_at=? WHERE skill=? AND status='open'", (status, now(), skill))

    def dismiss(self, hint_id):
        with self.connect() as db:
            row = db.execute("SELECT skill FROM skill_hints WHERE id=?", (str(hint_id or ''),)).fetchone()
        if not row:
            raise ValueError('That hint is gone')
        self.close(row['skill'], 'dismissed')

    def open(self):
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec='seconds')
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suggestions'").fetchone():
                return []
            return [{k: r[k] for k in ('id', 'suggestion_id', 'skill', 'kind', 'name', 'summary', 'why')}
                    for r in db.execute("SELECT h.* FROM skill_hints h JOIN suggestions s ON s.id=h.suggestion_id WHERE h.status='open' AND h.created_at>? "
                                        "AND s.status NOT IN ('completed','rejected') ORDER BY h.created_at DESC LIMIT 10", (since,))]


def fleet_module():
    import fleet
    return fleet
