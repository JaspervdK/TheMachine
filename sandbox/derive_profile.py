"""Shape of a real install, as counts only: how many signals per source and kind, how cards and promises split by
status. No text, names, addresses, ids or account names leave the database; fixture.py scales these numbers into the
synthetic test data. Usage: python3 sandbox/derive_profile.py <events.sqlite copy> > sandbox/profile.json"""
import json
import sqlite3
import sys

SERVICES = ('gmail', 'calendar', 'slack', 'intercom', 'whatsapp', 'linkedin', 'x', 'crm', 'stripe', 'github', 'android', 'gateway')


def service(account_id):
    tail = account_id.rsplit('-', 1)[-1]
    return tail if tail in SERVICES else 'other'


def derive(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    days = db.execute("SELECT julianday(max(occurred_at))-julianday(min(occurred_at)) FROM events WHERE occurred_at>datetime('now','-30 day')").fetchone()[0] or 1
    per_day = {}
    for account_id, kind, n in db.execute("SELECT account_id,kind,count(*) FROM events WHERE occurred_at>datetime('now','-30 day') GROUP BY 1,2"):
        key = service(account_id) + ' ' + kind
        per_day[key] = round(per_day.get(key, 0) + n / days, 1)
    count = lambda sql: {' '.join(str(x) for x in row[:-1]): row[-1] for row in db.execute(sql)}
    return {
        'note': 'Counts only, from a real install; no content or identifiers.',
        'signals_per_day': dict(sorted(per_day.items(), key=lambda x: -x[1])),
        'accounts_per_service': {s: n for s in SERVICES
                                 if (n := sum(1 for (a,) in db.execute('SELECT account_id FROM checkpoints') if service(a) == s))},
        'cards_by_status': count('SELECT status,count(*) FROM suggestions GROUP BY 1'),
        'cards_with_draft': db.execute("SELECT count(*) FROM suggestions WHERE next_action IS NOT NULL").fetchone()[0],
        'promises': count('SELECT direction,status,count(*) FROM promises GROUP BY 1,2'),
        'decisions': count('SELECT choice,count(*) FROM decisions GROUP BY 1'),
        'reviews': count('SELECT status,count(*) FROM review_runs GROUP BY 1'),
        'thread_length': db.execute("SELECT round(avg(n),1) FROM (SELECT count(*) n FROM events WHERE kind='gmail.message' "
                                    "GROUP BY json_extract(metadata,'$.thread_id'))").fetchone()[0],
        'projects': db.execute('SELECT count(*) FROM projects').fetchone()[0],
        'people': db.execute("SELECT count(*) FROM graph_nodes WHERE kind='person'").fetchone()[0],
        'orgs': db.execute("SELECT count(*) FROM graph_nodes WHERE kind='org'").fetchone()[0],
    }


if __name__ == '__main__':
    print(json.dumps(derive(sys.argv[1]), indent=1))
