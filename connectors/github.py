"""GitHub: threads you take part in (review requests, mentions, assignments, replies) and failed CI runs.

Read-only through the owner's logged-in `gh` CLI: GET requests only; notifications are never
marked as read.

Config:
    {"id": "github", "service": "github", "transport": "gh", "boundary": "work",
     "login": "your-login", "poll_seconds": 600}
Optional: "repos": ["owner/repo", ...] to keep only those repositories; "ci_repos": [...] and
"ci_branch" (default "main") to report failed workflow runs on that branch; config "gh" for the
path to the CLI.
"""
import datetime as dt
import json
import re
import subprocess

KINDS = {'github.notification': 'GitHub', 'github.ci': 'GitHub CI'}
REASONS = {'assign', 'author', 'comment', 'mention', 'review_requested', 'team_mention', 'security_alert', 'manual'}


class Source:
    def __init__(self, config, account, helpers):
        self.config, self.account, self.helpers = config, account, helpers
        self.verified = False

    def get(self, path):
        SourceError = self.helpers.SourceError
        if not re.fullmatch(r'[A-Za-z0-9_./?=&%:+,-]+', path) or path.startswith(('/', 'graphql')):
            raise SourceError('github_path_not_allowed')
        try:
            done = subprocess.run([self.config.get('gh', 'gh'), 'api', '--method', 'GET', path], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceError('network:' + type(error).__name__ + ':github_read') from None
        if done.returncode:
            code = re.search(r'HTTP (\d{3})', done.stderr or '')
            raise SourceError('github', int(code.group(1)) if code else None)
        try:
            return json.loads(done.stdout or 'null')
        except ValueError:
            raise SourceError('github_invalid_json') from None

    def verify(self):
        """gh must be logged in as the configured login, not whoever ran `gh auth login` last."""
        if not self.verified:
            if (self.get('user') or {}).get('login', '').lower() != self.account['login'].lower():
                raise self.helpers.SourceError('account_identity_mismatch')
            self.verified = True


def html(url):
    """API URL of an issue, PR or commit -> the page in the browser."""
    m = re.fullmatch(r'https://api\.github\.com/repos/([^/]+/[^/]+)/(issues|pulls|commits)/([^/]+)', url or '')
    if not m:
        return ''
    return f"https://github.com/{m.group(1)}/{ {'pulls': 'pull', 'commits': 'commit'}.get(m.group(2), 'issues')}/{m.group(3)}"


def utc(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc).isoformat()


def api_path(url):
    return url.split('api.github.com/', 1)[1] if (url or '').startswith('https://api.github.com/') else None


def sync(store, config, account, helpers, source=None):
    source = source or Source(config, account, helpers)
    source.verify()
    state = store.checkpoint(account['id']) or {}
    since = state.get('since') or (dt.datetime.fromisoformat(helpers.now()) - dt.timedelta(days=int(config.get('bootstrap_days', 7)))).strftime('%Y-%m-%dT%H:%M:%SZ')
    repos = {r.lower() for r in account.get('repos', [])}
    me = account['login'].lower()
    events, newest = [], since
    for page in range(1, 6):
        items = source.get(f'notifications?all=true&participating=true&per_page=50&page={page}&since={since}') or []
        for n in items:
            newest = max(newest, n.get('updated_at') or newest)
            repo = (n.get('repository') or {}).get('full_name', '')
            if n.get('reason') not in REASONS or (repos and repo.lower() not in repos):
                continue
            subject = n.get('subject') or {}
            detail = source.get(api_path(subject.get('url'))) or {} if api_path(subject.get('url')) else {}
            latest = {}
            comment = subject.get('latest_comment_url') or ''
            if comment != subject.get('url') and api_path(comment):
                try:
                    latest = source.get(api_path(comment)) or {}
                except helpers.SourceError as error:
                    if error.status != 404:
                        raise
            author = (latest.get('user') or detail.get('user') or {}).get('login') or ''
            url = latest.get('html_url') or detail.get('html_url') or html(subject.get('url')) or f'https://github.com/{repo}'
            status = ('merged' if detail.get('merged_at') else detail.get('state') or '') if detail else ''
            body = '\n\n'.join(x for x in [
                f"{subject.get('type', 'Thread')} in {repo}" + (f" ({status})" if status else '') + f"; why you got it: {n.get('reason', '').replace('_', ' ')}.",
                (detail.get('body') or '')[:6000],
                f"Latest comment by {author}:\n{(latest.get('body') or '')[:6000]}" if latest else ''] if x)
            events.append({'account_id': account['id'], 'source_id': 'n:' + str(n['id']), 'kind': 'github.notification',
                           'occurred_at': utc(n['updated_at']), 'title': f"[{repo.split('/')[-1]}] {subject.get('title', '')}"[:1000],
                           'body': body[:40000], 'deleted': False,
                           'metadata': {'repo': repo, 'reason': n.get('reason'), 'type': subject.get('type'), 'number': detail.get('number'),
                                        'state': status, 'from': author, 'from_me': author.lower() == me, 'source_url': url}})
        if len(items) < 50:
            break
    for repo in account.get('ci_repos', []):
        branch = account.get('ci_branch', 'main')
        runs = (source.get(f"repos/{repo}/actions/runs?branch={branch}&status=failure&per_page=20&created=%3E{since[:10]}") or {}).get('workflow_runs', [])
        for run in runs:
            actor = (run.get('actor') or {}).get('login', '')
            events.append({'account_id': account['id'], 'source_id': 'ci:' + str(run['id']), 'kind': 'github.ci',
                           'occurred_at': utc(run['updated_at']), 'title': f"[{repo.split('/')[-1]}] {run.get('name', 'CI')} failed on {branch}"[:1000],
                           'body': f"Commit: {((run.get('head_commit') or {}).get('message') or '')[:2000]}\nBy: {actor}",
                           'deleted': False, 'metadata': {'repo': repo, 'from': actor, 'from_me': actor.lower() == me, 'source_url': run.get('html_url', '')}})
    store.commit_page(account['id'], {'mode': 'incremental', 'since': newest}, events)
    return {'mode': 'incremental', 'events': len(events)}
