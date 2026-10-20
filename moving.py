"""Moving The Machine: everything it knows and how it is set up, in one archive, and back again on another computer.

An export holds the databases (signals, suggestions, people and organisations with your own edits, promises),
config.json, connectors you built yourself, your OpenClaw workspace files (AGENTS.md, USER.md, memory and the like)
and who may talk to your Telegram bot. Keys and tokens only come along when you pick a passphrase: then the whole
archive is encrypted with it (AES-256, the passphrase stretched with PBKDF2), and nothing in it can be read without
that passphrase. Without one, keys stay behind and the import lists what to connect again; that archive is a plain
.zip, which Windows, macOS and Linux open without extra tools. The encrypted one is a .tar.gz.enc: only openssl (and
so `assistant import`) opens it, because a zip that Windows opens on its own can only use weak encryption.
"""
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path

FORMAT = 1
APP = Path(__file__).resolve().parent
PASSPHRASE_ENV = 'THE_MACHINE_EXPORT_PASSPHRASE'
MIN_PASSPHRASE = 10
# openssl is on every Linux and macOS; the passphrase reaches it through its environment, never its arguments
OPENSSL = ['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '600000', '-md', 'sha256']
# Workspace parts that are not yours to move: the app itself, and git's own bookkeeping
WORKSPACE_SKIP = {'tools', '.git', 'node_modules'}
SKIP_SUFFIXES = ('.bak', '.lock', '.tmp', '-wal', '-shm', '-journal')
# Files in the data folder that are keys even when config.json does not point at them
SECRET_NAMES = {'owner-token', 'api-token', 'vapid.json', 'gog-password', 'browser-logins.enc', 'browser-logins.key'}
# Tokens that only work for a hosted server; they are never exported
HOSTED_ONLY = {'google-broker'}
# Settings a hosted server added that mean nothing elsewhere
HOSTED_KEYS = ('google_broker', 'manage_url', 'needs_you_file')


class MoveError(Exception):
    pass


def _workspace(path=None):
    return Path(path or os.environ.get('OPENCLAW_WORKSPACE') or Path.home() / '.openclaw/workspace').expanduser()


def _openclaw_home(path=None):
    return Path(path or os.environ.get('OPENCLAW_HOME') or Path.home() / '.openclaw').expanduser()


def _files(root, skip_top=()):
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] in skip_top or path.is_symlink() or not path.is_file():
            continue
        if path.name.endswith(SKIP_SUFFIXES):
            continue
        yield path, relative


def plan(config_path, workspace=None, openclaw_home=None):
    """What an export holds: [(kind, source path, name in the archive)]. kind is db, file or secret."""
    config_path = Path(config_path).expanduser()
    config = json.loads(config_path.read_text())
    data = config_path.parent
    keys = {Path(p).expanduser().resolve() for name, p in (config.get('credentials') or {}).items() if name not in HOSTED_ONLY}
    hosted = {Path(p).expanduser().resolve() for name, p in (config.get('credentials') or {}).items() if name in HOSTED_ONLY}
    items = [('file', config_path, 'config/config.json')]
    for path, relative in _files(data):
        resolved = path.resolve()
        if path == config_path or resolved in hosted or path.name == 'machine-token':
            continue
        if path.suffix in ('.sqlite', '.db'):
            items.append(('db', path, f'data/{relative}'))
        elif resolved in keys or path.name in SECRET_NAMES or path.suffix == '.env':
            items.append(('secret', path, f'secrets/data/{relative}'))
        else:
            items.append(('file', path, f'data/{relative}'))
    for path in keys - {p.resolve() for _, p, _ in items}:  # keys kept outside the data folder
        if path.is_file():
            items.append(('secret', path, f'secrets/elsewhere/{path.name}'))
    space = _workspace(workspace)
    if space.is_dir():
        for path, relative in _files(space, WORKSPACE_SKIP):
            if path.resolve() in hosted or path.name == 'machine-token':
                continue
            secret = path.resolve() in keys or path.name in SECRET_NAMES or path.suffix == '.env'
            items.append(('secret' if secret else 'file', path,
                          f'{"secrets/" if secret else ""}workspace/{relative}'))
    items += [('file', path, f'connectors/{path.relative_to(APP)}') for path in own_connectors()]
    home = _openclaw_home(openclaw_home)
    if (home / 'openclaw.json').is_file():
        items.append(('secret', home / 'openclaw.json', 'secrets/openclaw/openclaw.json'))
    if (home / 'credentials').is_dir():
        items += [('secret', path, f'secrets/openclaw/credentials/{relative}') for path, relative in _files(home / 'credentials')]
    # A configured credential can be reached through more than one root. Prefer the workspace's
    # restorable location, but never downgrade a known secret to an ordinary file.
    unique = {}
    for kind, path, name in items:
        previous = unique.get(path.resolve())
        if previous is None or kind == 'secret' or name.startswith(('workspace/', 'secrets/workspace/')):
            unique[path.resolve()] = (kind, path, name)
    return list(unique.values())


