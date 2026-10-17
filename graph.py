"""People and projects: a living graph over the collected signals.

Rebuilt from scratch every few minutes as a pure function of the signals, card decisions, project
labels and merges, so nothing drifts and a rule change applies to the past too. Every interaction
adds weight that fades by half per half-life (closeness 60 days, people seen together 45, a
person's part in a project 45, a project's heat 21); a day counts at most once per person, so a
burst of chat messages is one day of contact. The owner's own messages count double theirs, a
meeting more, a newsletter or mass mailing not at all. Card decisions teach it too: Do it, Send
and Mark done strengthen the people on the evidence, Not relevant weakens them.

People are matched by identifiers only (email address, phone, Slack user, LinkedIn or X
conversation, GitHub login), joined when Slack reports the user's email, when the same full name
appears on two different channels, or when the owner or agent says so (merges). Projects are named
by the review agent and the owner, and spread to the rest of their threads. Going quiet compares
the silence with that person's own usual gap. Messages are never deleted; links that faded away
are just not stored."""
from __future__ import annotations

import collections
import datetime as dt
import email.utils
import json
import math
import re
import unicodedata

SCHEMA = '''
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE,created_at TEXT NOT NULL,closed_at TEXT);
CREATE TABLE IF NOT EXISTS event_projects(event_id INTEGER NOT NULL,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(event_id,project_id));
CREATE TABLE IF NOT EXISTS identity_names(identifier TEXT PRIMARY KEY,name TEXT,email TEXT,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_merges(a TEXT NOT NULL,b TEXT NOT NULL,same INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS graph_nodes(id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,strength REAL NOT NULL,
  last_at TEXT,signals INTEGER NOT NULL,gap_days REAL,quiet INTEGER NOT NULL DEFAULT 0,trend REAL,info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_links(a TEXT NOT NULL,b TEXT NOT NULL,kind TEXT NOT NULL,strength REAL NOT NULL,last_at TEXT,PRIMARY KEY(a,b));
CREATE INDEX IF NOT EXISTS graph_links_b ON graph_links(b);
CREATE TABLE IF NOT EXISTS graph_events(node_id TEXT NOT NULL,event_id INTEGER NOT NULL,at TEXT NOT NULL,PRIMARY KEY(node_id,event_id));
CREATE INDEX IF NOT EXISTS graph_events_event ON graph_events(event_id);
CREATE TABLE IF NOT EXISTS graph_state(id INTEGER PRIMARY KEY CHECK(id=1),built_at TEXT NOT NULL,signals INTEGER NOT NULL,took_ms INTEGER NOT NULL);
'''
HALF_LIFE = {'person': 60, 'pair': 45, 'project_person': 45, 'project': 21}
DAY_CAP = 1.0           # one day of contact, however many messages
FLOOR = 0.01            # faded below this: not stored
HORIZON_DAYS = 400      # older signals have faded far below the floor anyway
EVENTS_PER_NODE = 40
BULK_LABELS = {'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_FORUMS', 'CATEGORY_UPDATES'}
BULK = re.compile(r'(?:^|[._+-])(?:no-?reply|do-?not-?reply|notifications?|notify|newsletters?|news|mailer-daemon|mailer|bounces?|postmaster|'
                  r'marketing|digest|alerts?|automated|receipts?|invoices?|billing)(?:[._+-]|@)|@(?:[\w-]+\.)*(?:mcsv\.net|mcdlv\.net|'
                  r'sendgrid\.net|amazonses\.com|mailgun\.org|bounces?\.[\w.-]+|calendar\.google\.com|resource\.calendar\.google\.com)$', re.I)
FREEMAIL = {'gmail.com', 'googlemail.com', 'hotmail.com', 'hotmail.be', 'outlook.com', 'outlook.be', 'live.com', 'live.be', 'msn.com',
            'yahoo.com', 'yahoo.fr', 'icloud.com', 'me.com', 'mac.com', 'proton.me', 'protonmail.com', 'telenet.be', 'skynet.be',
            'gmx.com', 'gmx.de', 'web.de', 'aol.com', 'pandora.be', 'scarlet.be', 'proximus.be', 'yandex.com', 'zoho.com'}
ROLE = re.compile(r'^(?:info|support|hello|hi|team|sales|billing|admin|office|contact|help|accounts?|finance|jobs|hr|press|legal|security|privacy|feedback)@')
GENERIC_NAMES = {'you', 'linkedin member', 'x conversation', 'linkedin conversation', 'unknown'}
WEIGHTS = {'owner': 0.6, 'them': 0.3, 'met': 0.5}
DECISION_WEIGHT = {'accept': 1.0, 'send': 1.0, 'complete': 1.0, 'other': 0.5, 'reject': -0.5}


