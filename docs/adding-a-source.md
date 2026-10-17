# Adding a source

A source (connector) reads one account of one service and stores what it finds as **events**. Reviews then decide what deserves a card. A connector never interprets, guesses or writes: no AI, no replies, no marking as read.

There are two ways to add one:

- **A connector plugin**: one Python file in `connectors/`, named after its service. No changes to `assistant.py`. This is the way for most sources. [`connectors/rss.py`](../connectors/rss.py) is a complete example; [`github.py`](../connectors/github.py) (a CLI) and [`stripe.py`](../connectors/stripe.py) (an API with a key, filtering what's worth storing) are two more, and [`x.py`](../connectors/x.py) and [`linkedin.py`](../connectors/linkedin.py) read through the signed-in OpenClaw browser.
- **A built-in source**: a branch in `sync_source` in `assistant.py`, for sources that need deeper integration (realtime threads, special review handling).

This guide covers the plugin.

## 1. The account in `config.json`

```json
{"id": "work-feed", "service": "rss", "transport": "http", "boundary": "work",
 "url": "https://example.com/feed.xml", "poll_seconds": 1800}
```

- `id` is the account's own ID. Its checkpoint, health and events are keyed by it.
- `service` picks the connector: `rss` loads `connectors/rss.py`.
- `boundary` (`work` or `personal`) keeps signals apart in reviews and on the dashboard.
- Any other keys (`url` here) are yours to read in the connector. Keep secrets out of `config.json`: name a credential (`"credential": "stripe"`) and read it with `helpers.credential`.

## 2. The module

```python
KINDS = {'rss.item': 'feed'}          # event kind -> short label for cards and reviews

def sync(store, config, account, helpers):
    state = store.checkpoint(account['id']) or {}
    ...                                # read new items since state
    store.commit_page(account['id'], state, events)
    return {'mode': 'incremental', 'events': len(events)}
```

The service calls `sync` every `poll_seconds` (default 300) in the account's own thread. `helpers` has:

| Helper | Use |
|---|---|
| `helpers.SourceError(category, status=None, retry_after=None)` | Raise on any failure. `category` is a short label like `rss_http`; it's shown on the dashboard, so never put message content or secrets in it. `retry_after` (seconds) delays the next try. |
| `helpers.iso(timestamp)` | A Unix timestamp as the UTC ISO string `occurred_at` expects. |
| `helpers.now()` | The current time as that string. |
| `helpers.local_tz()` | The owner's time zone (config `timezone`), for things like active hours. |
| `helpers.credential(config, name)` | The secret named `name` in `config['credentials']` (or systemd's `LoadCredential`), as text. Use it for API keys instead of reading files yourself. |

### Setup: `assistant connect <service>`

Declare `SETUP` so the owner can add an account without editing JSON:

```python
SETUP = {'label': 'RSS feed',                          # name in prompts, doctor and next steps
         'account': {'id': 'feed', 'service': 'rss', 'transport': 'http', 'boundary': 'work', 'poll_seconds': 1800},
         'note': 'Shown before the questions, e.g. where to find a key.',
         'ask': [('url', 'Feed URL (RSS or Atom)')],     # (key, prompt[, default]); defaults can use '{other_key}'
         'detect': {},                                   # key -> command whose output is the default, e.g. ['gh', 'api', 'user', '--jq', '.login']
         'secret': None}                                 # a prompt here asks for a key without echo into the `credential` file (mode 600)
```

`assistant connect` adds the account from `account`, asks each `ask` key (empty answers stop it), stores the secret, saves `config.json`, restarts the service and shows the first sync. With `credential_variable` in `account`, the secret is written as `VARIABLE=value`. `assistant doctor` treats a missing `ask` key or credential file as not set up. A test checks every connector has a complete `SETUP`.

## 3. Events

Each event is a dict with exactly these fields:

| Field | Meaning |
|---|---|
| `account_id` | `account['id']`. `commit_page` refuses events for another account. |
| `source_id` | Stable ID of the item in the source. The same item must always get the same ID; hash it if it's long or contains personal data. |
| `kind` | `<service>.<type>`, e.g. `rss.item`. Add it to `KINDS`. |
| `occurred_at` | When it happened, UTC ISO. |
| `title` | A short line (max 1000 chars): subject, chat name, item title. |
| `body` | The text (max 40000 chars). Plain text; strip HTML. Omit one-time codes and passwords. |
| `metadata` | JSON with what reviews need: `source_url` (an https link to open the original, shown as Open), sender, thread or conversation IDs, `from_me` for the owner's own messages. |
| `deleted` | `True` when the item was removed at the source. |

When an item changes, send it again with the same `source_id`: the service stores a new version and reviews see it again. Unchanged items are skipped by content hash, so rereading is cheap and safe.

## 4. Checkpoints

`store.commit_page(account_id, state, events)` stores the events **and** the new `state` in one transaction. If the process dies mid-read, the next run starts from the old state and rereads the same page; nothing is lost or doubled.

- Put your cursor in `state`: a page token, a `since` time, an ETag. It's JSON, read back with `store.checkpoint(account_id)`.
- For a long backlog, commit page by page, each with the cursor of the next page.
- When the source rejects an expired cursor, drop it and reread a bounded recent window (`config.get('bootstrap_days', 7)`).
- On the first run, read only that recent window, not years of history.

## 5. Dashboard and noise rules

- The account appears under Sources with its health, last read and errors. It gets a generic icon unless you add one to `ICONS` in `dashboard.html`.
- `exclude` rules (`senders`, `titles`, `text`) work on plugin events too. See Noise rules in the README.
- Every event opens in the dashboard's reader with its title, sender (`sender_display` or `from`, "You" when `from_me`) and body, so a readable plain-text `body` is all it needs.
- To put the people in your events on the Graph tab, add a branch for your `kind` to `touches()` in [`graph.py`](../graph.py): it returns who took part, with a stable identifier (`email:`, `wa:`, or your own prefix), a name and their role (you wrote to them, they wrote to you, a meeting, or just present). Without it the source works as before; it just adds no people.

## 6. Tests

Test with saved fixtures, not live accounts. [`test_connectors.py`](../test_connectors.py) shows the pattern for `rss.py`: it fakes the HTTP response and checks

- the events and their fields,
- that a second run with the same feed adds nothing,
- that `304 Not Modified` keeps the checkpoint,
- that errors raise `SourceError` with a safe label.

Then run `assistant connect <service>`: it adds the account, restarts the service and shows the first read. Check it later with `assistant sources`, `assistant doctor` or on the dashboard.

## Checklist

- [ ] Read-only credential for exactly this account, with the least access that works.
- [ ] Stable `source_id`; `kind` listed in `KINDS`.
- [ ] Events and cursor committed together; replays don't duplicate.
- [ ] Failures raise `SourceError` with a content-free label.
- [ ] Fixture tests for parsing, replays, expired cursors and errors.
- [ ] `SETUP` with what `assistant connect` should ask.
