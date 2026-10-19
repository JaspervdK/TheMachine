#!/usr/bin/env python3
"""The Machine's browser, seen from your own (optional).

A Chrome of its own on a virtual display (Xtigervnc), shown in noVNC over websockify, with a clipboard bridge so
Ctrl+C / Ctrl+V (Cmd on a Mac) work between your computer and that browser. Everything listens on 127.0.0.1 only:
the viewer on 6080, Chrome's DevTools port for OpenClaw on 9222. Both start the browser on first use, and it stops
after 15 idle minutes. Useful on a server without a screen; on a desktop the dashboard brings the right tab to the
front instead (`focus`).

    python3 browser_viewer.py install     units, viewer files, OpenClaw's `machine` browser profile
    python3 browser_viewer.py web DIR [NOVNC]  only the viewer files (noVNC plus the clipboard bridge) in DIR
    python3 browser_viewer.py focus [URL] bring the tab showing URL (or the first one) to the front

Needs: Xtigervnc, websockify, noVNC, matchbox-window-manager and Chrome or Chromium. On Debian or Ubuntu:
    sudo apt install tigervnc-standalone-server websockify novnc matchbox-window-manager
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNITS = ROOT / 'deploy/browser'
NOVNC = Path('/usr/share/novnc')
HOME = Path(os.environ.get('HOME') or Path.home())
WEB = HOME / '.local/share/machine-browser/web'
VIEWER_URL = 'http://127.0.0.1:6080/vnc.html?autoconnect=1&reconnect=1&resize=scale'
CDP_URL = 'http://127.0.0.1:9222'
NEEDS = {'Xtigervnc': 'tigervnc-standalone-server', 'websockify': 'websockify', 'matchbox-window-manager': 'matchbox-window-manager'}


def build_web(web=WEB, novnc=NOVNC):
    """noVNC as packaged, plus the clipboard bridge: its files linked, its page loading the bridge too."""
    web, novnc = Path(web), Path(novnc)
    web.mkdir(parents=True, exist_ok=True)
    page = (novnc / 'vnc.html').read_text()
    page = page.replace('</head>', '    <script type="module" src="machine-clipboard.js"></script>\n</head>', 1)
    for name, text in (('vnc.html', page), ('machine-clipboard.js', (UNITS / 'machine-clipboard.js').read_text())):
        target = web / name
        if not target.exists() or target.is_symlink() or target.read_text() != text:
            target.unlink(missing_ok=True)
            target.write_text(text)
            target.chmod(0o644)
    for item in novnc.iterdir():
        link = web / item.name
        if item.name != 'vnc.html' and not link.is_symlink() and not link.exists():
            link.symlink_to(item)


def missing():
    """What has to be installed first, as package names."""
    gone = [package for binary, package in NEEDS.items() if not shutil.which(binary)]
    if not (NOVNC / 'vnc.html').exists():
        gone.append('novnc')
    if not any(shutil.which(b) for b in ('google-chrome-stable', 'chromium', 'chromium-browser')):
        gone.append('google-chrome-stable (or chromium)')
    return gone


def install(say=print, openclaw='openclaw'):
    """Units in ~/.config/systemd/user, the viewer files, both entry sockets on, and OpenClaw's `machine` profile.
    Returns the viewer address, or None when something is missing."""
    gone = missing()
    if gone:
        say('  The browser viewer needs: ' + ', '.join(gone) + '\n  Install them (sudo apt install ...) and run this again.')
        return None
    units = HOME / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    for unit in sorted(UNITS.glob('machine-browser-*')):
        (units / unit.name).write_text(unit.read_text())
    settings = HOME / '.config/machine-browser/browser.env'
    if not settings.exists():
        chrome = next(b for b in ('google-chrome-stable', 'chromium', 'chromium-browser') if shutil.which(b))
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(f'# The browser for The Machine\'s viewer (see browser_viewer.py)\nBROWSER_BIN={chrome}\n')
    (HOME / '.local/share/machine-browser/chrome').mkdir(parents=True, exist_ok=True)
    build_web()
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=60)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'machine-browser-view.socket', 'machine-browser-cdp.socket'], check=True, timeout=60)
    # OpenClaw attaches to this browser; the units own its life.
    for args in (['browser.profiles.machine', json.dumps({'cdpUrl': CDP_URL, 'attachOnly': True}), '--strict-json'],
                 ['browser.defaultProfile', 'machine']):
        if shutil.which(openclaw) and subprocess.run([openclaw, 'config', 'set', *args], capture_output=True, timeout=60).returncode:
            say('  OpenClaw did not take the browser profile; set browser.profiles.machine.cdpUrl to ' + CDP_URL + ' yourself.')
    say('  The browser viewer is on: ' + VIEWER_URL)
    return VIEWER_URL


def focus(page=None, cdp_url=CDP_URL, timeout=5):
    """Bring a tab to the front (Target.activateTarget over the DevTools HTTP endpoint): the one showing `page`
    (same address, else same site), else the first page. Returns the tab's address, or None without a browser."""
    base = cdp_url.rstrip('/')
    if urllib.parse.urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Only a browser on this machine')
    try:
        with urllib.request.urlopen(base + '/json/list', timeout=timeout) as response:
            tabs = [t for t in json.loads(response.read()) if t.get('type') == 'page']
    except (OSError, ValueError):
        return None
    if not tabs:
        return None
    site = urllib.parse.urlsplit(page or '').hostname
    tab = (next((t for t in tabs if page and t.get('url') == page), None)
           or next((t for t in tabs if site and urllib.parse.urlsplit(t.get('url', '')).hostname == site), None) or tabs[0])
    try:
        urllib.request.urlopen(base + '/json/activate/' + urllib.parse.quote(tab['id'], safe=''), timeout=timeout).close()
    except OSError:
        return None
    return tab.get('url') or ''


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['web'] and len(argv) in (2, 3):
        build_web(*argv[1:])
        return 0
    if argv[:1] == ['install']:
        return 0 if install() else 1
    if argv[:1] == ['focus']:
        shown = focus(argv[1] if len(argv) > 1 else None)
        print(shown if shown is not None else 'No browser to bring to the front')
        return 0 if shown is not None else 1
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