class Owner:
    """The owner's own identities, from the configured accounts: never a person in the graph."""
    def __init__(self, config):
        accounts = config.get('accounts', [])
        self.emails = {str(a.get('email', '')).lower() for a in accounts if '@' in str(a.get('email', ''))}
        for a in accounts:
            self.emails |= {str(e).lower() for e in a.get('owner_emails') or () if '@' in str(e)}
        self.emails |= {str(e).lower() for e in config.get('owner_emails') or ()}
        self.domains = {e.rsplit('@', 1)[1] for e in self.emails} - FREEMAIL
        self.slack = {a['user_id'] for a in accounts if a.get('service') == 'slack' and a.get('user_id')}
        self.handles = {str(a.get('handle') or a.get('login') or '').lower() for a in accounts if a.get('service') in ('x', 'github')} - {''}
        self.names = set()

    def learn(self, signals):
        """Addresses the owner sends from (aliases, an older personal address) and the name on them."""
        for _, _, kind, _, m in signals:
            if kind == 'gmail.message' and 'SENT' in (m.get('labels') or ()):
                for name, addr in addresses(m.get('from'))[:1]:
                    self.emails.add(addr)
                    if plain_name(name):
                        self.names.add(plain_name(name))

    def mine(self, address):
        """The owner's own address, or a shared role address (info@, support@) at the owner's domains."""
        return address in self.emails or (address.rsplit('@', 1)[-1] in self.domains and bool(ROLE.match(address)))


def addresses(value):
    if isinstance(value, list):
        value = ', '.join(map(str, value))
    return [(name.strip().strip('"'), addr.strip().lower()) for name, addr in email.utils.getaddresses([str(value or '')]) if '@' in addr]


def bulk(address):
    return bool(BULK.search(address))


def plain_name(name):
    """Comparable full name: accents, case, punctuation and extra spaces removed; None unless it
    looks like a person's full name (two or more words, no address)."""
    name = re.sub(r'\s*\(@[^)]*\)\s*$', '', str(name or '')).strip()
    if not name or '@' in name or name.casefold() in GENERIC_NAMES:
        return None
    folded = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().casefold()
    words = re.findall(r'[a-z]+', folded)
    return ' '.join(words) if len(words) >= 2 and all(len(w) > 1 for w in words[:2]) else None


