"""Small operating-system differences in one place: file locks, process groups and the owner's name.

Linux and macOS use flock and process groups. Windows (where only the tests run natively; The Machine itself
runs inside WSL2 there) gets the closest equivalent, so the same code imports and behaves the same."""
import contextlib
import os
import signal
import subprocess
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def lock(handle, blocking=True):
    """An exclusive lock on an open file, like fcntl.flock(LOCK_EX). Without blocking it raises
    BlockingIOError when another process holds it, as flock does."""
    if fcntl:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not blocking:
                raise BlockingIOError('locked by another process') from None
            time.sleep(0.05)


def unlock(handle):
    if fcntl:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    handle.seek(0)
    with contextlib.suppress(OSError):
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def new_group():
    """Popen arguments that start a child in its own process group, so it and its children can be ended together."""
    return {'start_new_session': True} if os.name == 'posix' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}


def end_group(proc, wait=3):
    """End a child started with new_group() and everything it started: TERM first, then KILL."""
    if os.name != 'posix':
        with contextlib.suppress(OSError):
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=30)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(wait)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(wait)
            return


def tailscale():
    """The tailscale CLI: on PATH, or inside the Tailscale app on a Mac (which does not put it on PATH)."""
    import shutil
    import sys
    found = shutil.which('tailscale')
    app = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
    return found or (app if sys.platform == 'darwin' and os.path.exists(app) else None)


def owner_name():
    """The account's full name from the system, or ''."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0].strip()
    except (ImportError, KeyError, AttributeError):
        return ''


def home_of_user():
    """This account's home folder from the system, not from $HOME."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError):
        return os.path.expanduser('~')


def shred(path):
    """Overwrite a file with zeros, then remove it. Best effort: a copy-on-write file system or an SSD can keep the
    old blocks, so this is for files that should never stay readable after they are gone, not a guarantee."""
    try:
        size = os.lstat(path).st_size
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, 'r+b', buffering=0) as handle:
                block, done = b'\0' * (1 << 20), 0
                while done < size:
                    done += handle.write(block[:min(len(block), size - done)])
                os.fsync(handle.fileno())
    except FileNotFoundError:
        return
    except OSError:
        pass
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def shred_tree(folder):
    """shred() every file in a folder, then remove the folder."""
    import shutil
    if not os.path.lexists(folder):
        return
    if os.path.islink(folder) or not os.path.isdir(folder):
        return shred(folder)
    for root, _, files in os.walk(folder, followlinks=False):
        for name in files:
            shred(os.path.join(root, name))
    shutil.rmtree(folder, ignore_errors=True)


def private_dir(parent, prefix):
    """A new folder only this account can open (700), inside `parent`: on the same file system as what it is for."""
    import tempfile
    os.makedirs(parent, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=parent)
