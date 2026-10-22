"""Require a fresh Aikido release scan before signing a maintained release.

Install the official @aikidosec/ci-api-client and configure its API key locally.
The key belongs in the client's protected config, never in command arguments.
"""
import json
import shutil
import subprocess


def require_release_scan(repository, commit, *, cwd=None):
    client = shutil.which('aikido-api-client')
    if not client:
        raise RuntimeError('Install and configure the Aikido CI client before shipping this commit.')
    print(f'Aikido release scan: {repository} at {commit[:12]} (block high and critical risks).')
    try:
        result = subprocess.run(
            [client, 'scan-release', repository, commit,
             '--minimum-severity-level', 'HIGH', '--fail-on-sast-scan',
             '--fail-on-iac-scan', '--fail-on-secrets-scan', '--json'],
            cwd=cwd, capture_output=True, text=True, timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Aikido scan could not finish; nothing may ship.') from error
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Aikido returned no valid scan receipt; nothing may ship.') from error
    if not isinstance(report, dict):
        raise RuntimeError('Aikido returned no valid scan receipt; nothing may ship.')
    if result.returncode != 0 or report.get('gate_passed') is not True:
        raise RuntimeError('Aikido release gate did not pass; review its CI scan results before shipping.')
    print('Aikido release gate passed.')
    return report