def touches(kind, m, owner, wrote_to):
    """[(identifier, name, weight, role)] for one signal. Roles: 'owner' (the owner wrote to them),
    'them' (they wrote to the owner directly), 'met' (a meeting with the owner), 'seen' (present)."""
    if kind == 'gmail.message':
        labels = set(m.get('labels') or ())
        if labels & {'SPAM', 'TRASH'}:
            return []
        sender = addresses(m.get('from'))[:1]
        to, cc = addresses(m.get('to')), addresses(m.get('cc'))
        if 'SENT' in labels or (sender and sender[0][1] in owner.emails):
            to = [(n, a) for n, a in to if not owner.mine(a)]
            cc = [(n, a) for n, a in cc if not owner.mine(a)]
            count = len(to) + len(cc)
            if not count or count > 20:
                return []  # a note to self, or a mass mailing
            share = 1 / math.sqrt(count)
            return [('email:' + a, n, WEIGHTS['owner'] * share, 'owner') for n, a in to] + [('email:' + a, n, 0.3 * share, 'owner') for n, a in cc]
        if not sender or owner.mine(sender[0][1]):
            return []
        name, addr = sender[0]
        if (labels & BULK_LABELS or bulk(addr)) and addr not in wrote_to:
            return []
        direct = any(owner.mine(a) for _, a in to)
        # Unsolicited mail counts half until the owner has written to them.
        out = [('email:' + addr, name, (WEIGHTS['them'] if addr in wrote_to else 0.15) if direct else 0.1, 'them' if direct else 'seen')]
        others = [(n, a) for n, a in to + cc if not owner.mine(a) and a != addr and not bulk(a)]
        return out + ([('email:' + a, n, 0.05, 'seen') for n, a in others] if len(others) <= 8 else [])
    if kind == 'calendar.event':
        if m.get('status') == 'cancelled':
            return []
        attendees = [a for a in m.get('attendees') or () if isinstance(a, dict)]
        organizer = m.get('organizer') if isinstance(m.get('organizer'), dict) else {}
        if len(attendees) > 25:
            return []
        mine = any(a.get('self') or str(a.get('email', '')).lower() in owner.emails for a in attendees + [organizer])
        people = {}
        for a in attendees + [organizer]:
            addr = str(a.get('email') or '').lower()
            if ('@' in addr and not a.get('self') and not a.get('resource') and not owner.mine(addr)
                    and a.get('responseStatus') != 'declined' and not bulk(addr)):
                people.setdefault(addr, a.get('displayName'))
        if not people:
            return []
        share = (WEIGHTS['met'] if mine else 0.1) / math.sqrt(len(people))
        return [('email:' + a, n, share, 'met' if mine else 'seen') for a, n in people.items()]
    if kind == 'whatsapp.message':
        chat = str(m.get('chat_id') or '')
        mine = m.get('from_me') is True
        if chat.endswith('@g.us'):
            sender = str(m.get('sender_id') or '')
            return [] if mine or '@' not in sender else [('wa:' + sender.split('@')[0], m.get('from'), 0.1, 'seen')]
        if '@' not in chat or chat.endswith('@broadcast') or chat.startswith('status@'):
            return []
        return [('wa:' + chat.split('@')[0], m.get('chat_name'), WEIGHTS['owner' if mine else 'them'], 'owner' if mine else 'them')]
    if kind == 'slack.message':
        if m.get('bot_id') or m.get('subtype') in ('bot_message', 'channel_join', 'channel_leave'):
            return []
        user, kind_ = m.get('user_id'), m.get('conversation_type')
        if kind_ == 'dm':
            partner = re.fullmatch(r'DM ([UW][A-Z0-9]+)', str(m.get('channel_name') or ''))
            if not partner or partner[1] in owner.slack:
                return []
            mine = user in owner.slack
            return [('slack:' + partner[1], None, WEIGHTS['owner' if mine else 'them'], 'owner' if mine else 'them')]
        if not user or user in owner.slack:
            return []
        return [('slack:' + user, None, 0.15 if kind_ == 'group' else 0.05, 'seen')]
    if kind == 'linkedin.message':
        if m.get('group') or not m.get('conversation_id'):
            return []
        mine = m.get('from_me') is True
        return [('li:' + str(m['conversation_id']), m.get('chat_name'), WEIGHTS['owner' if mine else 'them'], 'owner' if mine else 'them')]
    if kind == 'x.dm':
        if m.get('group'):
            return []
        mine = m.get('from_me') is True
        sender = str(m.get('sender_id') or '')
        other = [p for p in str(m.get('conversation_id') or '').split('-') if p and p != sender] if mine else [sender]
        if len(other) != 1 or not other[0]:
            return []
        return [('x:' + other[0], m.get('chat_name'), WEIGHTS['owner' if mine else 'them'], 'owner' if mine else 'them')]
    if kind == 'x.mention':
        if not m.get('author_id') or str(m.get('screen_name') or '').lower() in owner.handles:
            return []
        return [('x:' + str(m['author_id']), m.get('from'), 0.2 if m.get('reply_to_account') else 0.1, 'seen')]
    if kind == 'github.notification':
        login = str(m.get('from') or '').lower()
        return [] if not login or m.get('from_me') or login in owner.handles or login.endswith('[bot]') else [('gh:' + login, m.get('from'), 0.1, 'seen')]
    if kind == 'intercom.conversation':
        found = addresses(m.get('from'))
        return [('email:' + found[0][1], None, 0.05, 'seen')] if found and not owner.mine(found[0][1]) and not bulk(found[0][1]) else []
    if kind in ('salesflare.email', 'salesflare.meeting'):
        contacts = [c for c in m.get('contacts') or () if isinstance(c, dict) and '@' in str(c.get('email') or '')][:5]
        met = kind == 'salesflare.meeting' and str(m.get('host') or '').lower() in owner.emails
        return [('email:' + str(c['email']).lower(), c.get('name'), (WEIGHTS['met'] / math.sqrt(len(contacts))) if met else 0.05,
                 'met' if met else 'seen') for c in contacts if not owner.mine(str(c['email']).lower()) and not bulk(str(c['email']).lower())]
    return []


def thread_key(kind, m):
    """(key, exact): signals sharing an exact key are one thread; a chat key holds only within days."""
    if kind == 'gmail.message' and m.get('thread_id'):
        return 'gm:' + str(m['thread_id']), True
    if kind == 'slack.message' and m.get('channel_id'):
        stamp = m.get('thread_ts') or m.get('message_ts')
        return ('sl:' + str(m['channel_id']) + ':' + str(stamp), True) if stamp else (None, False)
    if kind in ('whatsapp.message',) and m.get('chat_id'):
        return 'wa:' + str(m['chat_id']), False
    if kind in ('linkedin.message', 'x.dm') and m.get('conversation_id'):
        return kind + ':' + str(m['conversation_id']), False
    if kind == 'x.mention' and m.get('conversation_id'):
        return 'xc:' + str(m['conversation_id']), True
    if kind == 'intercom.conversation' and m.get('conversation_id'):
        return 'ic:' + str(m['conversation_id']), True
    if kind == 'github.notification' and m.get('repo') and m.get('number'):
        return 'gh:' + str(m['repo']) + '#' + str(m['number']), True
    if kind == 'calendar.event' and m.get('recurring_event_id'):
        return 'cal:' + str(m['recurring_event_id']), True
    return None, False


class Union:
    """Identifiers joined into people; a pair said to be different is never joined, directly or through others."""
    def __init__(self, apart=()):
        self.parent, self.members, self.apart = {}, {}, set(apart)

    def find(self, x):
        if x not in self.parent:
            self.parent[x], self.members[x] = x, {x}
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b or any(frozenset((x, y)) in self.apart for x in self.members[a] for y in self.members[b]):
            return
        # An email address names the node when there is one: stable and recognisable.
        if b.startswith('email:') and not a.startswith('email:'):
            a, b = b, a
        self.parent[b] = a
        self.members[a] |= self.members.pop(b)


