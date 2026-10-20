"""The sandbox: the real dashboard and app on made-up data, with a stand-in Network, sealed off from the world.

    python3 sandbox/run.py --state busy            # then open http://127.0.0.1:8870
    python3 sandbox/run.py --state fleet,friends --port 8880

What runs: assistant.App from this checkout, on a fresh database in --dir (default /tmp/machine-sandbox-<port>) built
from sandbox/fixture.json; sandbox/stub_network.py as the Network when the state has a Fleet or friends; and a small proxy on
--port that signs every request in as the owner, so a browser (or Playwright) on this machine can click everything.

What doesn't: no collector, review, preparation, delivery, model or enrichment worker runs. An approved send (email,
Slack, WhatsApp, LinkedIn, X) goes to <dir>/sent.jsonl instead, and the phone and Telegram notifications too. guard.py
refuses any connection beyond 127.0.0.1 and any program start, whatever the code path."""
import argparse
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guard  # noqa: E402
import load  # noqa: E402
import stub_network as network  # noqa: E402

for name in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(name, None)


class Sent:
    """Where the sandbox's sends go: one JSON line each."""

    def __init__(self, path):
        self.path, self.lock, self.count = Path(path), threading.Lock(), 0

    def record(self, channel, value):
        with self.lock:
            self.count += 1
            with self.path.open('a') as handle:
                handle.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'channel': channel, 'value': value}, default=str) + '\n')
            return self.count


class FakeTransport:
    """Stands in for next_actions.ActionTransport: every approved send is recorded and reported sent."""

    def __init__(self, sent):
        self.sent = sent

    def pick(self, action):
        return self

    def preflight(self, action):
        return None

    def send(self, action, job):
        n = self.sent.record(action.get('kind', 'email'), action)
        return {'message_id': f'sandbox-{n}', 'thread_id': f'sandbox-thread-{n}'}

    def reconcile(self, action, job):
        return {'message_id': job.get('message_id') or 'sandbox-reconciled'}


def quiet_workers(app, sent):
    """Turns off everything that would read a real source, call a model or reach the owner, and fakes the sends."""
    import assistant
    for name in ('collector', 'delivery_worker', 'preparation_worker', 'review_worker', 'graph_worker', 'model_usage_worker',
                 'embedding_worker', 'enrichment_worker'):
        setattr(app, name, lambda *a, **k: None)
    transport = FakeTransport(sent)
    original = app.action_step
    app.action_step = lambda transport_=None: original(transport)
    import onboarding
    onboarding.missing = lambda config, account: ''  # the made-up accounts count as set up; there is nothing to sign in to
    assistant.push_send = lambda store, config, subscriptions, payload, label, sender=None: sent.record('push', payload)
    assistant.fleet_push = lambda store, config, rows, sender=None: sent.record('push', {'fleet_questions': len(rows)})
    assistant.fleet_notice_push = lambda store, config, view, sender=None: sent.record('push', view)
    assistant.fleet_telegram = lambda config, message, message_id=None, request=None: sent.record('telegram', message) and {}
    assistant.telegram = lambda config, row, action, message_id=None: sent.record('telegram', {'card': row.get('id') if hasattr(row, 'get') else None}) and {}


def proxy(public_port, app_port, config):
    """Signs each request in as the owner on the dashboard's own host (what Tailscale Serve does for real)."""
    host, owner = config['dashboard_host'], config['tailscale_owner']

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def forward(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'origin', 'tailscale-user-login', 'x-tailnet-client',
                                                                                    'x-forwarded-for', 'authorization', 'connection')}
            headers.update({'Host': host, 'Tailscale-User-Login': owner})
            if self.command == 'POST':
                headers['Origin'] = 'https://' + host
            connection = http.client.HTTPConnection('127.0.0.1', app_port, timeout=120)
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
            except OSError:
                data, response = b'{"error":"The sandbox app is not answering"}', None
            self.send_response(response.status if response else 502)
            for k, v in (response.getheaders() if response else [('Content-Type', 'application/json')]):
                if k.lower() not in ('content-length', 'connection', 'transfer-encoding'):
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            connection.close()

        do_GET = do_POST = do_HEAD = forward

    server = http.server.ThreadingHTTPServer(('127.0.0.1', public_port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def seed_network(app, net, fixture):
    """What the Network had waiting: teammates' questions and requests, a seat request, and an answered question of the owner's."""
    groups_ids = {'work': fixture['network']['work']['id'], 'family': fixture['network']['family']['id']}
    names = {m['member_id']: m['name'] for g in ('work', 'family') for m in fixture['network'][g]['team']}
    asks = [{'id': a['id'], 'kind': a['kind'], 'fleet_id': groups_ids[a['fleet']], 'from': a['from'], 'name': names.get(a['from'], ''),
             'question': a['question']} for a in fixture['network']['asks'] if a['fleet'] in ('work', 'family') and
            {'work': 'work', 'family': 'family'}[a['fleet']] in net.groups]
    notices = [n for n in net.notices.values() if n['fleet'] in net.groups]
    net.queue(asks, notices)
    if 'work' in net.groups:
        for asked in fixture['network']['asked']:
            def ask(asked=asked):
                for _ in range(20):
                    try:
                        app.fleet.directory(refresh=True)
                        return app.fleet.ask(asked['kind'], asked['question'])
                    except Exception:  # the Fleet still starting up
                        time.sleep(0.5)
            threading.Thread(target=ask, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', default='busy', help='one or more of ' + ', '.join(load.STATES) + ' (comma separated)')
    parser.add_argument('--port', type=int, default=8870, help='the dashboard port; the Network uses port+1 and the app port+2')
    parser.add_argument('--dir', help='where the sandbox install goes (emptied first)')
    args = parser.parse_args()
    states = load.parse_states(args.state)
    for port in (args.port, args.port + 1, args.port + 2):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'port {port} is in use (another sandbox?); pick another with --port')
    directory = Path(args.dir or f'/tmp/machine-sandbox-{args.port}')
    config, config_path = load.load(directory, states, port=args.port + 2, network_port=args.port + 1)
    os.environ['OPENCLAW_WORKSPACE'] = str(directory / 'workspace')
    guard.install()  # from here on: nothing but 127.0.0.1, no programs
    import assistant
    sent = Sent(directory / 'sent.jsonl')
    app = assistant.App(config, config_path)
    quiet_workers(app, sent)
    fixture = json.loads((HERE / 'fixture.json').read_text())
    net = None
    if load.groups(states):
        net = network.Network(fixture, load.groups(states), load_secret(config, 'fleet'))
        network.serve(net, args.port + 1)
    proxy(args.port, args.port + 2, config)
    if net and app.fleet:
        seed_network(app, net, fixture)
    print(f"sandbox ({','.join(sorted(states))}) on http://127.0.0.1:{args.port}  data: {directory}  sends: {directory / 'sent.jsonl'}", flush=True)
    app.serve()


def load_secret(config, name):
    return Path(config['credentials'][name]).read_text().strip()


if __name__ == '__main__':
    main()
