#!/usr/bin/env python3
"""Run every Python and Node suite with a disposable HOME and no account configuration.

Each test file runs in its own process with its own HOME, several at once (as many as the computer has cores, at
most 8; THE_MACHINE_TEST_JOBS=1 runs them one by one). A file's output is printed when it's done, in full when it
failed. Name files to run only those: scripts/test.py test_moving.py test_fleet_dashboard.mjs"""
import concurrent.futures
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent


def openclaw_package(node):
    """OpenClaw beside this node (a global npm install), else wherever the `openclaw` on PATH comes from
    (a per-user install, as on hosted machines)."""
    beside = Path(node).parent.parent / 'lib' / 'node_modules' / 'openclaw'
    if beside.is_dir():
        return None
    found = shutil.which('openclaw')
    for folder in Path(os.path.realpath(found)).parents if found else ():
        if folder.name == 'openclaw' and (folder / 'package.json').exists():
            return str(folder)
    return None


def main():
    node = shutil.which('node')
    if not node:
        print('Node.js is required; see README prerequisites.', file=sys.stderr)
        return 1
    node = subprocess.check_output([node,'-p','process.execPath'],text=True).strip()
    cache = Path.home() / '.cache'
    cache.mkdir(exist_ok=True)
    # A HOME under /tmp would exercise the service's forbidden PrivateTmp attachment path.
    with tempfile.TemporaryDirectory(prefix='themachine-tests-',dir=cache) as home:
        env = {k:v for k,v in os.environ.items() if not k.startswith(('ASSISTANT_','OPENCLAW_','GOG_'))
               and k not in ('DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME')}
        env.update(HOME=home,PATH=str(Path(node).parent)+os.pathsep+os.environ.get('PATH',''))
        package = os.environ.get('OPENCLAW_PACKAGE_DIR') or openclaw_package(node)
        if package:
            env['OPENCLAW_PACKAGE_DIR'] = package
        browser = os.environ.get('CHROMIUM_PATH') or next(
            (found for name in ('chromium', 'chromium-browser', 'google-chrome-stable', 'google-chrome') if (found := shutil.which(name))), None)
        if browser:
            env['CHROMIUM_PATH'] = browser
        chosen = sys.argv[1:]
        files = [ROOT / name for name in chosen] if chosen else sorted(ROOT.glob('test_*.py')) + sorted(ROOT.glob('test_*.mjs'))
        jobs = max(1, int(os.environ.get('THE_MACHINE_TEST_JOBS') or min(os.cpu_count() or 2, 8)))

        def run(file):
            command = [sys.executable, '-m', 'unittest', file.stem] if file.suffix == '.py' else [node, str(file)]
            started = time.monotonic()
            own = Path(home) / file.name
            own.mkdir()
            try:
                result = subprocess.run(command, cwd=ROOT, env={**env, 'HOME': str(own)}, timeout=600,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
                ok, output = result.returncode == 0, result.stdout
            except subprocess.TimeoutExpired as timeout:
                out = timeout.output or ''
                ok, output = False, (out.decode(errors='replace') if isinstance(out, bytes) else out) + '\nTimed out after 600 s'
            return file.name, ok, output, time.monotonic() - started

        failed, took = [], []
        print(f'Running {len(files)} test files, {jobs} at a time', flush=True)
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            # the biggest files first, so a slow one doesn't start last and keep everyone waiting
            started = [pool.submit(run, file) for file in sorted(files, key=lambda f: -f.stat().st_size)]
            for done in concurrent.futures.as_completed(started):
                name, ok, output, seconds = done.result()
                took.append((seconds, name))
                print(f'{"ok  " if ok else "FAIL"} {name} ({seconds:.0f} s)', flush=True)
                if not ok:
                    failed.append(name)
                    print(output.rstrip() + '\n', flush=True)
        print('Slowest: ' + ', '.join(f'{name} {seconds:.0f} s' for seconds, name in sorted(took, reverse=True)[:5]))
        print('\n'+('Failed: '+', '.join(sorted(failed)) if failed else 'All Python and Node suites passed.'))
        return 1 if failed else 0

if __name__=='__main__':
    sys.exit(main())