def parse_time(value):
    at = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)


def fade(weight, age_days, half_life):
    return weight * 0.5 ** (max(0.0, age_days) / half_life)


def shown_id(identifier, x_names):
    space, _, value = identifier.partition(':')
    return {'email': value, 'wa': '+' + value if value.isdigit() else 'WhatsApp', 'slack': 'Slack', 'li': 'LinkedIn',
            'x': '@' + x_names[identifier] if x_names.get(identifier) else 'X', 'gh': 'GitHub ' + value}.get(space, identifier)


def build(db, config, now):
    """Recompute graph_nodes, graph_links and graph_events. Returns a short summary."""
    started = dt.datetime.now(dt.timezone.utc)
    current = parse_time(now)
    owner = Owner(config)
    horizon = (current - dt.timedelta(days=HORIZON_DAYS)).isoformat()
    rows = db.execute("SELECT id,account_id,kind,occurred_at,metadata FROM events WHERE deleted=0 AND occurred_at>=? ORDER BY occurred_at", (horizon,)).fetchall()
    signals = []
    for r in rows:
        try:
            m = json.loads(r['metadata'])
        except ValueError:
            continue
        signals.append((r['id'], r['account_id'], r['kind'], parse_time(r['occurred_at']), m if isinstance(m, dict) else {}))
    owner.learn(signals)
    wrote_to = set()
    for _, _, kind, _, m in signals:
        if kind == 'gmail.message' and ('SENT' in (m.get('labels') or ()) or any(a in owner.emails for _, a in addresses(m.get('from'))[:1])):
            wrote_to |= {a for _, a in addresses(m.get('to')) + addresses(m.get('cc'))}

    # Who: identifiers per signal, joined into people.
    seen_message_ids = set()
    per_event, names, x_names = {}, collections.defaultdict(collections.Counter), {}
    crm_names = {}
    for event_id, account_id, kind, at, m in signals:
        if kind == 'gmail.message' and m.get('message-id'):
            if m['message-id'] in seen_message_ids:
                continue  # the same email in two mailboxes counts once
            seen_message_ids.add(m['message-id'])
        crm = m.get('crm_account') if isinstance(m.get('crm_account'), dict) else None
        if crm and crm.get('domain') and crm.get('name'):
            crm_names[str(crm['domain']).lower()] = str(crm['name'])
        found = touches(kind, m, owner, wrote_to)
        if not found:
            continue
        per_event[event_id] = (kind, at, account_id, m, found)
        for identifier, name, _, _ in found:
            if name and str(name).strip() and '@' not in str(name) and str(name).strip().casefold() not in GENERIC_NAMES:
                names[identifier][re.sub(r'\s*\(@[^)]*\)\s*$', '', str(name)).strip()] += 1
            if identifier.startswith('x:') and m.get('screen_name') and not m.get('from_me'):
                x_names[identifier] = m['screen_name']
    merges = db.execute('SELECT a,b,same FROM graph_merges ORDER BY created_at').fetchall()
    union = Union(frozenset((r['a'], r['b'])) for r in merges if not r['same'])
    known = {identifier for (_, _, _, _, found) in per_event.values() for identifier, _, _, _ in found}
    for identifier in sorted(known):
        union.find(identifier)
    for r in merges:
        if r['same']:
            union.join(r['a'], r['b'])
    for r in db.execute('SELECT identifier,name,email FROM identity_names'):
        if r['name']:
            names[r['identifier']][r['name']] += 3
        if r['email'] and r['identifier'] in known:
            union.join('email:' + r['email'].lower(), r['identifier'])
    by_name = collections.defaultdict(set)
    for identifier in known:
        best = names[identifier].most_common(1)
        full = plain_name(best[0][0]) if best else None
        if full:
            by_name[full].add(identifier)
    for full, group in by_name.items():
        if full in owner.names:
            continue  # the owner, writing from another channel
        spaces = collections.Counter(i.split(':', 1)[0] for i in group)
        # The same full name on different channels is one person, unless it's ambiguous
        # within a channel (two addresses, two chats) or someone said they differ.
        if len(spaces) > 1 and all(c == 1 for c in spaces.values()):
            group = sorted(group, key=lambda i: (not i.startswith('email:'), i))
            for other in group[1:]:
                union.join(group[0], other)

    # How strong: per day, capped; faded by age.
    day_weight = collections.defaultdict(float)
    roles = collections.defaultdict(lambda: collections.defaultdict(set))
    pair_day = collections.defaultdict(float)
    node_events = collections.defaultdict(list)
    event_people, direct_people = {}, {}
    next_meeting, accounts = {}, collections.defaultdict(set)
    for event_id, (kind, at, account_id, m, found) in per_event.items():
        people = {}
        for identifier, _, weight, role in found:
            node = union.find(identifier)
            people[node] = max(people.get(node, 0), weight)
            if at > current:
                if role == 'met' and (node not in next_meeting or at < next_meeting[node]):
                    next_meeting[node] = at
                continue
            day = at.date()
            day_weight[(node, day)] += weight
            roles[node][role].add(day)
            accounts[node].add(account_id)
        event_people[event_id] = list(people)
        direct_people[event_id] = [union.find(i) for i, _, weight, role in found if role != 'seen']
        for node in people:
            node_events[node].append((at, event_id))
        if at <= current and 2 <= len(people) <= 8:
            ordered = sorted(people)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pair_day[(a, b, at.date())] = min(0.5, pair_day[(a, b, at.date())] + 0.2 / (len(people) - 1))

    def age(day):
        return (current - dt.datetime.combine(day, dt.time(12), dt.timezone.utc)).total_seconds() / 86400

    strength, last_day = collections.defaultdict(float), {}
    for (node, day), weight in day_weight.items():
        strength[node] += fade(min(DAY_CAP, weight), age(day), HALF_LIFE['person'])
        last_day[node] = max(last_day.get(node, day), day)

    # Projects: labels, spread to the rest of their threads.
    projects = {r['id']: dict(r) for r in db.execute('SELECT id,name,created_at,closed_at FROM projects')}
    labelled = collections.defaultdict(set)
    for r in db.execute('SELECT event_id,project_id FROM event_projects'):
        if r['project_id'] in projects:
            labelled[r['event_id']].add(r['project_id'])
    threads = collections.defaultdict(list)
    for event_id, account_id, kind, at, m in signals:
        key, exact = thread_key(kind, m)
        if key:
            threads[(account_id, key)].append((at, event_id, exact))
    spread = {e: set(p) for e, p in labelled.items()}
    for members in threads.values():
        tagged = [(at, labelled[e]) for at, e, _ in members if e in labelled]
        if not tagged:
            continue
        for at, event_id, exact in members:
            for tagged_at, ids in tagged:
                if exact or abs((at - tagged_at).total_seconds()) <= 2 * 86400:
                    spread.setdefault(event_id, set()).update(ids)
    times = {event_id: at for event_id, _, _, at, _ in signals}
    heat_day = collections.defaultdict(float)
    project_events = collections.defaultdict(list)
    project_people = collections.defaultdict(float)
    project_people_last = {}
    for event_id, ids in spread.items():
        at = times.get(event_id)
        if at is None:
            continue
        for pid in ids:
            project_events[pid].append((at, event_id))
            if at <= current:
                heat_day[(pid, at.date())] += 0.2
                for node in event_people.get(event_id, ()):
                    key = (node, pid)
                    project_people[key] += fade(0.3, age(at.date()), HALF_LIFE['project_person'])
                    project_people_last[key] = max(project_people_last.get(key, at), at)

    # The owner's decisions on cards: once per card, for the people it was directly about.
    decided = collections.defaultdict(float)
    cards = {}
    for r in db.execute('SELECT d.suggestion_id,d.choice,d.feedback,d.created_at,s.event_ids,s.project FROM decisions d '
                        'JOIN suggestions s ON s.id=d.suggestion_id '
                        'WHERE NOT EXISTS (SELECT 1 FROM decision_annotations a WHERE a.decision_id=d.id) ORDER BY d.created_at'):
        weight = DECISION_WEIGHT.get(r['choice'])
        if weight is None or (r['choice'] == 'reject' and (r['feedback'] or '').strip() not in ('', 'Not relevant', 'Wrong facts')):
            continue  # "Already handled" says nothing against the people
        at = parse_time(r['created_at'])
        if at > current:
            continue
        card = cards.setdefault(r['suggestion_id'], {'weight': weight, 'at': at, 'event_ids': r['event_ids'], 'project': r['project']})
        if weight > card['weight'] or (weight < 0 < card['weight']):
            card.update(weight=weight, at=at)  # the strongest yes; a later Not relevant overrides it
    projects_by_name = {p['name'].casefold(): pid for pid, p in projects.items()}
    for card in cards.values():
        try:
            evidence = [int(i) for i in json.loads(card['event_ids'] or '[]')]
        except (ValueError, TypeError):
            evidence = []
        for node in {n for e in evidence for n in direct_people.get(e, ())}:
            decided[node] += fade(card['weight'], age(card['at'].date()), HALF_LIFE['person'])
        project_id = projects_by_name.get(str(card['project'] or '').casefold())
        if project_id and card['weight'] > 0:
            heat_day[(project_id, card['at'].date())] += card['weight']
    for node, weight in decided.items():
        strength[node] = max(0.0, strength[node] + weight)

    heat_days = collections.defaultdict(list)
    for (pid, day), weight in heat_day.items():
        heat_days[pid].append((dt.datetime.combine(day, dt.time(12), dt.timezone.utc), min(2.0, weight)))

    def heat(pid, then):
        return sum(fade(w, (then - at).total_seconds() / 86400, HALF_LIFE['project']) for at, w in heat_days[pid] if at <= then)

    # Write.
    nodes, links, members = [], [], collections.defaultdict(list)
    for node, value in strength.items():
        if value < FLOOR:
            continue
        group = sorted(union.members.get(node, {node}))
        counts = collections.Counter()
        for identifier in group:
            counts.update(names.get(identifier, {}))
        name = counts.most_common(1)[0][0] if counts else shown_id(node, x_names)
        if plain_name(name) in owner.names:
            continue  # the owner on another channel (a note-to-self chat, an old address)
        talk = sorted(roles[node]['owner'] | roles[node]['them'] | roles[node]['met'])
        gaps = [(b - a).days for a, b in zip(talk, talk[1:])][-8:]
        usual = sorted(gaps)[len(gaps) // 2] if len(gaps) >= 3 else None
        since = (current.date() - last_day[node]).days if node in last_day else None
        quiet = bool(usual and len(roles[node]['owner']) >= 2 and since is not None and max(14, 3 * usual) < since < 365)
        domains = sorted({i.split('@', 1)[1] for i in group if i.startswith('email:')} - FREEMAIL - owner.domains)
        if domains:
            members[domains[0]].append((node, value))
        info = {'ids': sorted({shown_id(i, x_names) for i in group}), 'org': ('org:' + domains[0]) if domains else None,
                'wrote': len(roles[node]['owner']), 'heard': len(roles[node]['them']), 'met': len(roles[node]['met']),
                'accounts': sorted(accounts[node]), 'next_meeting': next_meeting[node].isoformat() if node in next_meeting else None,
                'colleague': any(i.startswith('email:') and i.rsplit('@', 1)[1] in owner.domains for i in group)}
        nodes.append((node, 'person', name[:200], round(value, 4), last_day[node].isoformat() if node in last_day else None,
                      len(node_events[node]), usual, int(quiet), None, json.dumps(info)))
    kept = {n[0] for n in nodes}
    for domain, people in members.items():
        total = sum(v for _, v in people)
        nodes.append(('org:' + domain, 'org', crm_names.get(domain, domain)[:200], round(total, 4),
                      max((last_day[p].isoformat() for p, _ in people if p in last_day), default=None), len(people), None, 0, None,
                      json.dumps({'domain': domain})))
        for node, value in people:
            links.append((node, 'org:' + domain, 'member', round(value, 4), None))
    two_weeks = current - dt.timedelta(days=14)
    for pid, project in projects.items():
        value, before = heat(pid, current), heat(pid, two_weeks)
        if value < FLOOR and not project_events.get(pid):
            continue
        events_ = sorted(project_events.get(pid, ()))
        nodes.append(('project:' + str(pid), 'project', project['name'], round(value, 4), events_[-1][0].date().isoformat() if events_ else None,
                      len(events_), None, 0, round(value / before, 2) if before > FLOOR else None,
                      json.dumps({'closed_at': project['closed_at'], 'created_at': project['created_at']})))
        kept.add('project:' + str(pid))
    for (node, pid), value in project_people.items():
        if value >= FLOOR and node in kept:
            links.append((node, 'project:' + str(pid), 'project', round(value, 4), project_people_last[(node, pid)].date().isoformat()))
    pairs = collections.defaultdict(float)
    pair_last = {}
    for (a, b, day), weight in pair_day.items():
        pairs[(a, b)] += fade(weight, age(day), HALF_LIFE['pair'])
        pair_last[(a, b)] = max(pair_last.get((a, b), day), day)
    for (a, b), value in pairs.items():
        if value >= FLOOR and a in kept and b in kept:
            links.append((a, b, 'together', round(value, 4), pair_last[(a, b)].isoformat()))
    kept |= {n[0] for n in nodes}
    event_rows = []
    for node, items in list(node_events.items()) + [('project:' + str(p), v) for p, v in project_events.items()]:
        if node in kept:
            # Complete membership for old task evidence; only readers cap display lists.
            event_rows += [(node, e, at.isoformat()) for at, e in sorted(items)]
    took = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('DELETE FROM graph_nodes')
        db.execute('DELETE FROM graph_links')
        db.execute('DELETE FROM graph_events')
        db.executemany('INSERT OR REPLACE INTO graph_nodes(id,kind,name,strength,last_at,signals,gap_days,quiet,trend,info) VALUES(?,?,?,?,?,?,?,?,?,?)', nodes)
        db.executemany('INSERT OR REPLACE INTO graph_links(a,b,kind,strength,last_at) VALUES(?,?,?,?,?)', links)
        db.executemany('INSERT OR IGNORE INTO graph_events(node_id,event_id,at) VALUES(?,?,?)', event_rows)
        db.execute('INSERT OR REPLACE INTO graph_state(id,built_at,signals,took_ms) VALUES(1,?,?,?)', (now, len(signals), took))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return {'people': sum(1 for n in nodes if n[1] == 'person'), 'projects': sum(1 for n in nodes if n[1] == 'project'),
            'links': len(links), 'signals': len(signals), 'took_ms': took}


# Reading.

def closeness(value):
    return 'close' if value >= 3 else 'regular' if value >= 1 else 'occasional' if value >= 0.3 else 'faint'


def node_view(row, links=None):
    info = json.loads(row['info'] or '{}')
    view = {'id': row['id'], 'kind': row['kind'], 'name': row['name'], 'strength': round(row['strength'], 2), 'last_at': row['last_at'],
            'signals': row['signals']}
    if row['kind'] == 'person':
        view.update(closeness=closeness(row['strength']), ids=info.get('ids', []), org=info.get('org'), quiet=bool(row['quiet']),
                    usual_gap_days=row['gap_days'], wrote_days=info.get('wrote'), heard_days=info.get('heard'), met_days=info.get('met'),
                    next_meeting=info.get('next_meeting'), colleague=info.get('colleague'))
    elif row['kind'] == 'project':
        view.update(trend=row['trend'], state=project_state(row), closed=bool(info.get('closed_at')))
    if links is not None:
        view['links'] = links
    return view


def project_state(row):
    if row['strength'] < 0.3:
        return 'dormant'
    # Fourteen quiet days decay heat to ~0.63. Below 0.8 means cooling,
    # while continuing activity can offset that decay and remain active.
    if row['trend'] is not None and row['trend'] < 0.8:
        return 'cooling'
    if row['trend'] is None or row['trend'] > 1.3:
        return 'heating up'
    return 'active'


def neighbours(db, node_id, limit=12):
    rows = db.execute('SELECT l.*,n.name,n.kind node_kind FROM graph_links l JOIN graph_nodes n ON n.id=(CASE WHEN l.a=? THEN l.b ELSE l.a END) '
                      'WHERE l.a=? OR l.b=? ORDER BY l.strength DESC LIMIT ?', (node_id, node_id, node_id, limit)).fetchall()
    return [{'id': r['b'] if r['a'] == node_id else r['a'], 'name': r['name'], 'kind': r['node_kind'], 'link': r['kind'],
             'strength': round(r['strength'], 2), 'last_at': r['last_at']} for r in rows]


def find(db, text, limit=10, kinds=('person', 'project', 'org')):
    """Nodes by name, address or handle; strongest first."""
    words = [w for w in re.findall(r'[\w@.+-]+', str(text or '').casefold()) if len(w) > 1]
    if not words:
        return []
    marks = ','.join('?' for _ in kinds)
    rows = db.execute(f'SELECT * FROM graph_nodes WHERE kind IN ({marks}) ORDER BY strength DESC', tuple(kinds)).fetchall()
    folded = lambda s: unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().casefold()
    hits = [r for r in rows if all(w in folded(r['name'] + ' ' + r['id'] + ' ' + r['info']) for w in map(folded, words))]
    return hits[:limit]


def spread(db, seeds, limit=12, hops=2):
    """Spreading activation from seed nodes: what is alive around them, by link strength."""
    activation = {s: 1.0 for s in seeds}
    frontier = dict(activation)
    for _ in range(hops):
        following = collections.defaultdict(float)
        for node, value in frontier.items():
            links = db.execute('SELECT a,b,strength FROM graph_links WHERE a=? OR b=? ORDER BY strength DESC LIMIT 40', (node, node)).fetchall()
            top = max((r['strength'] for r in links), default=0)
            for r in links:
                other = r['b'] if r['a'] == node else r['a']
                following[other] += value * 0.5 * r['strength'] / top
        frontier = {n: v for n, v in following.items() if n not in seeds}
        for node, value in frontier.items():
            activation[node] = activation.get(node, 0) + value
    ranked = sorted(((v, n) for n, v in activation.items() if n not in seeds), reverse=True)[:limit]
    rows = {r['id']: r for r in db.execute('SELECT * FROM graph_nodes WHERE id IN (' + ','.join('?' for _ in ranked) + ')', [n for _, n in ranked])} if ranked else {}
    return [{**node_view(rows[n]), 'activation': round(v, 3)} for v, n in ranked if n in rows]


def for_events(db, event_ids):
    """The people and projects on these signals, strongest first."""
    ids = [int(i) for i in event_ids]
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    return db.execute(f'SELECT DISTINCT n.* FROM graph_events g JOIN graph_nodes n ON n.id=g.node_id WHERE g.event_id IN ({marks}) ORDER BY n.kind DESC,n.strength DESC', ids).fetchall()


def short_date(value):
    if not value:
        return 'never'
    day = dt.date.fromisoformat(str(value)[:10])
    return f'{day:%a} {day.day} {day:%b}'


def context_lines(db, event_ids, limit_chars=1500):
    """A few lines for a preparation: who these people are to the owner, and the projects around them."""
    lines = []
    for row in for_events(db, event_ids)[:8]:
        view = node_view(row)
        if row['kind'] == 'person':
            projects = [n for n in neighbours(db, row['id'], 12) if n['kind'] == 'project'][:3]
            people = [n for n in neighbours(db, row['id'], 12) if n['kind'] == 'person'][:3]
            bits = [f"{view['closeness']} contact", f"last contact {short_date(view['last_at'])}"]
            if view['usual_gap_days']:
                gap = int(view['usual_gap_days'])
                bits.append(('usually daily' if gap <= 1 else f'usually every {gap} days') + (' (gone quiet)' if view['quiet'] else ''))
            if view['next_meeting']:
                bits.append('next meeting ' + short_date(view['next_meeting']))
            if projects:
                bits.append('projects: ' + ', '.join(p['name'] for p in projects))
            if people:
                bits.append('often with ' + ', '.join(p['name'] for p in people))
            lines.append(f"- {view['name']} ({', '.join(view['ids'][:2])}): " + '; '.join(bits))
        elif row['kind'] == 'project':
            people = [n['name'] for n in neighbours(db, row['id'], 6) if n['kind'] == 'person'][:4]
            lines.append(f"- Project {view['name']}: {view['state']}, last signal {short_date(view['last_at'])}" + (f"; people: {', '.join(people)}" if people else ''))
    text = ''
    for line in lines:
        if len(text) + len(line) + 1 > limit_chars:
            break
        text += line + '\n'
    return text.rstrip()


def overview(db, limit=12):
    rows = lambda sql, *args: [node_view(r) for r in db.execute(sql, args)]
    state = db.execute('SELECT built_at,signals,took_ms FROM graph_state WHERE id=1').fetchone()
    week_ago = ((parse_time(state['built_at']) if state else dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=7)).date().isoformat()
    projects = rows("SELECT * FROM graph_nodes WHERE kind='project' AND json_extract(info,'$.closed_at') IS NULL ORDER BY strength DESC LIMIT ?", 30)
    for project in projects:
        project['people'] = [n['name'] for n in neighbours(db, project['id'], 8) if n['kind'] == 'person'][:4]
    return {'built_at': state['built_at'] if state else None,
            'projects': projects,
            'people': rows("SELECT * FROM graph_nodes WHERE kind='person' ORDER BY strength DESC LIMIT ?", limit),
            'quiet': rows("SELECT * FROM graph_nodes WHERE kind='person' AND quiet=1 ORDER BY strength DESC LIMIT ?", limit),
            'new': rows("SELECT * FROM graph_nodes WHERE kind='person' AND signals<=3 AND last_at>=? ORDER BY strength DESC LIMIT ?",
                        week_ago, 8),
            'orgs': rows("SELECT * FROM graph_nodes WHERE kind='org' ORDER BY strength DESC LIMIT ?", 8)}


def detail(db, node_id, events=12):
    row = db.execute('SELECT * FROM graph_nodes WHERE id=?', (node_id,)).fetchone()
    if not row:
        return None
    view = node_view(row, neighbours(db, node_id, 20))
    view['event_ids'] = [r[0] for r in db.execute('SELECT event_id FROM graph_events WHERE node_id=? ORDER BY at DESC LIMIT ?', (node_id, events))]
    return view


# Writing labels and merges.

def project_id(db, name, now):
    name = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not name or len(name) > 60:
        raise ValueError('A project name is a short label (up to 60 characters), like "Doctura VAT" or "Q4 board deck"')
    row = db.execute('SELECT id,closed_at FROM projects WHERE name=? COLLATE NOCASE', (name,)).fetchone()
    if row:
        if row['closed_at']:
            db.execute('UPDATE projects SET closed_at=NULL WHERE id=?', (row['id'],))  # it came back
        return row['id']
    return db.execute('INSERT INTO projects(name,created_at) VALUES(?,?)', (name, now)).lastrowid


def label(db, event_ids, name, source, now):
    pid = project_id(db, name, now)
    db.executemany('INSERT OR IGNORE INTO event_projects(event_id,project_id,source,created_at) VALUES(?,?,?,?)',
                   [(int(e), pid, source, now) for e in event_ids])
    return pid


def active_projects(db, limit=40):
    """Project names to reuse: the graph's live ones first, then recently named ones it hasn't seen yet."""
    names = [r[0] for r in db.execute("SELECT name FROM graph_nodes WHERE kind='project' AND strength>=? AND json_extract(info,'$.closed_at') IS NULL ORDER BY strength DESC LIMIT ?", (0.1, limit))]
    names += [r[0] for r in db.execute('SELECT name FROM projects WHERE closed_at IS NULL ORDER BY created_at DESC LIMIT ?', (limit,)) if r[0] not in names]
    return names[:limit]


def merge(db, a, b, same, now):
    a, b = sorted((str(a), str(b)))
    if a == b or not all(re.fullmatch(r'(?:email|wa|slack|li|x|gh):\S+', i) for i in (a, b)):
        raise ValueError('Merge two different person ids from graph results (like email:… or wa:…)')
    db.execute('INSERT OR REPLACE INTO graph_merges(a,b,same,created_at) VALUES(?,?,?,?)', (a, b, int(bool(same)), now))
