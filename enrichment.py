"""Organisation data from an enrichment service, looked up by domain.

Works with any service that answers GET <url with the domain> with JSON about the company: a preset
(apollo, peopledatalabs, hunter) or your own url template with {domain} and {key}, and headers that can
use {key} too. The key is read from a file or an environment variable, never stored in the config.
Only organisation domains from the graph are sent (never free-mail or the owner's own domains), a few
at a time, each looked up again after REFRESH_DAYS. What comes back is normalised to a small profile
(industry, size, location, founded, about, links) that the graph, reviews and preparations show next
to the organisation; it describes the company, never what it is to the owner."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REFRESH_DAYS = 90
RETRY_HOURS = 24        # after an error (timeout, rate limit)
PER_PASS = 5
PER_DAY = 50
PRESETS = {
    'apollo': {'url': 'https://api.apollo.io/api/v1/organizations/enrich?domain={domain}', 'headers': {'X-Api-Key': '{key}'}},
    'peopledatalabs': {'url': 'https://api.peopledatalabs.com/v5/company/enrich?website={domain}', 'headers': {'X-Api-Key': '{key}'}},
    'hunter': {'url': 'https://api.hunter.io/v2/companies/find?domain={domain}&api_key={key}'},
}
# Where common services keep each field; the first one present wins. A config "fields" map adds its own paths first.
FIELDS = {
    'name': ['name', 'display_name', 'legalName', 'legal_name'],
    'industry': ['industry', 'category.industry', 'industries.0', 'category.sector'],
    'employees': ['employees_num', 'employee_count', 'estimated_num_employees', 'metrics.employees', 'employees'],
    'size': ['size', 'employee_range', 'metrics.employeesRange', 'employees_range'],
    'founded': ['founded', 'founded_year', 'foundedYear', 'year_founded'],
    'location': ['headquarters', 'location.name', 'location.locality', 'location.city', 'geo.city', 'city'],
    'country': ['location.country_code', 'location.country', 'geo.countryCode', 'country'],
    'about': ['about_us', 'description', 'summary', 'short_description', 'seo_description', 'overview'],
    'linkedin_url': ['linkedin_url', 'linkedin.url', 'linkedin.handle', 'linkedin'],
    'website': ['website', 'website_url', 'url', 'domain'],
    'type': ['type', 'company_type', 'ownership'],
}
WRAPPERS = ('organization', 'company', 'data', 'result')


class EnrichmentError(Exception):
    pass


def settings(config):
    """The enrichment settings with a preset filled in, or None when it isn't set up."""
    raw = config.get('enrichment')
    if not isinstance(raw, dict) or raw.get('enabled') is False:
        return None
    merged = {**PRESETS.get(str(raw.get('provider') or '').lower(), {}), **{k: v for k, v in raw.items() if v not in (None, '')}}
    return merged if merged.get('url') and '{domain}' in merged['url'] else None


def key(cfg):
    if cfg.get('key_env') and os.environ.get(cfg['key_env']):
        return os.environ[cfg['key_env']].strip()
    if cfg.get('key_file'):
        path = Path(os.path.expanduser(cfg['key_file']))
        directory = os.environ.get('CREDENTIALS_DIRECTORY')  # systemd LoadCredential, when used
        if directory and (Path(directory) / path.name).exists():
            path = Path(directory) / path.name
        return path.read_text().strip()
    return ''


def value(data, path):
    for part in path.split('.'):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def normalise(data, fields=None):
    """A small profile from whatever shape the service returns; None when it found nothing."""
    if isinstance(data, list):
        data = data[0] if data else None
    for wrapper in WRAPPERS:
        if isinstance(data, dict) and isinstance(data.get(wrapper), dict):
            data = data[wrapper]
            break
    if not isinstance(data, dict):
        return None
    profile = {}
    for field, paths in FIELDS.items():
        for path in list((fields or {}).get(field) or []) + paths:
            found = value(data, path)
            if found in (None, '', [], {}) or isinstance(found, (dict, list)):
                continue
            if field in ('employees', 'founded'):
                try:
                    found = int(found)
                except (TypeError, ValueError):
                    continue
            else:
                found, limit = ' '.join(str(found).split()), 300 if field == 'about' else 120
                if len(found) > limit:
                    found = found[:limit - 1].rsplit(' ', 1)[0].rstrip(',.;:') + '…'
            if field == 'linkedin_url' and not found.startswith('http'):
                found = 'https://www.linkedin.com/company/' + found.strip('/').rsplit('/', 1)[-1]
            profile[field] = found
            break
    return profile if profile.get('name') or profile.get('industry') or profile.get('about') else None


