# The Machine

A private, self-hosted assistant inbox for [OpenClaw](https://docs.openclaw.ai). It reads your email, calendar, chat and phone notifications, has your own OpenClaw agent look for what actually needs you, and sends a few short suggestions to Telegram. Tap **Do it** and the agent carries the task out.

Website: [themachine.computer](https://themachine.computer). **Early release:** intended for a single owner who already uses OpenClaw. Managed installation is Linux-only.

## Quick start

```bash
git clone https://github.com/JeroenCorthout/TheMachine.git ~/.openclaw/workspace/tools/personal-assistant
cd ~/.openclaw/workspace/tools/personal-assistant
./install.sh
```

Read [Requirements](#requirements) first. The installer checks prerequisites, asks for your owner identity and source choices, writes the config and secrets, installs the systemd user service and the OpenClaw plugin, and ends with a health check and the remaining manual steps (connecting Gmail, exposing the dashboard on your tailnet). Fresh installs collect configured sources but leave AI reviews and Telegram delivery **off**. To see what it would do for you first, run `./install.sh --preview`: your agent reviews the last 7 days of signals once and the resulting cards appear in the terminal and on the dashboard, with nothing sent and nothing done in your accounts. Once you have connected the sources, checked `assistant sources` and read [Security model](#security-model), run `./install.sh --activate`. Run the installer again to upgrade; `./install.sh --uninstall` removes it again (see [Install](#install)).

Contents: [What it does](#what-it-does) · [Architecture](#architecture) · [Requirements](#requirements) · [Install](#install) · [Configuration](#configuration) · [Sources](#sources) · [Reviews](#reviews) · [Telegram and the dashboard](#telegram-and-the-dashboard) · [Do it](#do-it) · [Related signals](#related-signals-optional-local-embeddings) · [Security model](#security-model) · [Command line](#command-line) · [Operations](#operations) · [Adding a source](#adding-a-source) · [Known limitations](#known-limitations) · [Tests](#tests)

## What it does

- **Collects** signals from Gmail, Google Calendar, Slack, the Salesflare CRM, Android notifications (through the OpenClaw node app), WhatsApp (through a local `wacli` store) and the OpenClaw Gateway's own failures, plus GitHub, Stripe, LinkedIn, X and RSS through [connector plugins](docs/adding-a-source.md). Collection is incremental and read-only by default; everything goes into one private SQLite database with full-text search.
- **Reviews** waiting signals in isolated turns of your existing OpenClaw main agent. The agent saves a suggestion only when something is worth your time: an unanswered request, a deadline, a problem it can fix, or an email of yours that slipped.
- **Delivers** suggestions as Telegram cards through your existing OpenClaw bot, with a phone-friendly dashboard (installable as an app, with optional push notifications) on your tailnet.
- **Acts** when you tap **Do it**: the agent drafts the email, LinkedIn or Slack message, or X reply (with a Send button), makes the fix, does the research, or hands the task to a colleague and waits for their reply, and reports back on the same card. **Change** lets you reply with instructions; **Snooze**, **Dismiss** and **Mark done** work as you'd expect, and your choices steer later reviews.

The service itself contains no model. All interpretation happens in your OpenClaw agent, with your model provider: see [Bring your own model](docs/models.md) for choosing one, including local models.

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
| `next_actions.py` | Approved sends: email (Gmail via `gog`), LinkedIn messages and X replies and DMs (OpenClaw browser) and Slack direct messages (Maton) |
| `preparation_state.py`, `rollout_health.py` | Preparation bookkeeping and health checks |
| `openclaw-plugin/` | `personal-assistant-bridge`: the `assistant_signals` tool, Telegram buttons and replies, Android feed capture, review/preparation guards |
| `*.mjs` | Small Node helpers that call the OpenClaw Gateway (`openclaw-root.mjs` finds the installed package) |
| `integrations/browser-lease.py` | Shares the OpenClaw browser between concurrent Do it runs |
| `AGENTS.md`, `PREPARATION.md` | Instructions the plugin gives the agent for reviews and for Do it runs (templates; see below) |
| `install.sh`, `installer.py` | Install, upgrade and uninstall |
| `config.example.json` | Every configuration key with example values |
| `embeddings.py` | Optional related signals by meaning, through a local embedding server |
| `deploy/` | Example systemd units (the service, an optional local embedding server) and Caddyfile |
| `site/` | The project website, [themachine.computer](https://themachine.computer) (GitHub Pages, `.github/workflows/pages.yml`) |
| `test_*.py`, `test_*.mjs` | Offline tests |

Python's standard library and SQLite are enough: no database server, queue, web framework or extra scheduler. Runtime data lives in `~/.local/share/personal-assistant/`: `config.json`, `events.sqlite`, tokens and receipts.

**Data model.** The main tables are `events` (with an `event_search` full-text index), `checkpoints`, `sync_activity`, `analyses` (reviews), `review_runs`, `suggestions`, `decisions`, `deliveries` (the Telegram outbox), `preparations` and `action_jobs` (approved sends). Provider IDs deduplicate events per account; a changed event gets a new version and becomes reviewable again. Each source page commits its events and next cursor in one transaction, so a crash replays the page instead of losing or duplicating it.

**Boundaries.** Every account has an explicit ID and `boundary` (`work:Acme`, `personal:phone`, `owner:system`…). Every read names its accounts explicitly; app names and chat labels never decide which company an item belongs to.

## Requirements

- Linux with systemd user services (macOS works for development and tests, not as a managed service).
- **Python 3.12+** (standard library only).
- **Node.js 24.16+ within the 24.x series, or 26.1+** (`>=24.16.0 <25 || >=26.1.0`, matching OpenClaw's engine range). Node 22 and 25 are unsupported. The installer resolves mise/asdf shims and the installed npm package.
- **OpenClaw 2026.10.2 or later**, with the Gateway running and its Telegram channel set up with your bot. CI tests 2026.10.2 and the latest release every week; see [Known limitations](#known-limitations).
- Optional, per feature:
  - `gog` (Google Workspace CLI) for Gmail and Calendar (OAuth per account, file keyring).
  - **Tailscale** for dashboard access (the dashboard only serves your Tailscale identity).
  - **Caddy** only to serve the dashboard on a custom domain (`deploy/Caddyfile`).
  - `wacli` for WhatsApp; a Maton connection for Slack (allowed to post as you, to send Slack messages); a Salesflare API key.
  - The `gh` CLI logged in as you for GitHub; a restricted read-only key (or a Maton connection) for Stripe.
  - For LinkedIn and X: your accounts signed in to the OpenClaw browser (profile `openclaw`).
  - The OpenClaw Android app paired as a node for phone notifications.

## Install

```bash
./install.sh                      # interactive install, or upgrade
./install.sh --non-interactive    # same, answers from environment variables (below)
./install.sh --no-start           # write files/link plugin; do not start service or reload plugin
./install.sh --preview            # existing install: one review of the last 7 days, shown only (assistant preview)
./install.sh --activate           # existing install: enable AI reviews and Telegram delivery
./install.sh --skip-plugin        # leave the OpenClaw plugin alone
./install.sh --uninstall          # remove service, plugin and CLI link; keep your data
./install.sh --uninstall --purge  # also delete ~/.local/share/personal-assistant
```

What it does, in order:

1. **Prerequisites:** Python, Node and the `openclaw` CLI are required, with fix hints. `gog`, `tailscale`, `caddy` and `wacli` are optional per feature. `systemctl` is required to start the managed service; use `--no-start` for manual setup.
2. **Config:** if `~/.local/share/personal-assistant/config.json` doesn't exist, a short wizard (your name, Telegram chat ID, Tailscale login, dashboard host, time zone, which sources) builds it from `config.example.json`. An existing config is kept as is; only missing top-level keys get their defaults, and install paths (`app_dir`, `openclaw`, `node`, `openclaw_package_dir`) are refreshed.
3. **Secrets:** creates `api-token`, `owner-token` and the Web Push key `vapid.json` when absent, mode 600 in a 700 directory. Existing secrets are never replaced.
4. **CLI:** links `~/.local/bin/assistant` to `cli.py`.
5. **Plugin:** `openclaw plugins install --link --force --accept-capabilities openclaw-plugin/`, `openclaw plugins enable personal-assistant-bridge`, then a reload. Running the installer accepts the local bridge's OpenClaw capability consent: it registers the assistant tool, owner Telegram commands and event hooks described in the security model. The plugin reads `AGENTS.md` and `PREPARATION.md` from `app_dir` (this checkout), so edits there apply to the next turn.
6. **Service:** writes `~/.config/systemd/user/personal-assistant.service` with this checkout's paths, then `daemon-reload`, `enable` and `restart`.
7. **Health:** waits for `http://127.0.0.1:<port>/health` and prints the next steps. Plugin install/enable/reload failures, systemd failures and an unanswered health check return a nonzero exit code; rerun after resolving the reported step.

`--non-interactive` reads: `ASSISTANT_OWNER_NAME`, `ASSISTANT_TELEGRAM_OWNER` (required), `ASSISTANT_TAILSCALE_OWNER` (required), `ASSISTANT_DASHBOARD_HOST` (required), `ASSISTANT_TIMEZONE`, `ASSISTANT_CONNECTORS` (comma list of `gmail,calendar,slack,crm,android,whatsapp,gateway`; default `gmail,calendar,gateway`), `ASSISTANT_EMAIL`, `ASSISTANT_GOG_CLIENT`, `ASSISTANT_BOUNDARY`, `ASSISTANT_ANDROID_NODE_ID`, `ASSISTANT_WHATSAPP_STORE`. Tailscale login and host default to what `tailscale status` reports.

**What you still do by hand** (the installer prints these):

- **Find your Telegram chat ID**: message your OpenClaw bot and read the sender ID from `openclaw logs`, or ask a bot such as @userinfobot.
- **Connect Google**: `gog auth add <email> --client <client>`, and put the gog file-keyring password in the `gog-password` credential file (mode 600). Read-only scopes are enough; sending approved emails also needs `gmail.send`, thread snoozes `gmail.modify`.
- **Other sources**: fill the IDs in `config.json` and put their API keys in the credential files it names (see [Sources](#sources)).
- **Dashboard**: `tailscale serve --bg --https=443 --set-path /assistant http://127.0.0.1:8787`, then open `https://<dashboard_host>/assistant/`. For a custom domain use `deploy/Caddyfile` and `deploy/caddy-machine.service`.
- **Tell the agent about you**: create `USER.md` in your OpenClaw workspace (see below).
- **Preview, then activate**: check `assistant sources` and resolve source errors. `./install.sh --preview` (or `assistant preview`) runs one regular review of the last 7 days and prints its cards, with progress while the agent works (usually a few minutes). The cards are real and stay on the dashboard, but they are never sent to Telegram, not even after activation, and nothing is prepared ahead; reviews and delivery stay off. Then read the security model and run `./install.sh --activate`. To start with one source, set `ASSISTANT_CONNECTORS=gateway ./install.sh` (Gateway failures only), or choose just `gmail` in the wizard. Add other accounts from `config.example.json` when ready.
- Restart after config edits: `systemctl --user restart personal-assistant.service`.

### Your own instructions: USER.md and overlays

`AGENTS.md` (reviews) and `PREPARATION.md` (Do it runs) are neutral templates about *how* the assistant works. Put *who you are* in `USER.md` in your OpenClaw workspace: your name, companies and their accounts, important people, tone of voice for emails, which tools you use and standing preferences. Review and preparation turns get it in their context. If you want different house rules, edit your checkout's `AGENTS.md`/`PREPARATION.md` (keep `PREPARATION.md` under 20,000 characters; the plugin truncates beyond that, and a test checks it).

## Configuration

`config.json` is plain JSON; paths should be absolute (the installer expands `~`). Restart the service after changes. Top-level keys:

| Key | Default | Meaning |
|---|---|---|
| `owner_name` | — | Your name; preparations sign emails with your first name |
| `timezone` | `Europe/Amsterdam` | IANA zone for quiet hours, digests, snoozes and weekend rules |
| `telegram_owner` | — (required) | Your Telegram chat ID; the only chat that gets cards and whose taps count |
| `tailscale_owner` | — (required) | The Tailscale login allowed to open the dashboard |
| `dashboard_host` | — (required) | Host name the dashboard answers on (with `:port` if not 443) |
| `dashboard_path` | `/` | Path the dashboard is served under on `dashboard_host` (`/assistant/` with Tailscale Serve `--set-path`) |
| `dashboard_aliases`, `dashboard_alias_path` | `[]`, `/assistant/` | Extra host names and their path |
| `port` | `8787` | Local API and dashboard port on 127.0.0.1 |
| `database` | — | SQLite file; tokens and keys sit next to it |
| `credentials` | — | Name → file path of each secret: `api-token`, `owner-token`, `gog-password`, and one per source credential. systemd `LoadCredential` with the same names takes precedence |
| `app_dir` | `~/.openclaw/workspace/tools/personal-assistant` | This checkout; the plugin reads `AGENTS.md`/`PREPARATION.md` here and protects it from edits |
| `openclaw`, `node` | `openclaw` / next to it | CLI paths for the Node helpers |
| `openclaw_package_dir` | `<node prefix>/lib/node_modules/openclaw` | Installed OpenClaw package (Gateway SDK) |
| `openclaw_state`, `agent_db` | OpenClaw defaults | OpenClaw state and agent transcript databases (read-only: Telegram receipts, progress steps) |
| `gog`, `wacli` | `gog`, `wacli` | Binaries for Google and WhatsApp |
| `heartbeat_job_id` | `""` | Optional OpenClaw cron job ID of your main heartbeat, for its health line |
| `browser_lease_script` | `integrations/browser-lease.py` | Lease helper Do it runs use for the shared browser |
| `delivery_enabled` | `false` | Send cards to Telegram (leave off while you test) |
| `delivery_batch_size` | `5` | Cards per delivery pass |
| `digest_at` | `08:30` | Morning digest time |
| `bootstrap_days` | `7` | History collected on first import |
| `poll_seconds` | `300` | Default source poll interval (per account `poll_seconds` overrides) |
| `http_timeout` | `15` | Dashboard request timeout |
| `slack_thread_retention_days` | `30` | How long known Slack threads are rechecked |
| `preparation_concurrency` | `3` | Do it runs at once |
| `prompt_receipts` | `<data>/prompt-receipts` | Telegram Change-prompt receipts |
| `attachment_dir`, `attachment_sources` | `<data>/attachments`; workspace `data/assistant-attachments`, `memory/assistant` | Where email attachments are stored, and the only folders they may come from |
| `excluded_email_subjects` | calendar "Accepted:" replies | Regexes; matching email subjects are never stored |
| `booking_links` | `{}` | `{"purpose": "url"}`; used when a draft offers to meet |
| `review_worker` | `enabled: false` | `enabled`, `min_signals` (40), `max_wait_minutes` (60), `gap_seconds` (600), `timeout_seconds` (1500), `outage_grace_seconds` (600), `retry_seconds` (60), `max_retry_seconds` (1800) |
| `weekly_recap` | `enabled: false` | `enabled`, `weekday` (1–7, default 5), `at` (`16:00`) |
| `embeddings` | `enabled: false` | Optional [related signals](#related-signals-optional-local-embeddings): `url`, `model`, `dims` (256), `floor` (0.6), `query_floor` (0.4) |
| `accounts` | `[]` | Sources, below |

Every account has `id`, `service`, `transport` and `boundary`, plus optional `scope` (`work`/`personal`, overrides the label the boundary implies), `poll_seconds` and `exclude` (noise rules, below). `config.example.json` has one complete example per built-in source; each plugin in `connectors/` (GitHub, Stripe, X, LinkedIn, RSS) shows its own at the top of its file.

**Noise rules (`exclude`).** A list of rules; an item matching all conditions of any rule is never stored, and stored copies are removed. Conditions: `channels` (Slack channel names), `bots_only`, `packages` (Android package names), `chats` (WhatsApp chat IDs), `senders` (email address substrings), `routine_recurring` (unchanged instances of recurring calendar events), `titles` (exact titles) and `text` (regexes over title and body).

## Sources

Collectors read existing source content. The optional Salesflare `join_pipelines` setting also adds your own user to matching CRM accounts; it is empty by default. Each collector runs in its own thread with its own checkpoint, so a slow or failing source never blocks the others. Absence from a later read is never treated as deletion.

| `service` | Needs | Account keys |
|---|---|---|
| `gmail` | `gog` OAuth for the account | `email`, `client` (gog OAuth client), `excluded_terms` (text that drops a message, e.g. a warmup tag) |
| `calendar` | `gog` OAuth | `email`, `client`, `calendar_id`, `excluded_title_terms` |
| `slack` | A [Maton](https://maton.ai) Slack connection | `credential` + `credential_variable` (file with `MATON_API_KEY=`), `connection_id`, `team_id`, `user_id`, `email`, `slack_host`, `fair_incremental` |
| `salesflare` | Salesflare API key | `api_base`, `app_base`, `credential`, `credential_variable`, `user_id`, `team_id`, `email`, `owner_emails`, `join_pipelines`, `accounts_per_batch`, `meeting_window_days`, `timeout_seconds` |
| `android` | OpenClaw Android app paired as a node | `transport: openclaw-feed`, `node_id`, `reconcile_seconds`, `direct_fallback`, `fallback_seconds` |
| `whatsapp` | `wacli` with a synced local store | `store`, `identity_sha256` (sha256 of the linked JID; the collector refuses a different account) |
| `gateway` | journald | `unit` (default `openclaw-gateway.service`), `bootstrap_hours` |
| `github` (plugin) | `gh` CLI logged in as you | `login`, `repos` (optional filter), `ci_repos` + `ci_branch` (failed runs) |
| `stripe` (plugin) | A restricted read-only Stripe key, or a Maton Stripe connection | `transport` (`api` or `maton`), `credential` (+ `credential_variable`, `connection_id` for Maton), `stripe_account`, `large_subscription_monthly` (500) |
| `linkedin` (plugin) | LinkedIn signed in to the OpenClaw browser | `active_hours` (`[7, 22]`, local time); config `linkedin_inbox` for the reader path |
| `x` (plugin) | X signed in to the OpenClaw browser | `handle`, `primary` (the account the browser is normally on), `active_hours` (`[7, 23]`), `long_posts` (X Premium: replies up to 25000 characters); config `x_inbox` for the reader path |
| `rss` (plugin) | — | `url` |

Plugin accounts aren't set up by the installer: add them to `accounts` by hand, using the keys above (each connector's header has a full example), then restart the service.

- **Gmail:** follows Gmail history after a seven-day import; replays the retained window when history expires. Replies keep their new text whole and only 3,000 characters of quoted history. Sending approved drafts and snoozing threads use separate workers and only scopes already granted.
- **Calendar:** seven past and thirty future days, recurring events expanded, incremental sync token with a daily full refresh. A missing event is "unavailable", not "cancelled". Context for email and preparation, not a reminder feed.
- **Slack:** conversations you already belong to; never joins channels or opens DMs. Per-conversation cursors, overlap rereads for edits, deferred retries for failed thread pages, and provider `Retry-After` honoured across restarts. Polling a large workspace can take hours.
- **Salesflare CRM:** only colleagues' emails that don't involve you (yours come from Gmail) and meetings with notes. With `join_pipelines`, it adds your own user to accounts linked to opportunities in those pipelines (a POST, never another user).
- **Android:** the plugin peeks OpenClaw's event queue for notifications from your phone node, with periodic direct reads to catch gaps. Repeated updates merge into one episode; one-time codes are omitted. Not a complete history.
- **WhatsApp:** reads your existing `wacli` store (`auth status`, `messages list` only), rereading the last seven days.
- **OpenClaw Gateway:** failed agent runs on Telegram, refused hand-offs and failed Telegram sends, grouped per half hour with secrets redacted.
- **GitHub:** notifications on threads you take part in (review requests, mentions, assignments, comments, threads you opened), each with the issue or PR and its latest comment. GET requests only; nothing is marked read. Each run checks that `gh` is logged in as `login`. Optional failed workflow runs per repository.
- **Stripe:** only events worth a look are stored: failed payments, disputes, ended subscriptions, scheduled cancellations, past due or unpaid, and new subscriptions of at least `large_subscription_monthly` a month. GET on the account, events and customers only; it never refunds, retries or changes anything. Each run checks the key belongs to `stripe_account`. For a direct key, create a restricted key with read access to Events, Customers and Account.
- **LinkedIn:** inbox conversations and messages (`connectors/linkedin.py` with `integrations/linkedin_inbox.py`), read through LinkedIn's own messaging API from a tab parked on `linkedin.com/robots.txt`, so nothing is marked read. Hourly in waking hours only (`poll_seconds`, `active_hours`), to keep automated LinkedIn traffic low. Pages follow LinkedIn's own cursors and commit with the messages; an unfinished scan continues after 30 seconds and keeps its lower bound through outages, however long; only a completed scan advances it. Spam is skipped. A busy browser retries in 10 minutes, signed out hourly. LinkedIn's query IDs are pinned; `linkedin_inbox.py discover` refreshes them when LinkedIn changes its client. No API key needed.
- **X:** replies, mentions and the DM inbox for each X account signed in to the OpenClaw browser (`connectors/x.py` with `integrations/x_inbox.py`), read through the owner's own signed-in x.com session (the same requests x.com makes, not X's developer API) from a background tab, so nothing is marked seen. Several accounts can share the browser through X's account switcher: x-inbox switches to the one it reads and always back to `primary`, one run at a time, and only while no other job holds the browser lease. Poll it hourly or less often (`poll_seconds`, default waking hours 07:00–23:00 via `active_hours`): X is sensitive to frequent automated reads. Encrypted X Chat isn't read. No API key needed. Replies and DMs can be sent too, only after your approval (X Send, below).
- **RSS/Atom:** the example plugin; see [Adding a source](docs/adding-a-source.md).

## Reviews

The service starts reviews itself (`review_worker.enabled`). When at least `min_signals` wait, or the oldest has waited `max_wait_minutes`, it starts one isolated turn of your main agent, `agent:main:assistant-review-signals-<run>`, with message delivery off. The plugin adds `USER.md` and `AGENTS.md` to that turn. The agent claims up to 120 signals, reads them through the `assistant_signals` tool, checks decisions, earlier suggestions and related evidence, saves suggestions with `when`/`due` timing, and finishes the analysis. One run at a time with a gap between runs; failed runs back off. Unfinished claims transfer safely to the next run, and the service never restarts the Gateway.

**Things that slipped.** Each review prompt also gets up to five "Possibly slipped" lines of each kind: your own sent emails whose new text asks something (a ? in a link doesn't count) and whose thread has had nothing since, 4-21 days later, but only in threads the other person started or to people who had written to you before you started the thread, so your outreach never shows up; and questions addressed directly to you (at most two To recipients, not Gmail's Updates/Promotions/Social/Forums categories, from someone you have emailed before) with no reply from you in the thread after 2-14 days. Three or more emails sent the same day with the same subject pattern count as outreach too and are left out. Anything already on a card is skipped. The `slipped` table records which review got each one; it's offered again only if that review didn't save within six hours. The review checks for an answer elsewhere before making a card.

Your decisions and written reasons are summarised into every review prompt, and lasting facts you state are saved (`remember_fact`) and listed, removable, on the dashboard. Style notes that hold beyond one card (signatures, formatting, tone) become **draft rules** that every later draft follows. Each Do it result says whether it's finished or what it needs from you, and the run sorts your Change notes (style, missing fact, keep going, new ask). The Feedback tab shows the split, the hit rate per day or week, and markers where the rules changed.

**Slack messages and hand-offs.** A Do it result can carry a Slack direct message from your own Slack account (the Maton Slack connection, as your user) to one person, named by Slack `user_id` or work email, with their name. `SlackTransport` in `next_actions.py` is separate from the read-only Slack source and can only look the person up (`users.info`, `users.lookupByEmail`), open the DM, post one message and read that DM back. Before sending it checks that the Slack name matches and that it's an active person, not a bot. An unclear outcome stays uncertain and is checked by finding exactly one message from you with that text in the DM; it's never resent. `delegate:true` (on Slack, or an email to one address) hands the task over: after the send the card is snoozed for `wait_days` (default 3; a weekday at 09:00) with a watch on that person. When they reply (in that DM, or in that email thread or to that email), the card closes itself and their reply is reviewed as an answer to the card; without a reply it comes back saying so. Sending needs a Maton Slack connection that is allowed to post as you.

**LinkedIn messages** use the same approval path. The `linkedin` action either replies in a conversation from the task's own LinkedIn signals (`conversation_id`), or writes to a profile slug you gave (`profile` plus `to_name`). Preflight resolves the recipient without sending, and a profile whose name doesn't match `to_name` blocks the send. `LinkedInTransport` runs `linkedin_inbox.py send --yes` with the job ID as token; the reader logs its intent before the one write (fsynced, token admission locked) and never reuses a token. An unclear outcome stays uncertain and is checked through `linkedin_inbox.py verify` by exact message identity, not by matching text or time; missing or damaged send evidence stays uncertain and is never resent. A busy browser just waits.

**X replies and DMs** use the same approval path, through the same signed-in x.com session. The `x` action either replies to a post from the task's own `x.mention` signals (`reply_to`, its tweet ID) or answers in a DM conversation from its `x.dm` signals (`conversation_id`), from the account the signal came in on: plain text, no media, at most 280 characters for a reply (links count 23, as on X; 25000 with `long_posts`) and 10000 for a DM. Send shows the account, the post replied to or the conversation, and the text. Preflight resolves the post or conversation without writing. `XTransport` then runs `x_inbox.py send --yes` with the job ID as token. A reply goes through X's own composer (`x.com/intent/post`): the tab first checks the composer holds exactly the approved text for the approved post, then clicks Reply once, and blocks every other post request. A DM goes to the DM inbox API (`dm/new2`) with a request ID derived from the token. The reader logs its intent before the one write (fsynced, in `~/.local/share/x-inbox/sends.jsonl`) and never sends a token that is already in the log; it switches accounts only under its run lock and always switches back. An unclear outcome stays uncertain and is checked through `x_inbox.py verify` (the returned post ID, else that account's reply to the post with the same text; for a DM the request ID), never resent. Encrypted X Chat conversations are refused. By hand, `x_inbox.py send --handle <h> --primary <p> --reply-to <id> --text '…'` only previews; `--check` also fills X's composer without posting; only `--yes` sends.

## Telegram and the dashboard

**Telegram** uses OpenClaw's existing bot and message route, never a second poller.
- The outbox records intent before sending. A send with an unknown outcome is held as `uncertain` and reconciled against the chat; it is never resent blindly.
- Quiet hours are 23:00–08:00 local time. Non-urgent work cards wait for Monday on weekends. Several cards may go out together; only the first buzzes.
- Buttons: **Do it**, **Change**, **Snooze** (2 hours, tomorrow, 3 days, 1 week, pick a date), **Dismiss** (with a reason), **Mark done**, **Undo** (five minutes). They edit the existing card instead of sending new messages; a finished Do it sends one short Ready notice. Each row holds up to three buttons that fit a phone's width, in order, so a Send shares its row with Mark done (`controls()` in `assistant.py` and `packRows` in the plugin follow the same rule).
- **Change** opens Telegram's native reply box. Only a reply to that exact prompt, within ten minutes, counts as instructions. `/assistant_feedback <id> <text>` is the fallback.
- The weekly recap (`weekly_recap`, off by default) arrives once per week, Fridays from 16:00 local time. Short sections with one point per line, so it reads at a glance on a phone: the hit rate first, then cards and decisions, what it noticed about how you acted, busy sources without results, and behind the scenes (reviews, backlog, token usage).

**The dashboard** shows the same cards grouped as Needs your decision, Ready for you, Preparing and Coming back later, with Work/Personal filters, deadlines, sources and their health, review runs, feedback and learned facts. It can be installed as a phone app and send push notifications mirroring Telegram. It is served on `127.0.0.1:<port>` and reached through Tailscale Serve or Caddy; see [Security model](#security-model).

## Do it

**Do it** queues a preparation: an isolated turn `agent:main:assistant-review-preparation-<job>` of your main agent with `USER.md`, `PREPARATION.md`, the card, your latest note and the full evidence. Up to `preparation_concurrency` run at once and share the OpenClaw browser through a lease. The agent does the work and saves a short result with `prepare` (longer output goes in `memory/assistant/` in the workspace), or records a plain-language blocker. Emails come back as a draft with a **Send** button; Send shows a full preview and only your **Send now** sends that exact revision, through the account's Gmail. A run interrupted by a crash or outage resumes with its history instead of redoing finished steps.

## Related signals (optional, local embeddings)

Off unless you set it up. Full-text search finds the same words; this finds the same *meaning*: a reply worded differently, the same topic in another source, an answer that already came in. Reviews and Do it runs get a `related` action that looks up signals by meaning, only within the accounts the call names (checked like every other read), and a Do it job message ends with a short "Possibly related" list from the task's own accounts when there are close matches. **Nothing leaves your machine:** signal text goes only to an embedding server you run on `127.0.0.1`, never to a model provider.

**Setup.**

1. Run an embedding server on loopback. Any OpenAI-compatible `/v1/embeddings` endpoint on this machine works (one that also lists its model in `/v1/models`); the tested one is llama.cpp's `llama-server --embedding` with an EmbeddingGemma GGUF, for example `embeddinggemma-300M-Q8_0.gguf` from [`ggml-org/embeddinggemma-300M-GGUF`](https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF) (about 330 MB). If OpenClaw's local memory search is on, it already has both: the model file it uses (`memory.search.local.modelPath` in `openclaw.json`, often in `~/.cache/qmd/models/`) and a bundled `llama-server` under `~/.openclaw/tools/llama.cpp/`. Reuse them rather than downloading again.
2. [`deploy/embeddinggemma.service`](deploy/embeddinggemma.service) is an example systemd user unit: bound to `127.0.0.1:8792`, `Nice=10`, low CPU and IO priority, `MemoryMax=2G`. Set the binary, model path and name in it (or in `~/.config/embeddinggemma.env`), then `systemctl --user enable --now embeddinggemma`. Check it with `curl -s 127.0.0.1:8792/v1/models`.
3. In `config.json`: `"embeddings": {"url": "http://127.0.0.1:8792", "model": "embeddinggemma-300m", "dims": 256}`. `model` must be the name the server reports (llama-server's `--alias`); the service refuses a server that serves another model. Optional `floor` (cosine for signal-to-signal matches, default 0.6) and `query_floor` (for a text query, default 0.4). The installer adds this block with `enabled: false`: set that to `true` (or drop it), then restart the service.
4. Embed what you already have: `python3 assistant.py embed-backfill` (oldest first, with progress; `--batch` sets events per call). It's safe while the service runs; the service itself embeds new and changed signals in the background, newest first.

**How it works.** `event_vectors` holds one normalized float32 vector per event version (Matryoshka-truncated to `dims`), deleted with its event; changing `model` or `dims` re-embeds. The embedded text is the title plus the first 2,000 characters of the body, without quoted earlier messages. `assistant_signals related` takes explicit `account_ids` and `event_ids` and/or a `query`, `limit` up to 20 and optional `exclude_event_ids`, and returns the `events` shape plus a `similarity`; the given events themselves are never returned. With a query, the meaning ranking is fused with full-text ranking. `events` with `semantic:true` and a `query` uses the same fused ranking. If the embedding server is down, the service logs `embeddings error=<class>` and retries after five minutes; queries still get keyword matches (`semantic:false`), and nothing else is affected.

**Costs.** No GPU needed. On CPU the server embeds a few signals per second; a first backfill of 5,000 signals takes about 30–40 minutes on a 4-core desktop CPU, and each new signal well under a second. The model takes about 0.3–0.5 GB of RAM while the server runs and no CPU when idle. Vectors are about 1 KB each at 256 dimensions (5 MB for 5,000 signals). Search is brute-force cosine in plain Python, about 0.1 s over 5,000 vectors.

**Off again:** remove `embeddings` (or set `enabled: false`) and restart; the `event_vectors` table can stay or be dropped.

## Security model

Read this before you turn on delivery.

- **Owner only.** Telegram cards go only to `telegram_owner`, and the plugin accepts taps and replies only from that private chat. The dashboard answers only requests that arrive through Tailscale Serve or Caddy on 127.0.0.1 with your `tailscale_owner` identity and the configured host; decisions also need a CSRF-protected same-origin POST. There is no public endpoint. `/health` exposes nothing private.
- **Two tokens.** The local API token (`api-token`, used by the CLI and the agent's tool) can read and save results but cannot record decisions or approve sends; owner actions need the plugin's second key (`owner-token`). The CLI refuses owner actions.
- **Reviews are read-only.** Review turns can only use `assistant_signals`, memory lookups and read-only file tools.
- **Do it gives the agent broad authority.** OpenClaw can't show approval prompts in background runs, so a Do it run can run commands, use the logged-in browser, edit files and act in your accounts, including payments, deletions and signing, as far as the card and your note ask. The plugin blocks only reading credentials and editing the assistant's own code, the OpenClaw config/extensions and systemd units. These are guardrails against mistakes, not a sandbox. Source messages are treated as untrusted evidence, but the agent reads them; prompt injection remains a risk you accept by using Do it. Use a model and accounts you trust with that.
- **Privacy.** Collected content stays in your SQLite database, but reviews and preparations send it to your agent's model provider. The optional related signals embed text only through the local server you configure. Logs contain account IDs, counts and redacted errors only. Source credentials never reach the model.

## Command line

`assistant` (linked by the installer; code in `cli.py`) talks to the running service with the local API token. Every command takes `--json`.

| Command | What it does |
|---|---|
| `assistant tasks` | Outstanding tasks; `--later`, `--done` or `--all` for the other tabs |
| `assistant show <id>` | One task (a unique ID prefix is enough) with decisions, feedback and full evidence |
| `assistant search "<text>"` | Full-text search over all sources; `--account <id>` narrows it |
| `assistant events --account <id> --ids 12,13` | Specific signals, or `--query` within explicit accounts |
| `assistant sources` | Connection health per source |
| `assistant reviews` | Review backlog and recent runs |
| `assistant feedback` | Recent decisions and written feedback |
| `assistant prepare <id> --file brief.md` | Save a preparation on an accepted task |
| `assistant preview` | Before activation: one review of the last 7 days; prints the cards, sends nothing |
| `assistant raw <action> '<json>'` | Any other non-owner service action |

## Operations

- **Service:** `systemctl --user status personal-assistant.service`; logs with `journalctl --user -u personal-assistant.service -n 50 --no-pager`. Enable lingering (`loginctl enable-linger $USER`) to start at boot.
- **Plugin:** after editing `openclaw-plugin/`, run `openclaw plugins reload personal-assistant-bridge --wait`; no Gateway restart needed.
- **One collector only:** don't run `assistant.py sync-once` while the service runs. Stop the service before manual syncs or migrations and resume from the existing checkpoints. Never reset a checkpoint to get past throttling.
- **Backups:** use SQLite's backup API (`sqlite3 events.sqlite ".backup copy.sqlite"`); never copy the live file without its WAL.
- **Rollback:** disable a source by removing its account from `config.json`; disable reviews with `review_worker.enabled: false`; stop delivery with `delivery_enabled: false`; disable the plugin with `openclaw plugins disable personal-assistant-bridge`. Keep the database.

## Adding a source

Sources can be added as a single-file plugin in `connectors/` without touching `assistant.py`. [docs/adding-a-source.md](docs/adding-a-source.md) walks through one, with [`connectors/rss.py`](connectors/rss.py) (RSS and Atom feeds) as a working example. The rules for any source:

1. Use an existing credential for the exact account, with only the read permission needed. Give the source its own account ID, boundary and checkpoint.
2. Add an adapter that returns the common event fields: `account_id`, a stable `source_id`, `kind`, `occurred_at`, `title`, `body`, `metadata` (with a source URL where possible) and `deleted`. No AI, guessing or writes in collectors.
3. Fetch incrementally and commit events with the next cursor atomically, so replays are idempotent.
4. Test identity checks, pagination, interrupted commits and expired cursors with fixtures, then do a real read and check its health on the dashboard.

## Known limitations

- **Change prompt and OpenClaw versions.** The native Telegram reply prompt (`openclaw-plugin/change-prompt.ts`) uses the Telegram extension API and a bridge for three client helpers. It resolves them from the installed build's export table, without a version pin or hardcoded content hashes, so OpenClaw updates don't break it. CI loads the real transport modules of 2026.10.2 and of the latest release. If a release removes or renames a helper, Change falls back to a plain prompt before sending (replies still work); an uncertain send never triggers that fallback. The Gateway SDK entry points used elsewhere (`plugin-sdk/gateway-runtime`, `system-event-runtime`, `plugin-entry`) are public but experimental; check them when upgrading OpenClaw.
- **Telegram only** for delivery; the dashboard needs Tailscale (or your own reverse proxy that sets the same identity headers).
- **Gmail and Calendar need `gog`**, Slack needs Maton; there are no built-in OAuth flows.
- **Not realtime.** Sources poll (five minutes by default; Slack can take hours on a large workspace), and reviews batch signals.
- **Android and WhatsApp are best effort**: notifications cleared during a gap are lost; WhatsApp depends on `wacli` staying synced.
- Single owner, single machine. Not a multi-user product.

## Tests

```bash
python3 scripts/test.py
```

The runner returns nonzero if any suite fails and isolates HOME/config from your live installation. The tests are offline: they use temporary databases, a temporary OpenClaw workspace (`test-env.mjs`) and stubs for the Gateway, `openclaw` and `systemctl`. `test_installer.py` runs the installer against a temporary `HOME`. `test_ux_dashboard.mjs` drives the dashboard in Chromium through the `playwright-core` that ships with OpenClaw and `/usr/bin/chromium`; point `PLAYWRIGHT_CORE` and `CHROMIUM_PATH` elsewhere if yours differ. The Node tests that load the plugin need the `openclaw` package (`OPENCLAW_PACKAGE_DIR`). `test_openclaw_compat.mjs` also loads the real SDK and installs the plugin into a disposable OpenClaw home, with no Gateway or messages.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and pull requests, and [SECURITY.md](SECURITY.md) for private vulnerability reports and publishing checks. CI runs every Python and Node suite on two supported toolchains and scans all reachable history for secrets. Personal account data belongs outside the checkout.

## License

MIT, see [LICENSE](LICENSE).
