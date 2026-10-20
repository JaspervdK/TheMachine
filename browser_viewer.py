#!/usr/bin/env python3
"""The Machine's browser, seen from your own (optional).

A Chrome of its own on a virtual display (Xtigervnc), shown in noVNC over websockify, with a clipboard bridge so
Ctrl+C / Ctrl+V (Cmd on a Mac) work between your computer and that browser. Everything listens on 127.0.0.1 only:
the viewer on 6080, Chrome's DevTools port for OpenClaw on 9222. Both start the browser on first use, and it stops
after 15 idle minutes. Useful on a server without a screen; on a desktop the dashboard brings the right tab to the
front instead (`focus`).

On a desktop, the dashboard can also show its own screen live (optional, `browser_vnc` in the config): a VNC
server there on a unix socket (or 127.0.0.1), reached only through the dashboard's /browser/ under its own
sign-in, in noVNC (`browser_novnc`, the viewer files). See the README, "See the browser from anywhere".

    python3 browser_viewer.py install     units, viewer files, OpenClaw's `machine` browser profile
    python3 browser_viewer.py web DIR [NOVNC]  only the viewer files (noVNC plus the clipboard bridge) in DIR
    python3 browser_viewer.py focus [URL] bring the tab showing URL (or the first one) to the front

Needs: Xtigervnc, websockify, noVNC, matchbox-window-manager and Chrome or Chromium. On Debian or Ubuntu:
    sudo apt install tigervnc-standalone-server websockify novnc matchbox-window-manager
"""
import base64
import hashlib
import json
import mimetypes
import os
import socket
import struct
import shutil
import subprocess
import sys
import threading
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


# ------------------------------------------------- the dashboard's own view of a desktop (browser_vnc, optional)
VIEW_QUERY = 'autoconnect=1&reconnect=1&resize=scale'
WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_FRAME = 16 << 20


def view_link(base='/'):
    """The dashboard's viewer page under `base` (the dashboard's own path). The websocket path is given from the root
    for noVNC releases that read it that way; newer ones read it relative, which also stays under /browser/."""
    base = '/' + base.strip('/') + '/' if base.strip('/') else '/'
    return base + 'browser/vnc.html?' + VIEW_QUERY + '&path=' + urllib.parse.quote(base.lstrip('/') + 'browser/websockify')


def vnc_target(spec):
    """`browser_vnc` as a socket address: a unix socket path, or a port on this machine's loopback only."""
    spec = os.path.expandvars(os.path.expanduser(str(spec or '').strip()))
    if spec.startswith('unix:'):
        spec = spec[5:]
    if spec.startswith('/'):
        return socket.AF_UNIX, spec
    host, _, port = spec.rpartition(':')
    host = host.strip('[]') or '127.0.0.1'
    if host not in ('127.0.0.1', 'localhost', '::1') or not port.isdigit():
        raise ValueError('browser_vnc is a unix socket path or a 127.0.0.1 port')
    return (socket.AF_INET6 if host == '::1' else socket.AF_INET), (host, int(port))


def view_file(web, path):
    """The viewer file for /browser/<path> in `web` (noVNC, maybe with links into its package), or None."""
    parts = [p for p in urllib.parse.unquote(path).split('/') if p]
    if not parts or any(p.startswith('.') or '\\' in p for p in parts):
        return None
    found = Path(web).expanduser().joinpath(*parts)
    return found if found.is_file() else None


def file_type(path):
    kind = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return kind + ('; charset=utf-8' if kind.startswith('text/') or kind.endswith(('javascript', 'json')) else '')


def ws_accept(key):
    return base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()


def ws_frame(payload, opcode=2):
    head = bytes([0x80 | opcode])
    n = len(payload)
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack('>H', n) if n < 65536 else bytes([127]) + struct.pack('>Q', n)
    return head + payload


def ws_read(read):
    """One frame from a client (always masked): (opcode, fin, payload); None at the end."""
    head = read(2)
    if len(head) < 2:
        return None
    n = head[1] & 0x7f
    if not head[1] & 0x80:
        raise ValueError('unmasked client frame')
    if n == 126:
        n = struct.unpack('>H', read(2))[0]
    elif n == 127:
        n = struct.unpack('>Q', read(8))[0]
    if n > MAX_FRAME:
        raise ValueError('frame too large')
    mask, data = read(4), read(n)
    if len(mask) < 4 or len(data) < n:
        return None
    return head[0] & 0x0f, bool(head[0] & 0x80), bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def bridge(rfile, connection, target, headers):
    """A noVNC websocket (already accepted by the caller's checks) to the VNC server at `target`: binary frames
    both ways until either side ends."""
    family, address = vnc_target(target)
    upstream = socket.socket(family, socket.SOCK_STREAM)
    upstream.settimeout(10)
    upstream.connect(address)
    upstream.settimeout(None)
    offered = [p.strip() for p in headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
    connection.settimeout(None)
    connection.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                        f"Sec-WebSocket-Accept: {ws_accept(headers['Sec-WebSocket-Key'])}\r\n"
                        + ('Sec-WebSocket-Protocol: binary\r\n' if 'binary' in offered else '') + '\r\n').encode())
    lock = threading.Lock()

    def send(data, opcode=2):
        with lock:
            connection.sendall(ws_frame(data, opcode))

    def down():
        try:
            while data := upstream.recv(65536):
                send(data)
        except OSError:
            pass
        finally:
            try:
                send(b'', 8)
            except OSError:
                pass
            for s in (connection, upstream):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    worker = threading.Thread(target=down, daemon=True)
    worker.start()
    try:
        while frame := ws_read(rfile.read):
            opcode, _, data = frame
            if opcode == 8:
                break
            if opcode == 9:
                send(data, 10)
            elif opcode in (0, 1, 2):
                upstream.sendall(data)
    except (OSError, ValueError, struct.error):
        pass
    finally:
        try:
            upstream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        worker.join(5)
        upstream.close()


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
