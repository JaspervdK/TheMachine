#!/usr/bin/env python3
"""Run every Python and Node suite with a disposable HOME and no account configuration."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


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
        if os.environ.get('OPENCLAW_PACKAGE_DIR'):
            env['OPENCLAW_PACKAGE_DIR'] = os.environ['OPENCLAW_PACKAGE_DIR']
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