def fetch(cfg, domain, timeout=15):
    """The profile for one domain, None when the service doesn't know it. Errors never carry the key or url."""
    secret = key(cfg)
    if not secret and ('{key}' in cfg['url'] or '{key}' in json.dumps(cfg.get('headers') or {})):
        raise EnrichmentError('no key: set key_file or key_env in enrichment')
    url = cfg['url'].replace('{domain}', urllib.parse.quote(domain)).replace('{key}', urllib.parse.quote(secret))
    headers = {'Accept': 'application/json', 'User-Agent': 'the-machine/1 (+https://themachine.computer)'}
    headers.update({k: str(v).replace('{key}', secret) for k, v in (cfg.get('headers') or {}).items()})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            body = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (404, 422):
            return None
        raise EnrichmentError({401: 'the service rejected the key', 403: 'the service refused (key or plan)',
                               429: 'rate limited'}.get(error.code, f'HTTP {error.code}')) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EnrichmentError('unreachable: ' + type(getattr(error, 'reason', error)).__name__) from None
    try:
        return normalise(json.loads(body or b'null'), cfg.get('fields'))
    except ValueError:
        raise EnrichmentError('the answer was not JSON') from None


def due(db, now, limit):
    """Organisation domains from the graph, strongest first, that were never looked up or are due again."""
    current = dt.datetime.fromisoformat(now)
    fresh = (current - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    retry = (current - dt.timedelta(hours=RETRY_HOURS)).isoformat()
    rows = db.execute("SELECT json_extract(n.info,'$.domain') domain FROM graph_nodes n LEFT JOIN org_profiles p ON p.domain=json_extract(n.info,'$.domain') "
                      "WHERE n.kind='org' AND (p.domain IS NULL OR (p.status!='error' AND p.fetched_at<?) OR (p.status='error' AND p.fetched_at<?)) "
                      'ORDER BY n.strength DESC LIMIT ?', (fresh, retry, limit)).fetchall()
    return [r[0] for r in rows if r[0]]


def save(db, domain, status, profile, now):
    db.execute('INSERT OR REPLACE INTO org_profiles(domain,status,profile,fetched_at) VALUES(?,?,?,?)',
               (domain, status, json.dumps(profile) if profile else None, now))


def step(connect, config, now, per_pass=PER_PASS):
    """Look up a few organisations. Returns how many got a profile; stops at the first service error."""
    cfg = settings(config)
    if not cfg:
        return 0
    with connect() as db:
        today = now[:10]
        used = db.execute('SELECT count(*) FROM org_profiles WHERE substr(fetched_at,1,10)=?', (today,)).fetchone()[0]
        domains = due(db, now, max(0, min(per_pass, int(cfg.get('per_day') or PER_DAY) - used)))
    found = 0
    for domain in domains:
        try:
            profile = fetch(cfg, domain)
        except EnrichmentError:
            with connect() as db:
                save(db, domain, 'error', None, now)
            raise
        with connect() as db:
            save(db, domain, 'found' if profile else 'none', profile, now)
        found += bool(profile)
    return found


def summary(profile):
    """One short line: "Software Development, 2-10 people, Antwerp, founded 2014"."""
    if not profile:
        return ''
    size = profile.get('size') or (str(profile['employees']) if profile.get('employees') else '')
    bits = [profile.get('industry'), size and (size + ' people'), profile.get('location'), profile.get('founded') and f"founded {profile['founded']}"]
    return ', '.join(str(b) for b in bits if b)


def status(db, config):
    cfg = settings(config)
    counts = dict(db.execute('SELECT status,count(*) FROM org_profiles GROUP BY status').fetchall())
    last = db.execute("SELECT domain,fetched_at FROM org_profiles WHERE status='error' ORDER BY fetched_at DESC LIMIT 1").fetchone()
    host = urllib.parse.urlsplit(cfg['url']).hostname if cfg else None
    return {'service': host, 'found': counts.get('found', 0), 'unknown': counts.get('none', 0), 'errors': counts.get('error', 0),
            'last_error_at': last[1] if last else None, 'refresh_days': REFRESH_DAYS}


def lookup(connect, config, domain, now):
    """One domain now (assistant enrich <domain>): looked up, saved, returned."""
    cfg = settings(config)
    if not cfg:
        raise EnrichmentError('Enrichment is not set up: add "enrichment" to the config (see README, Enrichment)')
    domain = re.sub(r'^(?:https?://)?(?:www\.)?', '', str(domain or '').strip().lower()).split('/')[0]
    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', domain):
        raise EnrichmentError('Give a domain, like globex.com')
    profile = fetch(cfg, domain)
    with connect() as db:
        save(db, domain, 'found' if profile else 'none', profile, now)
    return {'domain': domain, 'profile': profile}