def own_connectors():
    """Connectors built here that the app's own repository doesn't have."""
    try:
        listed = subprocess.run(['git', '-C', str(APP), 'ls-files', '--others', '--exclude-standard', '--', 'connectors',
                                 'test_connector_*.py'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [APP / line for line in listed.stdout.splitlines()
            if line.endswith('.py') and '__pycache__' not in line and (APP / line).is_file()]


def telegram(openclaw_home=None):
    """Who may talk to the bot, without the bot's token: enough to recognise you again after a move."""
    try:
        config = json.loads((_openclaw_home(openclaw_home) / 'openclaw.json').read_text())
    except (OSError, ValueError):
        return None
    allowed = (config.get('commands') or {}).get('ownerAllowFrom') or []
    channel = (config.get('channels') or {}).get('telegram') or {}
    return {'enabled': bool(channel.get('enabled')), 'owner_allow_from': allowed} if channel or allowed else None


def reconnect(config, secrets):
    """What has to be connected again after an import, in plain words."""
    out = []
    for account in config.get('accounts') or []:
        service = account.get('service', 'gmail')
        if account.get('transport') == 'broker':
            out.append(f'{account["id"]}: sign in to Google again (hosted Google access does not move along)')
        elif not secrets and account.get('credential'):
            out.append(f'{account["id"]}: its {service} key or token')
    out.append('your model: sign in to ChatGPT again, or paste your API key')
    if not secrets:
        out.append('Telegram: your bot token (the people allowed to talk to it are kept)')
    return out


def estimate(config_path, secrets=False, workspace=None, openclaw_home=None):
    items = [i for i in plan(config_path, workspace, openclaw_home) if secrets or i[0] != 'secret']
    return {'files': len(items), 'bytes': sum(path.stat().st_size for _, path, _ in items)}


def _add_bytes(archive, name, data):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o600, int(dt.datetime.now().timestamp())
    archive.addfile(info, io.BytesIO(data))


def _pack(target, entries, zipped):
    """Write (name in the archive, bytes or a file path) entries as a .zip or a .tar.gz."""
    if zipped:
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name, source in entries:
                archive.writestr(name, source) if isinstance(source, bytes) else archive.write(source, name)
        return
    with tarfile.open(target, 'w:gz') as archive:
        for name, source in entries:
            _add_bytes(archive, name, source) if isinstance(source, bytes) else archive.add(source, name)


def export(config_path, out_dir, passphrase=None, workspace=None, openclaw_home=None, source='local', now=None):
    """Write the archive into out_dir and return (path, manifest). With a passphrase the archive includes keys and is
    encrypted as a whole (name ends in .tar.gz.enc); without one, keys stay out and it is a plain .zip."""
    if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
        raise MoveError(f'Pick a passphrase of at least {MIN_PASSPHRASE} characters')
    config_path = Path(config_path).expanduser()
    config = json.loads(config_path.read_text())
    secrets = passphrase is not None
    items = [i for i in plan(config_path, workspace, openclaw_home) if secrets or i[0] != 'secret']
    now = now or dt.datetime.now(dt.timezone.utc)
    manifest = {'format': FORMAT, 'created_at': now.isoformat(timespec='seconds'), 'source': source,
                'data_dir': str(config_path.parent), 'workspace_dir': str(_workspace(workspace)), 'includes_keys': secrets,
                'files': sorted(name for _, _, name in items), 'telegram': telegram(openclaw_home),
                'reconnect': reconnect(config, secrets)}
    out_dir = Path(out_dir)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f'the-machine-export-{now:%Y-%m-%d}'
    plain = out_dir / (f'{stem}.tar.gz' if secrets else f'{stem}.zip')
    with tempfile.TemporaryDirectory() as temp:
        def entries():
            yield 'manifest.json', json.dumps(manifest, indent=2).encode()
            yield 'README.txt', readme(manifest).encode()
            for kind, path, name in items:
                if kind == 'db':
                    copy = Path(temp) / 'copy.sqlite'
                    copy.unlink(missing_ok=True)
                    with closing(sqlite3.connect(path, timeout=30)) as source_db, closing(sqlite3.connect(copy)) as target:
                        source_db.backup(target)  # a consistent copy while the service keeps writing
                    yield name, copy
                else:
                    yield name, path
        _pack(plain, entries(), zipped=not secrets)
        os.chmod(plain, 0o600)
        if not secrets:
            return plain, manifest
        sealed = out_dir / f'{stem}.tar.gz.enc'
        try:
            _openssl(['-salt', '-in', str(plain), '-out', str(sealed)], passphrase)
        finally:
            plain.unlink(missing_ok=True)
        os.chmod(sealed, 0o600)
        return sealed, manifest


def _openssl(args, passphrase, decrypt=False):
    env = {**os.environ, PASSPHRASE_ENV: passphrase}
    try:
        result = subprocess.run(OPENSSL + (['-d'] if decrypt else []) + args + ['-pass', f'env:{PASSPHRASE_ENV}'],
                                env=env, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise MoveError('openssl is needed for an export with keys; install it and try again') from None
    if result.returncode:
        raise MoveError('Wrong passphrase, or the file is damaged' if decrypt else 'Could not encrypt the export')


def readme(manifest):
    lines = ['The Machine: your export', '', f'Made {manifest["created_at"]}.',
             'This holds what The Machine knows and how it was set up. It is yours: keep it somewhere safe.', '',
             'To run it on your own computer:',
             '  1. Install The Machine: https://github.com/JeroenCorthout/TheMachine/blob/main/docs/getting-started.md',
             '     Stop its service before importing: systemctl --user stop personal-assistant',
             '  2. Put it back:  assistant import <this file>',
             '  3. Connect what the import lists, then check with:  assistant doctor', '']
    lines.append('Keys and tokens are included.' if manifest['includes_keys'] else
                 'Keys and tokens are not included; the import lists what to connect again.')
    lines += ['', 'Connect again after the import:'] + [f'  - {line}' for line in manifest['reconnect']]
    return '\n'.join(lines) + '\n'


def _outside(name, zipped=False):
    path = Path(name)
    # a zip may come from Windows: a backslash or a drive letter there points outside as well
    return path.is_absolute() or '..' in path.parts or (zipped and ('\\' in name or name[1:2] == ':'))


def _safe_extract(archive, target):
    if isinstance(archive, zipfile.ZipFile):
        for member in archive.infolist():
            kind = (member.external_attr >> 16) & 0o170000
            if _outside(member.filename, zipped=True) or kind not in (0, 0o100000, 0o040000):  # files and folders only, no links
                raise MoveError('This archive has entries outside its own folder; not importing it')
        archive.extractall(target)
        return
    for member in archive.getmembers():
        if _outside(member.name) or not (member.isfile() or member.isdir()):
            raise MoveError('This archive has entries outside its own folder; not importing it')
    archive.extractall(target, filter='data') if hasattr(tarfile, 'data_filter') else archive.extractall(target)


def _rebase(value, old, new):
    if isinstance(value, dict):
        return {k: _rebase(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, old, new) for v in value]
    if isinstance(value, str) and old and (value == old or value.startswith(old.rstrip('/') + '/')):
        return new + value[len(old.rstrip('/')):]
    return value


def _place(source, target, notes, mode=0o600):
    """Copy one file into place. A different file already there is kept, and the imported one sits next to it."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != source.read_bytes():
        target = target.with_name(target.name + '.from-export')
        notes.append(f'kept your {target.name[:-len(".from-export")]}; the exported one is {target}')
    shutil.copyfile(source, target)
    os.chmod(target, mode)


@contextmanager
def offline_target(config_path):
    """Share the service's lock before touching any imported files. Never overlay an existing WAL."""
    import fcntl
    data = Path(config_path).expanduser().parent
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (data / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MoveError('Stop personal-assistant first: systemctl --user stop personal-assistant. Then import again.') from None
        try:
            if any(data.rglob('*-wal')) or any(data.rglob('*-shm')):
                raise MoveError('The target database is still open (SQLite WAL files exist). Close its readers and stop '
                                'personal-assistant before importing; do not delete its WAL files.')
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def import_(archive_path, config_path, passphrase=None, workspace=None, openclaw_home=None, force=False, now=None):
    with offline_target(config_path):
        return _import(archive_path, config_path, passphrase, workspace, openclaw_home, force, now)


def _import(archive_path, config_path, passphrase=None, workspace=None, openclaw_home=None, force=False, now=None):
    """Put an export back on this computer. Returns {'restored': [...], 'reconnect': [...], 'notes': [...]}."""
    archive_path, config_path = Path(archive_path).expanduser(), Path(config_path).expanduser()
    now = now or dt.datetime.now(dt.timezone.utc)
    if config_path.exists() and not force:
        try:
            existing = json.loads(config_path.read_text())
        except ValueError:
            existing = {}
        if existing.get('accounts'):
            raise MoveError(f'{config_path} already has connected sources; import into a fresh install, '
                            'or pass --force to replace it (the current data folder is kept as a copy first)')
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        plain = archive_path
        if archive_path.name.endswith('.enc'):
            if not passphrase:
                raise MoveError('This export is encrypted; give its passphrase')
            plain = temp / 'export.tar.gz'
            _openssl(['-in', str(archive_path), '-out', str(plain)], passphrase, decrypt=True)
        unpacked = temp / 'export'
        try:
            with (zipfile.ZipFile(plain) if zipfile.is_zipfile(plain) else tarfile.open(plain, 'r:gz')) as archive:
                _safe_extract(archive, unpacked)
        except (tarfile.TarError, zipfile.BadZipFile, OSError):
            raise MoveError('This is not an export of The Machine, or it is damaged') from None
        try:
            manifest = json.loads((unpacked / 'manifest.json').read_text())
        except (OSError, ValueError):
            raise MoveError('This archive has no manifest; it is not an export of The Machine') from None
        if manifest.get('format', 0) > FORMAT:
            raise MoveError('This export is newer than this install; update The Machine first')
        # Validate every database before replacing any live file.
        for root in (unpacked / 'data', unpacked / 'secrets/data'):
            for path in root.rglob('*') if root.is_dir() else []:
                if path.suffix in ('.sqlite', '.db') and path.is_file():
                    try:
                        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as check:
                            valid = check.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    except sqlite3.Error:
                        valid = False
                    if not valid:
                        raise MoveError('An exported database failed its integrity check; nothing was imported.')
        data = config_path.parent
        if force and data.exists() and any(data.iterdir()):
            keep = data.with_name(f'{data.name}.before-import-{now:%Y%m%d-%H%M%S}')
            shutil.copytree(data, keep)
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        restored, notes = [], []
        for folder, mode in (('data', 0o600), ('secrets/data', 0o600)):
            root = unpacked / folder
            for path in sorted(root.rglob('*')) if root.is_dir() else []:
                if path.is_file():
                    target = data / path.relative_to(root)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + '.import-new')
                    shutil.copyfile(path, temporary)
                    os.chmod(temporary, mode)
                    temporary.replace(target)
                    restored.append(str(target))
        config = _rebase(json.loads((unpacked / 'config/config.json').read_text()), manifest.get('data_dir'), str(data))
        config = _rebase(config, manifest.get('workspace_dir'), str(_workspace(workspace)))
        for key in HOSTED_KEYS:
            config.pop(key, None)
        for name in HOSTED_ONLY:
            (config.get('credentials') or {}).pop(name, None)
        if (config.get('enrichment') or {}).get('by') == 'hosting':
            config['enrichment'] = {'enabled': False}  # hosting did the look-ups; set up your own service to turn it on
            notes.append('organisation look-ups were done by the hosting; the profiles found so far are kept')
        for account in config.get('accounts') or []:
            if account.get('transport') == 'broker':
                account['transport'] = 'gog'  # Google from here on goes through your own sign-in
        for name, path in list((config.get('credentials') or {}).items()):
            if not Path(path).expanduser().exists() and (unpacked / 'secrets/elsewhere' / Path(path).name).is_file():
                target = data / Path(path).name
                _place(unpacked / 'secrets/elsewhere' / Path(path).name, target, notes)
                config['credentials'][name] = str(target)
        if config_path.exists():
            shutil.copyfile(config_path, config_path.with_name(config_path.name + '.bak'))
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(config, indent=2) + '\n')
        restored.append(str(config_path))
        space = _workspace(workspace)
        for folder in ('workspace', 'secrets/workspace'):
            for path in sorted((unpacked / folder).rglob('*')) if (unpacked / folder).is_dir() else []:
                if path.is_file():
                    target = space / path.relative_to(unpacked / folder)
                    _place(path, target, notes)
                    restored.append(str(target))
        for path in sorted((unpacked / 'connectors').rglob('*')) if (unpacked / 'connectors').is_dir() else []:
            if path.is_file():
                target = APP / path.relative_to(unpacked / 'connectors')
                _place(path, target, notes, mode=0o644)
                restored.append(str(target))
        home = _openclaw_home(openclaw_home)
        for path in sorted((unpacked / 'secrets/openclaw').rglob('*')) if (unpacked / 'secrets/openclaw').is_dir() else []:
            if path.is_file():
                target = home / path.relative_to(unpacked / 'secrets/openclaw')
                _place(path, target, notes)
                restored.append(str(target))
        return {'restored': restored, 'reconnect': manifest.get('reconnect') or [], 'notes': notes,
                'telegram': manifest.get('telegram')}
