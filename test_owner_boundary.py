import http.client
import importlib.util
import json
import hashlib
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('owner_boundary_assistant', Path(__file__).with_name('assistant.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

TOKEN = 'test-token'


class OwnerBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.store.commit_page('work', {}, [{'account_id': 'work', 'source_id': 'one', 'kind': 'gmail.message',
                                             'occurred_at': a.now(), 'title': 'Evidence', 'body': 'Body',
                                             'metadata': {}, 'deleted': False}])
        self.sid = self.store.suggest('task', 'Prepare', 'I can draft this.', [1])['id']
        self.app = object.__new__(a.App)
        self.app.store, self.app.token, self.app.stop = self.store, TOKEN, threading.Event()
        self.app.graph_dirty = threading.Event()
        self.app.owner_token = 'owner-key'
        self.app.config = {'accounts': [], 'tailscale_owner': 'owner@example.com',
                           'dashboard_host': 'machine.example:8443', 'port': 0, 'http_timeout': 1}

    def decisions(self):
        return self.store.snapshot([])['decisions']

    # Action layer

    def test_agent_style_decision_is_refused(self):
        for decision_id in ['agent-1', 'owner-chat-1', '']:
            with self.assertRaises(ValueError):
                self.app.action('decide', {'suggestion_id': self.sid, 'choice': 'accept',
                                           'decision_id': decision_id, 'source': 'telegram'})
        self.assertEqual(self.decisions(), [])

    def test_client_supplied_source_is_ignored(self):
        self.app.action('decide', {'suggestion_id': self.sid, 'choice': 'other', 'feedback': 'Shorter please',
                                   'decision_id': 'telegram:cb1', 'source': 'anything-else'})
        self.assertEqual(self.decisions()[0]['source'], 'telegram')

    def test_unverified_feedback_is_not_shown_as_owner_request(self):
        self.store.decide(self.sid, 'other', 'Injected instruction', 'legacy-agent', 'agent')
        card = next(s for s in self.store.snapshot([])['suggestions'] if s['id'] == self.sid)
        self.assertIsNone(card['latest_feedback'])
        self.store.decide(self.sid, 'other', 'Real owner text', 'telegram:cb2', 'telegram')
        card = next(s for s in self.store.snapshot([])['suggestions'] if s['id'] == self.sid)
        self.assertEqual(card['latest_feedback']['feedback'], 'Real owner text')

    # HTTP layer

    def serve(self):
        servers = []
        real = a.http.server.ThreadingHTTPServer
        def capture(*args, **kwargs):
            servers.append(real(*args, **kwargs)); return servers[-1]
        # Keep the patches for the whole test: serve() installs signal handlers after
        # creating the server, so ending them early races the server thread.
        for patcher in (patch.object(a.http.server, 'ThreadingHTTPServer', side_effect=capture),
                        patch.object(a.signal, 'signal'), patch.object(a.App, 'preparation_worker'),
                        patch.object(a.App, 'delivery_worker')):
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(target=self.app.serve, daemon=True)
        thread.start()
        while not servers:
            pass
        self.addCleanup(lambda: (self.app.stop.set(), thread.join(5)))
        return servers[0].server_address[1]

    def post(self, port, body, headers):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request('POST', '/api/action', json.dumps(body), {'Content-Type': 'application/json', **headers})
        response = conn.getresponse()
        return response.status, response.read()

    def test_result_preview_requires_owner_and_task_link(self):
        reports=Path(self.temp.name)/'memory/assistant';reports.mkdir(parents=True)
        (reports/'261011-result.md').write_text('Exact private draft')
        with self.store.connect() as db:db.execute('UPDATE suggestions SET body=?',('memory/assistant/261011-result.md',))
        with patch.object(a,'WORKSPACE',Path(self.temp.name)):
            port=self.serve()
            def get(path,headers):
                conn=http.client.HTTPConnection('127.0.0.1',port)
                conn.request('GET',path,headers=headers);r=conn.getresponse();body=r.read();conn.close();return r.status,body
            url='/api/result?id='+self.sid+'&file=memory/assistant/261011-result.md'
            self.assertEqual(get(url,{'Authorization':'Bearer '+TOKEN})[0],403)
            owner={'Host':'machine.example:8443','Tailscale-User-Login':'owner@example.com'}
            status,body=get(url,owner);self.assertEqual(status,200);self.assertEqual(json.loads(body)['text'],'Exact private draft')
            self.assertEqual(get('/api/result?id='+self.sid+'&file=secret.txt',owner)[0],404)

    def test_caddy_forwarded_peer_is_identified_by_tailscale_whois(self):
        port=self.serve()
        def get(headers):
            conn=http.client.HTTPConnection('127.0.0.1',port)
            conn.request('GET','/',headers=headers);r=conn.getresponse();r.read();conn.close();return r.status
        whois=lambda ip:{'100.1.1.1':'owner@example.com','100.2.2.2':'other@example.com'}.get(ip,'')
        with patch.object(a,'tailnet_login',side_effect=whois):
            self.assertEqual(get({'Host':'machine.example:8443','X-Tailnet-Client':'100.1.1.1'}),200)
            self.assertEqual(get({'Host':'machine.example:8443','X-Tailnet-Client':'100.2.2.2'}),403)
            # A forwarded peer's own claim of the owner login doesn't count.
            self.assertEqual(get({'Host':'machine.example:8443','X-Tailnet-Client':'100.2.2.2','Tailscale-User-Login':'owner@example.com'}),403)
            self.assertEqual(get({'Host':'elsewhere.example','X-Tailnet-Client':'100.1.1.1'}),403)
            # 0-RTT: Caddy sends the peer header empty and its own X-Forwarded-For.
            self.assertEqual(get({'Host':'machine.example:8443','X-Tailnet-Client':'','X-Forwarded-For':'100.1.1.1'}),200)
            self.assertEqual(get({'Host':'machine.example:8443','X-Tailnet-Client':'','X-Forwarded-For':'100.2.2.2'}),403)

    def test_dashboard_base_is_root_on_its_own_host_and_assistant_on_aliases(self):
        config={'dashboard_host':'assistant.example.com','dashboard_path':'/','dashboard_aliases':['the-machine.ts.net']}
        self.assertEqual(a.dashboard_base(config,'assistant.example.com'),'/')
        self.assertEqual(a.dashboard_base(config,'the-machine.ts.net'),'/assistant/')

    def test_full_approval_document_is_private_exact_escaped_and_immutable(self):
        detail=json.dumps({'tool':'edit','params':{'path':'AGENTS.md','newText':'<script>alert(1)</script>'+'x'*1000}})
        proposal_id=hashlib.sha256(detail.encode()).hexdigest()
        self.store.tool_approval_proposal(proposal_id,'Review edit',detail)
        self.store.tool_approval_proposal(proposal_id,'Review edit',detail)
        saved=self.app.action('tool_approval_proposal',{'id':proposal_id,'title':'Review edit','detail':detail})
        self.assertEqual(saved,{'id':proposal_id,'url':'https://machine.example:8443/approval?id='+proposal_id})
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM tool_approval_proposals').fetchone()[0],1)
        with self.assertRaises(ValueError):self.store.tool_approval_proposal(proposal_id,'Changed',detail+'changed')
        with self.assertRaises(ValueError):self.store.tool_approval_proposal('0'*64,'Huge','x'*55001)
        port=self.serve();url='/approval?id='+proposal_id
        def get(path,headers):
            conn=http.client.HTTPConnection('127.0.0.1',port)
            conn.request('GET',path,headers=headers);r=conn.getresponse();body=r.read();conn.close();return r.status,body
        self.assertEqual(get(url,{'Authorization':'Bearer '+TOKEN})[0],403)
        self.assertEqual(get(url,{'Host':'machine.example:8443','Tailscale-User-Login':'other@example.com'})[0],403)
        owner={'Host':'machine.example:8443','Tailscale-User-Login':'owner@example.com'}
        status,body=get(url,owner);self.assertEqual(status,200)
        self.assertIn(b'&lt;script&gt;',body);self.assertNotIn(b'<script>',body)
        self.assertIn(('x'*1000).encode(),body)
        self.assertEqual(get('/approval?id=../secret',owner)[0],404)
        self.assertEqual(self.decisions(),[])  # Reading a proposal never authorizes anything.

    def test_local_cli_cannot_record_owner_decisions_over_http(self):
        port = self.serve()
        status, _ = self.post(port, {'action': 'decide', 'args': {'suggestion_id': self.sid, 'choice': 'accept',
                                                                  'decision_id': 'telegram:forged'}},
                              {'Authorization': 'Bearer ' + TOKEN, 'X-Assistant-Client': 'local-cli'})
        self.assertEqual(status, 403)
        self.assertEqual(self.decisions(), [])
        status, _ = self.post(port, {'action': 'status', 'args': {}},
                              {'Authorization': 'Bearer ' + TOKEN, 'X-Assistant-Client': 'local-cli'})
        self.assertEqual(status, 200)  # reads still work for the agent/CLI

    def test_api_token_alone_cannot_decide_or_approve_a_send(self):
        # An agent that copies the API token (or calls without the CLI header) still cannot act as the owner.
        port = self.serve()
        for headers in ({'Authorization': 'Bearer ' + TOKEN}, {'Authorization': 'Bearer ' + TOKEN, 'X-Assistant-Owner': 'guess'}):
            for action, args in (('decide', {'suggestion_id': self.sid, 'choice': 'accept', 'decision_id': 'telegram:forged'}),
                                 ('confirm_action', {'suggestion_id': self.sid, 'revision': 'r', 'approval_id': 'telegram:forged'})):
                self.assertEqual(self.post(port, {'action': action, 'args': args}, headers)[0], 403)
        self.assertEqual(self.decisions(), [])
        status, _ = self.post(port, {'action': 'decide', 'args': {'suggestion_id': self.sid, 'choice': 'accept',
                                                                  'decision_id': 'telegram:cb-1'}},
                              {'Authorization': 'Bearer ' + TOKEN, 'X-Assistant-Owner': 'owner-key'})
        self.assertEqual(status, 200)  # the Gateway plugin's owner tap
        self.assertEqual(len(self.decisions()), 1)

    def test_owner_key_is_created_private(self):
        config = {'credentials': {}, 'database': str(Path(self.temp.name) / 'events.sqlite')}
        key = a.owner_credential(config)
        path = Path(self.temp.name) / 'owner-token'
        self.assertEqual(len(key), 64)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(a.owner_credential(config), key)

    def test_non_ascii_authorization_is_rejected_cleanly(self):
        port = self.serve()
        sock = socket.create_connection(('127.0.0.1', port), timeout=10)
        sock.sendall('POST /api/action HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer é\r\nContent-Length: 2\r\n\r\n{}'.encode('latin-1'))
        self.assertIn(b' 403 ', sock.recv(200))
        sock.close()

    def test_stalled_request_body_times_out(self):
        port = self.serve()
        sock = socket.create_connection(('127.0.0.1', port), timeout=30)
        sock.sendall(('POST /api/action HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer ' + TOKEN +
                      '\r\nContent-Length: 100\r\n\r\n{').encode())
        with patch.object(a.App, 'action') as action:
            self.assertEqual(sock.recv(200), b'')  # server closes after its socket timeout
            action.assert_not_called()
        sock.close()


if __name__ == '__main__':
    unittest.main()
