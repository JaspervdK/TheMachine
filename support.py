"""`assistant support-bundle`: one archive with what is needed to see why an install misbehaves, without secrets.

It holds versions, `assistant doctor`, the service status and recent log, the OpenClaw Gateway's status and recent
log, and config.json with every key, token, password and address taken out. Nothing is sent anywhere: the owner
looks at the file and passes it on."""
import datetime as dt
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import service

ROOT = Path(__file__).resolve().parent
SENSITIVE_KEY = re.compile(r'token|secret|password|passphrase|key|credential|cookie|auth|session|owner|email|phone|host', re.I)
PATTERNS = [
    (re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}'), r'\1 <redacted>'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*'), '<jwt>'),
    (re.compile(r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b'), '<bot-token>'),
    (re.compile(r'\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}'), '<api-key>'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{20,})'), '<token>'),
    (re.compile(r'(?i)([?&](?:key|token|code|secret|sig|signature|access_token|k)=)[^&\s"\']+'), r'\1<redacted>'),
    (re.compile(r'(?i)(#k=)[^\s"\']+'), r'\1<redacted>'),
    (re.compile(r'\b[0-9a-f]{32,}\b', re.I), '<hex>'),
    (re.compile(r'(?<![/\w.-])[A-Za-z0-9+_-]{40,}={0,2}'), '<secret>'),
    (re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+'), '<email>'),
    (re.compile(r'(?i)((?:token|secret|password|passphrase|api[_-]?key)\s*[=:]\s*)\S+'), r'\1<redacted>'),
]


def scrub(text):
    """Text with anything that looks like a secret or an address taken out."""
    text = str(text or '')
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_config(value, key=''):
    """config.json with sensitive values replaced; structure and harmless settings kept."""
    if isinstance(value, dict):
        return {k: scrub_config(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_config(v, key) for v in value]
    if isinstance(value, str) and (SENSITIVE_KEY.search(key) or '@' in value):
        return '<redacted>' if value else ''
    if isinstance(value, (int, float)) and SENSITIVE_KEY.search(key) and not isinstance(value, bool):
        return '<redacted>'
    return scrub(value) if isinstance(value, str) else value


def _run(args, timeout=60):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors='replace')
        return (result.stdout + ('\n' + result.stderr if result.stderr.strip() else '')).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'({" ".join(map(str, args[:3]))}: {error})'


def versions(config):
    lines = [f'when: {dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}',
             f'os: {platform.platform()}', f'wsl: {service.wsl()}', f'service manager: {service.manager()}',
             f'python: {sys.version.split()[0]} ({sys.executable})', f'app: {ROOT}',
             'commit: ' + _run(['git', '-C', str(ROOT), 'log', '-1', '--format=%h %cd', '--date=short'])]
    for name in ('node', 'openclaw', 'gog', 'tailscale', 'wacli'):
        binary = (config or {}).get(name) or shutil.which(name)
        lines.append(f'{name}: ' + (_run([binary, '--version'], 30).splitlines() or ['?'])[0] if binary else f'{name}: not found')
    return '\n'.join(lines)


def windows_desktop():
    """Inside WSL: the Windows Desktop folder, so the bundle is easy to find and attach."""
    if not service.wsl() or not shutil.which('cmd.exe') or not shutil.which('wslpath'):
        return None
    profile = _run(['cmd.exe', '/c', 'echo %USERPROFILE%'], 20).splitlines()
    if not profile:
        return None
    folder = Path(_run(['wslpath', '-u', profile[-1].strip()], 20).strip()) / 'Desktop'
    return folder if folder.is_dir() else None


def bundle(config_path, out=None, doctor=None, lines=1000):
    """Write the archive and return its path."""
    try:
        config = json.loads(Path(config_path).expanduser().read_text())
    except (OSError, ValueError):
        config = {}
    parts = {'versions.txt': versions(config),
             'service-status.txt': service.status_text() if service.available() else f'no service manager ({service.manager()})',
             'service-log.txt': service.logs(lines=lines) if service.available() else ''}
    if doctor:
        try:
            parts['doctor.txt'] = '\n'.join(f'{mark} {text}' + (f'\n    fix: {fix}' if fix else '') for mark, text, fix in doctor(config_path))
        except Exception as error:  # the bundle is most needed when things are broken
            parts['doctor.txt'] = f'doctor failed: {type(error).__name__}: {error}'
    openclaw = config.get('openclaw') or shutil.which('openclaw')
    if openclaw:
        parts['openclaw-gateway.txt'] = _run([openclaw, 'gateway', 'status'], 60)
        # Warnings and errors only, shortened: info lines can carry what the agent said.
        parts['openclaw-log.txt'] = '\n'.join(line[:240] for line in _run([openclaw, 'logs', '--limit', '1500', '--plain'], 60).splitlines()
                                              if re.search(r'\s(warn|error|fatal)\s', line) and '[agent:' not in line)
    parts['config.json'] = json.dumps(scrub_config(config), indent=2)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    folder = Path(out).expanduser() if out else (windows_desktop() or Path.cwd())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'the-machine-logs-{stamp}.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        for name, text in parts.items():
            data = scrub(text).encode() if name != 'config.json' else text.encode()
            info = tarfile.TarInfo(f'the-machine-logs-{stamp}/{name}')
            info.size, info.mtime, info.mode = len(data), int(dt.datetime.now().timestamp()), 0o600
            archive.addfile(info, io.BytesIO(data))
    os.chmod(path, 0o600)
    return path
