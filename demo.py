#!/usr/bin/env python3
"""A local, disposable dashboard demo. No accounts, credentials, database or OpenClaw."""
import argparse
import datetime as dt
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
VENUE_RESULT = '## Venue comparison\n\n| Venue | Cost | Access |\n|---|---|---|\n| Riverside | €180 | Step-free |\n| Garden room | €240 | Two steps |\n| Community hall | €120 | Step-free |\n\nRiverside is the best fit for the group.\n\nThis is synthetic demo content.'


def sample_state():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat()
    titles = ['Compare the venue options', 'Reply about the project kickoff',
              'Check the renewal terms', 'Plan the weekend walk']
    bodies = ['The three options are compared below. The riverside venue fits the budget and has step-free access.',
              'Sam asked whether Tuesday at 10:00 works. Check your availability and prepare a short reply.',
              'The renewal is next month. Gather the cancellation deadline and the current plan details.',
              'A friend suggested a walk on Saturday. Pick this up tomorrow.']
    suggestions = []
    for index, (title, body) in enumerate(zip(titles, bodies)):
        status = ['prepared', 'pending', 'accepted', 'snoozed'][index]
        suggestions.append({'id':f'{index+1:016x}', 'title':title, 'body':body,
                            'status':status, 'scope':'personal' if index in (0,3) else 'work',
                            'created_at':stamp, 'source_line':'Sample data · ' + ['Email from Taylor', 'Email from Sam', 'Calendar', 'WhatsApp from Alex'][index],
                            'preparation_status':'done' if index==0 else 'running' if index==2 else None,
                            'result_files':[{'name':'venue-comparison.md','path':'demo/venue-comparison.md'}] if index==0 else [],
                            'snoozed_until':(now+dt.timedelta(days=1)).isoformat() if index==3 else None})
    connectors = [{'id':'demo-email','service':'gmail','display_name':'Sample email',
                   'email':'owner@example.com','boundary':'work:Example','scope':'work',
                   'last_success':stamp,'state':'{}','failures_24h':0},
                  {'id':'demo-calendar','service':'calendar','display_name':'Sample calendar',
                   'email':'owner@example.com','boundary':'work:Example','scope':'work',
                   'last_success':stamp,'state':'{}','failures_24h':0}]
    return {'suggestions':suggestions,'connectors':connectors,'suggestion_counts':{},
            'decisions':[],'analyses':[],'deliveries':[],'facts':[],
            'review_health':{'pending':0,'total':24,'last_completed':stamp},'review_runs':[]}


class DemoServer(ThreadingHTTPServer):
    def __init__(self, port=8788):
        self.state = sample_state()
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', port), DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, value, status=200, mime='application/json'):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == '/':
            html = (ROOT / 'dashboard.html').read_text()
            html = html.replace('<main>', '<main><aside style="margin:16px 0;padding:12px 16px;border:1px solid var(--line);border-radius:12px">Demo · Sample data. Buttons only change this local demo; nothing is sent or run.</aside>', 1)
            # The demo never installs a service worker or offers push subscriptions.
            html = html.replace("const pushOK='serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;", "const pushOK=false;")
            self.respond(html.encode(), mime='text/html; charset=utf-8')
        elif path.startswith('/brand/'):
            name = path.removeprefix('/brand/')
            aliases = {'avatar.png':'app-icon-192.png','favicon-32.png':'app-icon-192.png',
                       'favicon-512.png':'app-icon-maskable-512.png','sleepy.webp':'app-icon-192.png',
                       'sad.webp':'app-icon-192.png','thinking.webp':'app-icon-192.png'}
            file = ROOT / 'brand' / aliases.get(name,name)
            if file.parent != ROOT / 'brand' or not file.is_file():
                self.respond({'error':'Not found'}, 404)
            else:
                self.respond(file.read_bytes(), mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        elif path == '/manifest.webmanifest':
            self.respond((ROOT / 'manifest.webmanifest').read_bytes(), mime='application/manifest+json')
        elif path == '/api/status':
            with self.server.lock:
                self.respond(self.server.state)
        elif path == '/api/events':
            self.respond([{'id':1,'kind':'email','title':'Project kickoff',
                           'occurred_at':self.server.state['suggestions'][0]['created_at'],
                           'body':'Hi! Would Tuesday at 10:00 work for the kickoff?',
                           'metadata':{'from':'sam@example.com'}}])
        elif path == '/api/originals':
            self.respond({'originals':[{'kind':'email','event_id':1,'title':'Project kickoff',
                          'from':'Sam Example <sam@example.com>','to':'owner@example.com','cc':'',
                          'occurred_at':self.server.state['suggestions'][0]['created_at'],
                          'body':'Hi! Would Tuesday at 10:00 work for the kickoff?','attachments':[]}]})
        elif path == '/api/email':
            self.respond({'html':''})
        elif path == '/api/result':
            self.respond({'name':'venue-comparison.md','text':VENUE_RESULT})
        elif path == '/result-file' and query.get('file')==['demo/venue-comparison.md']:
            self.respond(VENUE_RESULT.encode(), mime='text/markdown; charset=utf-8')
        elif path == '/api/progress':
            self.respond({'status':'running','started_at':None,'steps':[{'title':'Read the renewal notice','at':''},{'title':'Compare the plan terms','at':''}]})
        else:
            self.respond({'error':'Unavailable in the demo'}, 404)

    def do_POST(self):
        # Browser actions are an in-memory simulation; unknown actions fail closed.
        if self.path != '/api/action':
            self.respond({'error':'Unavailable in the demo'}, 404)
            return
        host = self.headers.get('Host')
        if host != f'127.0.0.1:{self.server.server_port}' or self.headers.get('Origin') != f'http://{host}':
            self.respond({'error':'Open the demo on its printed localhost URL'}, 403)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            args = data['args']
            if data['action'] != 'decide' or not isinstance(args, dict):
                raise ValueError()
            with self.server.lock:
                task = next(t for t in self.server.state['suggestions'] if t['id']==args['suggestion_id'])
                choice = args['choice']
                status = {'accept':'accepted','complete':'completed','reject':'rejected','other':'instructions',
                          'snooze':'snoozed','retry':'accepted','undo':task.get('closed_from','pending'),
                          'reopen':'pending'}[choice]
                if choice in ('complete','reject'):
                    task['closed_from'] = task['status']
                    task['undo_until'] = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5)).isoformat()
                task['status'] = status
                if choice in ('accept','other','retry'):
                    task['preparation_status'] = 'running'
                if choice == 'snooze':
                    task['snoozed_until'] = args.get('snoozed_until') or (dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat()
                self.server.state['decisions'].append({'suggestion_id':task['id'],'choice':choice,
                    'feedback':str(args.get('feedback',''))[:1000],'source':'demo',
                    'created_at':dt.datetime.now(dt.timezone.utc).isoformat()})
            self.respond({'demo':True})
        except (ValueError, KeyError, TypeError, StopIteration):
            self.respond({'error':'This action is unavailable in the demo; nothing was sent or run'}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8788)
    args = parser.parse_args()
    with DemoServer(args.port) as server:
        print(f'Demo: http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop; changes disappear)',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
