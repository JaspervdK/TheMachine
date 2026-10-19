#!/usr/bin/env python3
"""Run every Python and Node suite with a disposable HOME and no account configuration."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

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
        commands = [[sys.executable,'-m','unittest','discover','-p','test_*.py']]
        commands += [[node,str(file)] for file in sorted(ROOT.glob('test_*.mjs'))]
        failed = []
        for command in commands:
            print('\nRunning: '+ ' '.join(command),flush=True)
            try:
                result = subprocess.run(command,cwd=ROOT,env=env,timeout=600)
                if result.returncode:
                    failed.append(command[-1])
            except subprocess.TimeoutExpired:
                failed.append(command[-1])
        print('\n'+('Failed: '+', '.join(failed) if failed else 'All Python and Node suites passed.'))
        return 1 if failed else 0


if __name__=='__main__':
    sys.exit(main())
