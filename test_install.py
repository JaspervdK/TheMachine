import json, struct, unittest
import assistant as a


def png_size(path):
    with open(path, 'rb') as f:
        head = f.read(24)
    return struct.unpack('>II', head[16:24])


class HomeScreenApp(unittest.TestCase):
    """Android installs the dashboard as a full-screen app from its manifest."""

    def test_manifest_is_installable_and_every_icon_is_served(self):
        manifest = json.loads((a.ROOT / 'manifest.webmanifest').read_text())
        self.assertEqual(manifest['display'], 'fullscreen')
        self.assertEqual((manifest['start_url'], manifest['scope']), ('.', '.'))  # relative: works under /assistant/
        purposes = set()
        for icon in manifest['icons']:
            path = a.BRAND_ASSETS['/' + icon['src']]
            self.assertEqual('%dx%d' % png_size(path), icon['sizes'], icon['src'])
            purposes.add((icon['sizes'], icon['purpose']))
        self.assertTrue({('192x192', 'any'), ('512x512', 'any'), ('512x512', 'maskable')} <= purposes)

    def test_own_origin_for_the_app_and_old_links_keep_working(self):
        config = {'dashboard_host': 'm.ts.net:8443', 'dashboard_aliases': ['m.ts.net']}
        self.assertEqual(a.dashboard_hosts(config), ['m.ts.net:8443', 'm.ts.net'])
        self.assertEqual(a.dashboard_hosts({'dashboard_host': 'm.ts.net'}), ['m.ts.net'])

    def test_dashboard_links_the_manifest(self):
        self.assertIn('<link rel="manifest" href="manifest.webmanifest" crossorigin="use-credentials">', (a.ROOT / 'dashboard.html').read_text())

    def test_something_wrong_goes_to_github_issues_or_the_hosted_support_form(self):
        page = (a.ROOT / 'dashboard.html').read_text()
        self.assertIn('id="report-problem" class="footer-link" href="https://github.com/JeroenCorthout/TheMachine/issues/new"', page)
        self.assertIn("$('#report-problem').href=new URL('/support',state.manage_url).href", page)


if __name__ == '__main__':
    unittest.main()
