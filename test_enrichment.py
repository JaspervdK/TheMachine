import http.server
import json
import threading
import unittest
from pathlib import Path

import test_graph as tg

a, graph = tg.a, tg.graph
enrichment = a.enrichment
KEY = 'not-a-real-key'  # a test value; a realistic-looking one trips secret scanners


class Service(http.server.BaseHTTPRequestHandler):
    """A fake enrichment service: Globex is known, nobody else; a wrong key is refused."""
    seen = []

    def do_GET(self):
        Service.seen.append((self.path, self.headers.get('Authorization')))
        if self.headers.get('Authorization') != 'Bearer ' + KEY:
            self.send_response(401); self.end_headers(); return
        if 'globex.com' not in self.path:
            self.send_response(404); self.end_headers(); return
        body = json.dumps({'name': 'Globex', 'industry': 'Manufacturing', 'size': '51-200', 'headquarters': 'Springfield', 'founded': '1989',
                           'about_us': 'Globex makes   everything.', 'linkedin_url': 'https://www.linkedin.com/company/globex'}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


class Enrichment(unittest.TestCase):
    add = tg.Graph.add
    build = tg.Graph.build
    node = tg.Graph.node
    ids = tg.Graph.ids
    def setUp(self):
        tg.Graph.setUp(self)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.key = Path(self.temp.name) / 'enrich.key'; self.key.write_text(KEY + '\n')
        self.config = {'enrichment': {'url': f'http://127.0.0.1:{self.server.server_port}/enrich/companies?domain={{domain}}',
                                      'headers': {'Authorization': 'Bearer {key}'}, 'key_file': str(self.key)}}
        Service.seen.clear()

    def test_any_service_shape_becomes_one_small_profile(self):
        flat = enrichment.normalise({'name': 'Acme', 'employees_num': 7, 'size': '2-10', 'industry': 'Software', 'founded': 2014,
                                     'headquarters': 'Antwerp', 'linkedin_id': 'acme', 'about_us': 'word ' * 100})
        self.assertEqual((flat['employees'], flat['founded'], len(flat['about']) <= 300, flat['about'][-6:]), (7, 2014, True, ' word…'))
        wrapped = enrichment.normalise({'organization': {'name': 'Initech', 'industry': 'software', 'estimated_num_employees': '120',
                                                         'linkedin_url': 'http://linkedin.com/company/initech', 'founded_year': 1998}})
        self.assertEqual(wrapped, {'name': 'Initech', 'industry': 'software', 'employees': 120, 'founded': 1998, 'linkedin_url': 'http://linkedin.com/company/initech'})
        nested = enrichment.normalise({'data': {'name': 'Hooli', 'category': {'industry': 'Internet'}, 'metrics': {'employees': 900},
                                                'geo': {'city': 'Palo Alto'}, 'linkedin': {'handle': 'company/hooli'}}})
        self.assertEqual((nested['industry'], nested['employees'], nested['location'], nested['linkedin_url']),
                         ('Internet', 900, 'Palo Alto', 'https://www.linkedin.com/company/hooli'))
        self.assertEqual(enrichment.normalise({'data': {'custom_sector': 'Retail', 'name': 'Shop'}}, {'industry': ['custom_sector']})['industry'], 'Retail')
        self.assertIsNone(enrichment.normalise({'status': 'not_found'}))
        self.assertEqual(enrichment.summary({'industry': 'Software', 'size': '2-10', 'location': 'Antwerp', 'founded': 2014}),
                         'Software, 2-10 people, Antwerp, founded 2014')
        preset = enrichment.settings({'enrichment': {'provider': 'apollo', 'key_env': 'X'}})
        self.assertEqual((preset['url'], preset['headers']), ('https://api.apollo.io/api/v1/organizations/enrich?domain={domain}', {'X-Api-Key': '{key}'}))
        self.assertIsNone(enrichment.settings({'enrichment': {'url': 'https://example.com/no-domain'}}))
        self.assertIsNone(enrichment.settings({}))

    def test_fetch_sends_the_key_and_never_shows_it(self):
        cfg = enrichment.settings(self.config)
        self.assertEqual(enrichment.fetch(cfg, 'globex.com')['size'], '51-200')
        self.assertEqual(Service.seen[-1], ('/enrich/companies?domain=globex.com', 'Bearer ' + KEY))
        self.assertIsNone(enrichment.fetch(cfg, 'unknown.example'))
        self.key.write_text('wrong')
        with self.assertRaises(enrichment.EnrichmentError) as caught:
            enrichment.fetch(cfg, 'globex.com')
        self.assertEqual(str(caught.exception), 'the service rejected the key')
        self.assertNotIn('wrong', str(caught.exception))

    def test_the_graph_looks_up_its_organisations_a_few_a_day_and_shows_them(self):
        self.add(tg.mail(3, 'Lee <lee@globex.com>', 'sam@acme.com'), tg.mail(2, 'Sam <sam@acme.com>', 'Lee <lee@globex.com>', ('SENT',)),
                 tg.mail(3, 'Kim <kim@initech.example>', 'sam@acme.com'), tg.mail(3, 'Bo <bo@gmail.com>', 'sam@acme.com'))
        self.build()
        now = tg.NOW.isoformat()
        self.assertEqual(enrichment.step(self.store.connect, self.config, now), 1)
        self.assertEqual(sorted(p for p, _ in Service.seen), ['/enrich/companies?domain=globex.com', '/enrich/companies?domain=initech.example'])  # never free mail
        self.assertEqual(enrichment.step(self.store.connect, self.config, now), 0)  # both done until they're due again
        self.assertEqual(len(Service.seen), 2)
        with self.store.connect() as db:
            self.assertEqual(enrichment.status(db, self.config)['found'], 1)
            db.execute('DELETE FROM org_profiles')
        self.assertEqual(enrichment.step(self.store.connect, {'enrichment': {**self.config['enrichment'], 'per_day': 1}}, now), 1)
        self.assertEqual(enrichment.step(self.store.connect, {'enrichment': {**self.config['enrichment'], 'per_day': 1}}, now), 0)  # the daily cap
        self.build()
        org = self.node('org:globex.com')
        self.assertEqual((org['name'], org['about']), ('Globex', 'Manufacturing, 51-200 people, Springfield, founded 1989'))
        self.assertEqual(org['profile']['about'], 'Globex makes everything.')
        with self.store.connect() as db:
            lines = graph.context_lines(db, [r[0] for r in db.execute("SELECT event_id FROM graph_events WHERE node_id='email:lee@globex.com'")])
        self.assertIn('at Globex (Manufacturing, 51-200 people, Springfield, founded 1989)', lines)
        # The CLI's one-domain lookup and status, through the app.
        self.app.config = {**tg.CONFIG, **self.config}
        self.app.graph_dirty = threading.Event()
        self.assertEqual(self.app.action('enrich', {'domain': 'https://www.Globex.com/about'})['profile']['name'], 'Globex')
        self.assertEqual(self.app.action('enrich', {})['service'], '127.0.0.1')
        with self.assertRaisesRegex(ValueError, 'Give a domain'):
            self.app.action('enrich', {'domain': 'not a domain'})


if __name__ == '__main__':
    unittest.main()
