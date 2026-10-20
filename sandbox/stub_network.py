"""A stand-in for The Machine Network, for the sandbox: the same endpoints the app calls (/directory, /inbox, /ask,
/answer, /decide, /claim(s), /friends, /me), answered from memory. Teammates and friends are the fixture's invented
people; their Machines answer a question a few seconds after it is asked, so asking, handing off and adding friends
behave as they would. Nothing leaves this machine."""
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER_AFTER = 3  # seconds before a teammate's Machine answers


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())


class Network:
    def __init__(self, fixture, groups, token):
        self.net, self.groups, self.token = fixture['network'], set(groups), token
        self.lock = threading.Condition()
        self.inbox = {'asks': [], 'answers': [], 'notices': []}
        self.answered = []  # what this Machine answered teammates, for tests
        self.friends = [dict(f) for f in self.net['friends']['friends']]
        self.requests_in = [{'id': 'frq_in_0001', **self.net['friends']['request_in'], 'at': now()}]
        self.requests_out = [{'id': 'frq_out_0001', 'username': self.net['friends']['request_out'], 'at': now()}]
        self.blocked = []
        self.undelivered = set()  # questions to a teammate without a Machine
        self.keys = None  # this Machine's public keys, as it published them (POST /keys)
        self.notices = {n['id']: {**n, 'at': now()} for n in self.net['notices']}

    # -- what the app reads
    def me(self, emails=None):
        owner = self.net['owner']
        return {'member_id': owner['member_id'], 'name': owner['name'], 'role': 'owner', 'emails': [owner['email']], 'machine': True,
                'account_email': owner['email'], 'label': owner['name'], 'me': True, **({'keys': self.keys} if self.keys else {})}

    def member(self, m):
        return {'member_id': m['member_id'], 'name': m['name'], 'role': m.get('role', 'member'), 'emails': [m['email']] if m.get('email') else [],
                'machine': m.get('machine', True), 'account_email': m.get('email', ''), 'label': m['name'],
                **({} if m.get('machine', True) else {'answers_with': 'no Machine yet'})}

    def directory(self):
        fleets = []
        for kind in ('work', 'family'):
            if kind in self.groups:
                group = self.net[kind]
                fleets.append({'id': group['id'], 'kind': kind, 'name': group['name'], 'asks': ['relationships', 'meeting', 'brief', 'anything',
                               'intro', 'handoff', 'followup'] if kind == 'work' else [], 'me': {'member_id': self.net['owner']['member_id'], 'role': 'owner'},
                               'members': [self.me()] + [self.member(m) for m in group['team']]})
        if 'friends' in self.groups:
            fleets.append({'id': self.net['friends']['id'], 'kind': 'friends', 'name': 'Friends', 'username': self.net['owner']['username'],
                           'asks': ['availability'], 'me': {'member_id': self.net['owner']['member_id'], 'role': 'owner'},
                           'members': [self.me()] + [{'member_id': f['member_id'], 'name': f['name'], 'role': 'friend', 'emails': [], 'machine': True,
                                                      'label': f['name'] + ' (@' + f['username'] + ')'} for f in self.friends],
                           'requests': {'in': list(self.requests_in), 'out': list(self.requests_out)}, 'blocked': list(self.blocked)})
        return {'fleets': fleets}

    def group_of(self, fleet_id):
        for kind in ('work', 'family'):
            if kind in self.groups and fleet_id in (self.net[kind]['id'], kind):
                return self.net[kind]['team']
        if 'friends' in self.groups and fleet_id in (self.net['friends']['id'], 'friends'):
            return [{'member_id': f['member_id'], 'name': f['name']} for f in self.friends]
        return []

    # -- simulated teammates
    def reply(self, kind, index):
        if kind == 'relationships':
            return {'knows': True, 'people': 2, 'closeness': 'close', 'last_contact': 'last week'} if index == 0 else {'knows': False}
        if kind == 'anything':
            return {'text': ['Not that I know of. Tomasz met them in September, I think.', 'Yes, I spoke to their ops lead on Monday.'][index % 2]}
        if kind in ('intro', 'handoff', 'followup'):
            return {'stage': 'accepted'}
        if kind == 'availability':
            return {'busy': []}
        return None

    def answer_later(self, ids, kind):
        def later():
            time.sleep(ANSWER_AFTER)
            with self.lock:
                for index, (member_id, ask_id) in enumerate(ids.items()):
                    answer = self.reply(kind, index)
                    self.inbox['answers'].append({'id': ask_id, 'status': 'answered' if answer is not None else 'declined',
                                                  **({'answer': answer} if answer is not None else {}), 'receipt': secrets.token_hex(8)})
                self.lock.notify_all()
        threading.Thread(target=later, daemon=True).start()

    def queue(self, asks=(), notices=()):
        with self.lock:
            self.inbox['asks'] += list(asks)
            self.inbox['notices'] += list(notices)
            self.lock.notify_all()

    # -- the endpoints
    def handle(self, method, path, query, body):
        if method == 'GET' and path == '/directory':
            return self.directory()
        if method == 'POST' and path == '/me':
            return {}
        if method == 'POST' and path == '/keys':  # teammates here have no keys, so they ask and answer in plaintext
            self.keys = body
            return {}
        if method == 'GET' and path == '/inbox':
            wait = min(int((query.get('wait') or ['0'])[0] or 0), 10)
            with self.lock:
                if not any(self.inbox.values()):
                    self.lock.wait(wait)
                found, self.inbox = self.inbox, {'asks': [], 'answers': [], 'notices': []}
            return {**found, 'ack': True}
        if method == 'POST' and path in ('/ack', '/answer'):
            if path == '/answer':
                self.answered.append(body)
            return {}
        if method == 'POST' and path == '/ask':
            fleet = body.get('fleet_id') or body.get('fleet') or 'work'
            team = [m for m in self.group_of(fleet) if not body.get('to') or m['member_id'] in body['to']]
            ids = {m['member_id']: 'fa_' + secrets.token_hex(8) for m in team}
            machine = {t['member_id']: t.get('machine', True) for t in team}
            self.undelivered |= {i for m, i in ids.items() if not machine[m]}
            self.answer_later({m: i for m, i in ids.items() if machine[m]}, body.get('kind'))
            return {'ids': ids}
        if method == 'POST' and path == '/decide':
            notice = self.notices.get(body.get('id'))
            if not notice:
                return {'status': 'stale', 'reason': 'Already settled'}
            status = 'confirmed' if body.get('choice') == 'confirm' else 'declined'
            notice.update(status=status, version=notice['version'] + 1, buttons=[], outcome='Confirmed' if status == 'confirmed' else 'Declined')
            return {'status': status, 'notice': notice}
        if method == 'POST' and path == '/delivery':  # a teammate without a Machine never gets the question
            return {'delivery': {i: {'delivered': i not in self.undelivered} for i in body.get('ids', [])}}
        if method == 'POST' and path == '/claims':
            return {'claims': {}}
        if method == 'POST' and path == '/claim':
            return {'mine': not body.get('release')}
        if method == 'POST' and path == '/friends':
            return self.friend(body)
        return None

    def friend(self, body):
        action = body.get('action')
        if action == 'add':
            self.requests_out.append({'id': 'frq_' + secrets.token_hex(6), 'username': body.get('username', ''), 'at': now()})
        elif action in ('accept', 'decline', 'block') and body.get('id'):
            found = next((r for r in self.requests_in if r['id'] == body['id']), None)
            self.requests_in = [r for r in self.requests_in if r['id'] != body['id']]
            if found and action == 'accept':
                self.friends.append({'member_id': 'm_fr_' + secrets.token_hex(4), 'name': found['name'], 'username': found['username']})
            if found and action == 'block':
                self.blocked.append({'member_id': 'm_fr_' + secrets.token_hex(4), 'name': found['name']})
        elif action == 'withdraw':
            self.requests_out = [r for r in self.requests_out if r['id'] != body.get('id')]
        elif action in ('remove', 'block') and body.get('member_id'):
            gone = next((f for f in self.friends if f['member_id'] == body['member_id']), None)
            self.friends = [f for f in self.friends if f['member_id'] != body['member_id']]
            if gone and action == 'block':
                self.blocked.append({'member_id': gone['member_id'], 'name': gone['name']})
        elif action == 'unblock':
            self.blocked = [b for b in self.blocked if b['member_id'] != body.get('member_id')]
        return {'ok': True}


def serve(network, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self):
            url = urllib.parse.urlsplit(self.path)
            path = url.path.removeprefix('/api/fleet')
            if self.headers.get('Authorization') != 'Bearer ' + network.token:
                return self.send(403, {'error': 'Not a Machine in this sandbox'})
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
            found = network.handle(self.command, path, urllib.parse.parse_qs(url.query), body)
            if found is None:
                print(f'sandbox network: no stand-in for {self.command} {path}', file=sys.stderr, flush=True)
                return self.send(404, {'error': 'Not here'})
            self.send(200, found)

        def send(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = respond

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
