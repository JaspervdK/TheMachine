"""Example connector: an RSS or Atom feed.

Config: {"id": "work-feed", "service": "rss", "transport": "http", "boundary": "work",
         "url": "https://example.com/feed.xml", "poll_seconds": 1800}

A connector is one module in connectors/ named after its service. It exposes
sync(store, config, account, helpers) and optionally KINDS (event kind -> label).
"""
import calendar
import datetime as dt
import email.utils
import hashlib
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

KINDS = {'rss.item': 'feed'}
ATOM = '{http://www.w3.org/2005/Atom}'


def fetch(url, state, SourceError):
    request = urllib.request.Request(url, headers={'User-Agent': 'TheMachine/1 (+feed reader)'})
    if state.get('etag'):
        request.add_header('If-None-Match', state['etag'])
    if state.get('last_modified'):
        request.add_header('If-Modified-Since', state['last_modified'])
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(5_000_001), response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, error.headers
        raise SourceError('rss_http', error.code, retry_after=3600 if error.code == 429 else None) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise SourceError('network:' + type(error).__name__ + ':rss_read') from None


def text(node, *names):
    for name in names:
        found = node.find(name)
        if found is not None and (found.text or found.get('href')):
            return (found.text or found.get('href')).strip()
    return ''


def when(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_tz(value)
        if parsed:
            return calendar.timegm(parsed[:9]) - (parsed[9] or 0)
    except (TypeError, ValueError):
        pass
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})', value.strip())
    if not match:
        return None
    return dt.datetime.fromisoformat(f'{match[1]}T{match[2]}{"+00:00" if match[3] == "Z" else match[3]}').timestamp()


def item_event(account, node, helpers):
    title = text(node, 'title', ATOM + 'title') or 'Untitled item'
    link = text(node, 'link', ATOM + 'link')
    guid = text(node, 'guid', ATOM + 'id') or link or title
    stamp = when(text(node, 'pubDate', ATOM + 'updated', ATOM + 'published'))
    if stamp is None:
        raise helpers.SourceError('rss_invalid_timestamp')
    body = re.sub(r'<[^>]+>', ' ', text(node, 'description', ATOM + 'summary', ATOM + 'content'))
    return {'account_id': account['id'],
            'source_id': hashlib.sha256(guid.encode()).hexdigest(),
            'kind': 'rss.item',
            'occurred_at': helpers.iso(stamp),
            'title': title[:1000],
            'body': re.sub(r'\s+', ' ', body).strip()[:40000],
            'metadata': {'url': link, 'feed': account['url'], 'guid': guid},
            'deleted': False}


def sync(store, config, account, helpers):
    if not re.fullmatch(r'https://\S+', account.get('url') or ''):
        raise helpers.SourceError('rss_missing_url')
    state = store.checkpoint(account['id']) or {}
    raw, headers = fetch(account['url'], state, helpers.SourceError)
    events = []
    if raw is not None:
        if len(raw) > 5_000_000:
            raise helpers.SourceError('rss_feed_too_large')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise helpers.SourceError('rss_invalid_xml') from None
        nodes = root.findall('./channel/item') or root.findall(ATOM + 'entry')
        events = list({e['source_id']: e for e in (item_event(account, n, helpers) for n in nodes[:200])}.values())
    state.update(etag=headers.get('ETag') or state.get('etag'),
                 last_modified=headers.get('Last-Modified') or state.get('last_modified'))
    # Unchanged items are skipped by content hash, so rereading the feed is idempotent.
    store.commit_page(account['id'], state, events)
    return {'mode': 'incremental', 'events': len(events)}
