# Architecture and reviews

[Documentation index](../README.md). Current reference; dated repair evidence lives in [History](history.md).

## Architecture

```text
Gmail · Calendar · Slack · CRM · Android notifications · WhatsApp · OpenClaw Gateway · plugins (GitHub, Stripe, LinkedIn, X, RSS)
                │  (read-only collectors, one thread and checkpoint per account)
                ▼
assistant.py (systemd user service) → private SQLite + full-text search, local API on 127.0.0.1
                │
                ▼
OpenClaw main agent, isolated review turns started by the service → short suggestions
                │
                ▼
Telegram bot (via OpenClaw) ⇄ owner decisions ⇄ dashboard on your tailnet · `assistant` CLI
                │
                ▼
Do it → isolated preparation turn of the same agent → result, draft + Send, or blocker
```

| Path | Role |
|---|---|
| `assistant.py` | Collectors, SQLite store, local API, dashboard server, Telegram outbox, review and preparation workers |
| `dashboard.html`, `sw.js`, `manifest.webmanifest`, `brand/` | Owner dashboard and installable web app |
| `cli.py` | The `assistant` command line |
| `next_actions.py` | Approved sends: email (Gmail via `gog`), LinkedIn messages and X replies and DMs (OpenClaw browser), Slack direct messages (Maton) and WhatsApp messages (`wacli`) |
| `preparation_state.py`, `rollout_health.py` | Preparation bookkeeping and health checks |
| `openclaw-plugin/` | `personal-assistant-bridge`: the `assistant_signals` tool, Telegram buttons and replies, Android feed capture, review/preparation guards |
| `*.mjs` | Small Node helpers that call the OpenClaw Gateway (`openclaw-root.mjs` finds the installed package) |
| `integrations/browser-lease.py` | Shares the OpenClaw browser between concurrent Do it runs |
| `browser_viewer.py`, `deploy/browser/` | Optional: a browser of its own on a virtual display that you watch and use from yours (noVNC, 127.0.0.1 only), and bringing a tab to the front on a desktop |
| `browser_login.py` | The sign-in dialog: fills a sign-in page in the assistant's browser with what you type on the dashboard, and keeps saved sign-ins encrypted |
| `integrations/browser_cdp.py` | Talks to a browser's DevTools address directly, for the LinkedIn and X readers when one is set |
| `AGENTS.md`, `PREPARATION.md` | Instructions the plugin gives the agent for reviews and for Do it runs (templates; see below) |
| `install.sh`, `installer.py` | Install, upgrade and uninstall |
| `config.example.json` | Every configuration key with example values |
| `embeddings.py` | Optional related signals by meaning, through a local embedding server |
| `graph.py` | People and projects: a living graph rebuilt from signals, card decisions, project labels and merges |
| `deploy/` | Example systemd units (the service, an optional local embedding server) and Caddyfile |
| `test_*.py`, `test_*.mjs` | Offline tests |

Python's standard library and SQLite are enough: no database server, queue, web framework or extra scheduler. Runtime data lives in `~/.local/share/personal-assistant/`: `config.json`, `events.sqlite`, tokens and receipts.

**Data model.** The main tables are `events` (with an `event_search` full-text index), `checkpoints`, `sync_activity`, `analyses` (reviews), `review_runs`, `suggestions`, `decisions`, `deliveries` (the Telegram outbox), `preparations` and `action_jobs` (approved sends). Provider IDs deduplicate events per account; a changed event gets a new version and becomes reviewable again. Each source page commits its events and next cursor in one transaction, so a crash replays the page instead of losing or duplicating it.

**Boundaries.** Every account has an explicit ID and `boundary` (`work:Acme`, `personal:phone`, `owner:system`…). Every read names its accounts explicitly; app names and chat labels never decide which company an item belongs to.


## Reviews

The service starts reviews itself (`review_worker.enabled`). When at least `min_signals` wait, or the oldest has waited `max_wait_minutes`, it starts one isolated turn of your main agent, `agent:main:assistant-review-signals-<run>`, with message delivery off. The plugin adds `USER.md` and `AGENTS.md` to that turn. The agent claims up to 120 signals, reads them through the `assistant_signals` tool, checks decisions, earlier suggestions and related evidence, saves suggestions with `when`/`due` timing, and finishes the analysis. One run at a time with a gap between runs; failed runs back off. The dashboard (Connections, Signal reviews) shows a running review at the top: how far it is (12 of 40 signals), from which accounts, and its latest step from its own tool calls ("Reading 20 emails", "Looking up Globex", "Suggesting: …", "3 suggestions so far"; earlier steps fold away; never message text or search words), updated every few seconds; then when the next one starts. Finished runs move to Recent runs. Unfinished claims transfer safely to the next run, and the service never restarts the Gateway.

**Things that slipped.** Each review prompt also gets up to five "Possibly slipped" lines of each kind: your own sent emails whose new text asks something (a ? in a link doesn't count) and whose thread has had nothing since, 4-21 days later, but only in threads the other person started or to people who had written to you before you started the thread, so your outreach never shows up; and questions addressed directly to you (at most two To recipients, not Gmail's Updates/Promotions/Social/Forums categories, from someone you have emailed before) with no reply from you in the thread after 2-14 days. Three or more emails sent the same day with the same subject pattern count as outreach too and are left out. Anything already on a card is skipped. The `slipped` table records which review got each one; it's offered again only if that review didn't save within six hours. The review checks for an answer elsewhere before making a card.

### Promises

Reviews record every promise they find in a message, with `promise`, instead of making a card: one you made in something you sent (`mine`), or one someone made to you (`theirs`, in their message or in your ask they took on), with who, what and a date when one was said. Most are never spelled out and look different per person, language and relationship, so the review judges what a message means in its conversation (reading the request in `earlier_in_conversation`), with examples as a guide rather than words to look for. Each goes into the `promises` table, once per message, person, direction and distinct promised action. Nothing shows up on Telegram yet.

- **When it's checked:** yours on the morning it's due (at `digest_at`, never sooner than three hours after it was recorded), theirs the morning after their date. Without a date: yours after 3 days, theirs after a week.
- **Closing by itself:** someone else's promise made by email closes when that sender replies in the original thread (sender-only fallback if no thread was recorded). Chats run on, so a later chat message is no sign it was kept: those stay open for the review or the check. Their new message is reviewed as usual, so a delay can become a new promise. Every review prompt lists the open promises, and the review closes one (`kept` or `dropped`, with the reason) or moves its date when a later message shows it.
- **The card:** a promise still open when it's checked becomes a card (`promise:<id>`), prepared ahead under the usual limits: for yours the thing you promised, for theirs a short nudge. If they answer while the card is on its way, it closes. At most 5 such cards a day.
- **Where you see them:** the dashboard's Suggestions tab has a **Promises** list (You promised, Promised to you) with Done and Drop for open ones, `assistant promises` lists them, and the weekly recap counts kept and open ones.

Your decisions and written reasons are summarised into every review prompt, and lasting facts you state are saved (`remember_fact`) and listed, removable, on the dashboard. Style notes that hold beyond one card (signatures, formatting, tone) become **draft rules** that every later draft follows. Each Do it result says whether it's finished or what it needs from you, and the run sorts your Change notes (style, missing fact, keep going, new ask). The Feedback tab shows the split, the hit rate per day or week, and markers where the rules changed.
