#!/usr/bin/env python3
"""Signed releases of The Machine.

A release is a git tag `release-YYYYMMDD-HHMM` on a commit of main, signed with the release key (an SSH key). Hosted
Machines and `update` below only install a release whose signature checks out against the public key in the code
that is already installed (deploy/release-signers), so a pushed commit alone never reaches a Machine.

  python3 scripts/release.py cut       sign origin/main as a new release and push the tag (needs the private key)
  python3 scripts/release.py verify    check a release tag (default: the newest) against deploy/release-signers
  python3 scripts/release.py update    self-hosted: move this checkout to the newest signed release, then ./install.sh

The private key is read from $THE_MACHINE_RELEASE_KEY, else ~/.config/the-machine-release/signing-key."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.security_gate import require_release_scan
except ModuleNotFoundError:
    from security_gate import require_release_scan

ROOT = Path(__file__).resolve().parent.parent
SIGNERS = ROOT / 'deploy' / 'release-signers'
TAG = re.compile(r'release-\d{8}-\d{4}')
PRINCIPAL = 'release@themachine.computer'
BEGIN = b'-----BEGIN SSH SIGNATURE-----\n'
END = b'-----END SSH SIGNATURE-----\n'


class Refused(Exception):
    pass


def git(*args, check=True, raw=False):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=not raw)
    if check and result.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {(result.stderr or b"").strip() if raw else result.stderr.strip()}')
    return result


def signed_commit(raw: bytes, tag: str, signers: str) -> str:
    """The commit a raw tag object points to, if it is the release `tag` signed by one of `signers` (allowed-signers
    lines). Raises Refused otherwise. The same check runs on hosted Machines and on the control plane."""
    if not TAG.fullmatch(tag):
        raise Refused(f'{tag} is not a release tag')
    start = raw.rfind(b'\n' + BEGIN)
    if start < 0 or not raw.endswith(END):
        raise Refused(f'{tag} is not signed')
    payload, signature = raw[:start + 1], raw[start + 1:]
    head = payload.split(b'\n\n', 1)[0].decode('utf-8', 'replace').split('\n')
    found = re.fullmatch(r'object ([0-9a-f]{40})', head[0]) if head else None
    if not found or head[1:3] != ['type commit', f'tag {tag}']:
        raise Refused(f'{tag} does not name itself or a commit')
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'signers').write_text(signers)
        (Path(tmp) / 'sig').write_bytes(signature)
        result = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(Path(tmp) / 'signers'), '-I', PRINCIPAL, '-n', 'git',
                                 '-s', str(Path(tmp) / 'sig')], input=payload, capture_output=True)
    if result.returncode:
        raise Refused(f'{tag} is not signed with the release key')
    return found.group(1)


def releases():
    names = git('tag', '--list', 'release-*').stdout.split()
    return sorted(n for n in names if TAG.fullmatch(n))


def verify(tag=None, signers=None):
    """The commit of a verified release tag (default: the newest)."""
    tag = tag or (releases() or [None])[-1]
    if not tag:
        raise Refused('there is no release yet')
    raw = git('cat-file', 'tag', f'refs/tags/{tag}', raw=True).stdout
    return tag, signed_commit(raw, tag, signers if signers is not None else SIGNERS.read_text())


def cut(args):
    key = Path(args.key or os.environ.get('THE_MACHINE_RELEASE_KEY') or Path.home() / '.config/the-machine-release/signing-key')
    if not key.exists():
        raise SystemExit(f'No release key at {key}. Only the release key holder can cut a release.')
    git('fetch', '--quiet', '--tags', 'origin', 'main')
    sha = git('rev-parse', 'origin/main^{commit}').stdout.strip()
    for name in reversed(releases()):
        if git('rev-parse', f'{name}^{{commit}}').stdout.strip() == sha:
            print(f'origin/main ({sha[:7]}) is already released as {name}.')
            return
    tag = 'release-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M')
    if git('rev-parse', '-q', '--verify', f'refs/tags/{tag}', check=False).returncode == 0:
        raise SystemExit(f'{tag} exists already: wait a minute and try again.')
    try:
        require_release_scan('TheMachine', sha, cwd=ROOT)
    except RuntimeError as error:
        raise SystemExit(f'Not released: {error}') from error
    subject = git('log', '-1', '--format=%s', sha).stdout.strip()
    git('-c', 'gpg.format=ssh', '-c', f'user.signingkey={key}', '-c', f'user.name=The Machine releases', '-c', f'user.email={PRINCIPAL}',
        'tag', '-s', '-m', f'The Machine {tag}\n\nUp to: {subject}', tag, sha)
    try:
        verify(tag)
    except Refused as error:
        git('tag', '-d', tag, check=False)
        raise SystemExit(f'Not released: {error}. Is {key} the key in deploy/release-signers?')
    if not args.no_push:
        git('push', '--quiet', 'origin', f'refs/tags/{tag}')
    print(f'Released {sha[:7]} as {tag}{"" if args.no_push else " (pushed)"}. Salesflare Machines take it first; '
          'everyone else after the soak time, or sooner when promoted on /admin.')


def update(args):
    git('fetch', '--quiet', '--tags', 'origin')
    tag, sha = verify(args.tag)
    if git('merge-base', '--is-ancestor', sha, 'HEAD', check=False).returncode == 0:
        print(f'Already on {tag} or newer.')
        return
    result = git('merge', '--ff-only', '--quiet', sha, check=False)
    if result.returncode:
        raise SystemExit(f'{tag} is verified, but this checkout has its own commits: rebase them onto {tag}. '
                         + result.stderr.strip()[-300:])
    print(f'Updated to {tag} ({sha[:7]}), signature verified. Now run ./install.sh')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('cut', help='sign origin/main as a new release and push it')
    c.add_argument('--key', help='private key file (default: $THE_MACHINE_RELEASE_KEY or ~/.config/the-machine-release/signing-key)')
    c.add_argument('--no-push', action='store_true', help='tag and verify locally only')
    v = sub.add_parser('verify', help='check a release tag against deploy/release-signers')
    v.add_argument('tag', nargs='?')
    u = sub.add_parser('update', help='move this checkout to the newest signed release')
    u.add_argument('--tag', help='a specific release instead of the newest')
    args = parser.parse_args(argv)
    try:
        if args.command == 'cut':
            cut(args)
        elif args.command == 'verify':
            tag, sha = verify(args.tag)
            print(f'{tag}: signed with the release key, commit {sha}')
        else:
            update(args)
    except Refused as error:
        raise SystemExit(f'Unsigned update refused: {error}')


if __name__ == '__main__':
    main()
