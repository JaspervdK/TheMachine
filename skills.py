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
import tempfile
from pathlib import Path

LOG = logging.getLogger('personal-assistant')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,slug TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,body TEXT NOT NULL,
  status TEXT NOT NULL,origin TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,sha TEXT NOT NULL,author TEXT,fleet TEXT,member_id TEXT,
  ask_id TEXT,their_slug TEXT,replaces TEXT,note TEXT,shared TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS skills_status ON skills(status);
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
    def __init__(self, connect, workspace, fleet=None):
        self.connect, self.workspace, self.fleet = connect, Path(workspace), fleet
        with self.connect() as db:
            ensure(db)

    def sync(self):
        try:
            with self.connect() as db:
                return sync(db, self.workspace)
        except OSError as error:
            LOG.error('skills sync error=%s', type(error).__name__)
            return []

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
        return {'skills': items, 'waiting': waiting, 'targets': self.targets(), 'folder': f'skills/{FOLDER}'}


def fleet_module():
    import fleet
    return fleet
