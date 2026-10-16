"""The demo must stay local and never admit real service actions or private files."""
import http.client
import json
import threading
import unittest
from unittest import mock
import demo


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.server=demo.DemoServer(0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.connection=http.client.HTTPConnection(*self.server.server_address)
        self.addCleanup(self.connection.close)

    def request(self,path,body=None,origin=True):
        headers={'Content-Type':'application/json'}
        if origin:
            headers['Origin']=f'http://127.0.0.1:{self.server.server_port}'
        self.connection.request('GET' if body is None else 'POST',path,
                                None if body is None else json.dumps(body),headers)
        response=self.connection.getresponse()
        return response.status,response.read()

    def test_local_synthetic_dashboard_and_files_only(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        status,body=self.request('/')
        self.assertEqual(status,200)
        self.assertIn(b'Sample data',body)
        status,body=self.request('/api/status')
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(body)['suggestions']),4)
        self.assertEqual(self.request('/brand/avatar.png')[0],200)
        self.assertEqual(self.request('/brand/favicon-512.png')[0],200)
        for path in ['/config.json','/assistant.py','/brand/../config.example.json','/api/token']:
            self.assertEqual(self.request(path)[0],404,path)

    def test_actions_are_memory_only_and_live_actions_are_rejected(self):
        task=self.server.state['suggestions'][1]
        with mock.patch('subprocess.run',side_effect=AssertionError('No commands in demo')):
            status,body=self.request('/api/action',{'action':'decide','args':{'suggestion_id':task['id'],'choice':'accept'}})
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)['status'],'accepted')
            self.assertEqual(task['status'],'accepted')
            status,_=self.request('/api/action',{'action':'confirm_action','args':{'suggestion_id':task['id']}})
            self.assertEqual(status,400)
            status,_=self.request('/api/action',{'action':'decide','args':{'suggestion_id':task['id'],'choice':'complete'}},origin=False)
            self.assertEqual(status,403)

if __name__=='__main__':
    unittest.main()
