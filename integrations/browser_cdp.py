"""Direct DevTools (CDP) access to a browser the readers share, for when one is set (e.g. http://127.0.0.1:9222).

The OpenClaw browser commands attach Playwright to every tab, frame and worker of the browser and give up after
about 9 seconds, so on a small, busy server a browser with a few heavy pages open fails every read. This talks to
the one tab a reader opens, with the Python standard library only: open, evaluate, navigate, list and close.
"""
import base64, json, os, socket, struct, urllib.parse, urllib.request


class CdpError(Exception):
    pass


def _http(base, path, method='GET', timeout=60):
    # The first request may start the browser (socket activation), so it gets time.
    request = urllib.request.Request(base.rstrip('/') + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except OSError as e:
        raise CdpError('browser not reachable at ' + base + ': ' + str(e)) from None
    try:
        return json.loads(body)
    except ValueError:
        return body


def tab_ids(base):
    return {t['id'] for t in _http(base, '/json/list') if t.get('type') == 'page'}


def open_tab(base, url):
    tab = _http(base, '/json/new?' + urllib.parse.quote(url, safe=':/?&=%'), method='PUT')
    if not isinstance(tab, dict) or not tab.get('id'):
        raise CdpError('the browser did not open a tab: ' + str(tab)[:200])
    return tab['id']


def close_tab(base, target):
    try:
        _http(base, '/json/close/' + urllib.parse.quote(target, safe=''), timeout=30)
    except CdpError as e:
        if 'HTTP Error 404' not in str(e):  # 404: no such tab (any more), which is what closing wants
            raise


class _Socket:
    """A minimal client WebSocket (RFC 6455): text frames out, text frames (possibly fragmented) in."""

    def __init__(self, base, target, timeout):
        parsed = urllib.parse.urlparse(base)
        host, port = parsed.hostname or '127.0.0.1', parsed.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET /devtools/page/' + urllib.parse.quote(target, safe='') + ' HTTP/1.1\r\nHost: '
                           + host + ':' + str(port) + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError('the browser closed the connection to tab ' + target)
            head += chunk
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise CdpError('the browser refused a connection to tab ' + target + ': '
                           + head.split(b'\r\n', 1)[0].decode(errors='replace'))
        self.rest = head.split(b'\r\n\r\n', 1)[1]

    def _read(self, n):
        while len(self.rest) < n:
            chunk = self.sock.recv(max(65536, n - len(self.rest)))
            if not chunk:
                raise CdpError('the browser closed the connection')
            self.rest += chunk
        data, self.rest = self.rest[:n], self.rest[n:]
        return data

    def send(self, text):
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        head = struct.pack('!B', 0x81) + (struct.pack('!B', 0x80 | n) if n < 126 else
                                          struct.pack('!BH', 0x80 | 126, n) if n < 65536 else
                                          struct.pack('!BQ', 0x80 | 127, n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def receive(self):
        message = b''
        while True:
            first, second = self._read(2)
            n = second & 0x7f
            if n == 126:
                n = struct.unpack('!H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read(8))[0]
            payload = self._read(n)
            opcode = first & 0x0f
            if opcode == 8:
                raise CdpError('the browser closed the connection')
            if opcode in (0, 1):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _call(base, target, method, params, timeout):
    ws = _Socket(base, target, timeout)
    try:
        ws.send(json.dumps({'id': 1, 'method': method, 'params': params}))
        while True:
            reply = json.loads(ws.receive())
            if reply.get('id') == 1:
                if 'error' in reply:
                    raise CdpError(method + ' failed: ' + str(reply['error'].get('message'))[:200])
                return reply.get('result') or {}
    except socket.timeout:
        raise CdpError('the browser did not answer in time') from None
    finally:
        ws.close()


def evaluate(base, target, fn, timeout_ms=90000):
    """Run a JavaScript function (source text, possibly async) in the tab and return its JSON value."""
    result = _call(base, target, 'Runtime.evaluate',
                   {'expression': '(' + fn + ')()', 'awaitPromise': True, 'returnByValue': True,
                    'timeout': timeout_ms}, timeout_ms / 1000 + 30)
    if result.get('exceptionDetails'):
        details = result['exceptionDetails']
        raise CdpError('script failed: ' + str((details.get('exception') or {}).get('description') or details.get('text'))[:200])
    return (result.get('result') or {}).get('value')


def navigate(base, target, url, timeout=60):
    _call(base, target, 'Page.navigate', {'url': url}, timeout)
