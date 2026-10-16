# The Machine

A private, self-hosted assistant inbox for [OpenClaw](https://docs.openclaw.ai). It reads your email, calendar, chat and phone notifications, has your own OpenClaw agent look for what actually needs you, and sends a few short suggestions to Telegram. Tap **Do it** and the agent carries the task out.

**Early release:** intended for a single owner who already uses OpenClaw. Managed installation is Linux-only.

## Quick start

```bash
git clone https://github.com/JeroenCorthout/TheMachine.git ~/.openclaw/workspace/tools/personal-assistant
cd ~/.openclaw/workspace/tools/personal-assistant
./install.sh
```

Read [Requirements](#requirements) first. The installer checks prerequisites, asks for your owner identity and source choices, writes the config and secrets, installs the systemd user service and the OpenClaw plugin, and ends with a health check and the remaining manual steps (connecting Gmail, exposing the dashboard on your tailnet). Fresh installs collect configured sources but leave AI reviews and Telegram delivery **off**. Once you have connected the sources, checked `assistant sources` and read [Security model](#security-model), run `./install.sh --activate`. Run the installer again to upgrade; `./install.sh --uninstall` removes it again (see [Install](#install)).

Contents: [What it does](#what-it-does) · [Architecture](#architecture) · [Requirements](#requirements) · [Install](#install) · [Configuration](#configuration) · [Sources](#sources) · [Reviews](#reviews) · [Telegram and the dashboard](#telegram-and-the-dashboard) · [Do it](#do-it) · [Security model](#security-model) · [Command line](#command-line) · [Operations](#operations) · [Adding a source](#adding-a-source) · [Known limitations](#known-limitations) · [Tests](#tests)

## What it does

- **Collects** signals from Gmail, Google Calendar, Slack, the Salesflare CRM, Android notifications (through the OpenClaw node app), WhatsApp (through a local `wacli` store) and the OpenClaw Gateway's own failures. Collection is incremental and read-only by default; everything goes into one private SQLite database with full-text search.
- **Reviews** waiting signals in isolated turns of your existing OpenClaw main agent. The agent saves a suggestion only when something is worth your time: an unanswered request, a deadline, a problem it can fix.
- **Delivers** suggestions as Telegram cards through your existing OpenClaw bot, with a phone-friendly dashboard (installable as an app, with optional push notifications) on your tailnet.
- **Acts** when you tap **Do it**: the agent drafts the email (with a Send button), makes the fix, or does the research, and reports back on the same card. **Change** lets you reply with instructions; **Snooze**, **Dismiss** and **Mark done** work as you'd expect, and your choices steer later reviews.

The service itself contains no model. All interpretation happens in your OpenClaw agent, with your model provider: see [Bring your own model](docs/models.md) for choosing one, including local models.

## Architecture

```text
Gmail · Calendar · Slack · CRM · Android notifications · WhatsApp · OpenClaw Gateway
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
| `next_actions.py` | Approved email sends (Gmail via `gog`) |
| `preparation_state.py`, `rollout_health.py` | Preparation bookkeeping and health checks |
| `openclaw-plugin/` | `personal-assistant-bridge`: the `assistant_signals` tool, Telegram buttons and replies, Android feed capture, review/preparation guards |
| `*.mjs` | Small Node helpers that call the OpenClaw Gateway (`openclaw-root.mjs` finds the installed package) |
| `integrations/browser-lease.py` | Shares the OpenClaw browser between concurrent Do it runs |
| `AGENTS.md`, `PREPARATION.md` | Instructions the plugin gives the agent for reviews and for Do it runs (templates; see below) |
| `install.sh`, `installer.py` | Install, upgrade and uninstall |
| `config.example.json` | Every configuration key with example values |
| `deploy/` | Example systemd units and Caddyfile |
| `test_*.py`, `test_*.mjs` | Offline tests |

Python's standard library and SQLite are enough: no database server, queue, web framework or extra scheduler. Runtime data lives in `~/.local/share/personal-assistant/`: `config.json`, `events.sqlite`, tokens and receipts.

**Data model.** The main tables are `events` (with an `event_search` full-text index), `checkpoints`, `sync_activity`, `analyses` (reviews), `review_runs`, `suggestions`, `decisions`, `deliveries` (the Telegram outbox), `preparations` and `action_jobs` (approved sends). Provider IDs deduplicate events per account; a changed event gets a new version and becomes reviewable again. Each source page commits its events and next cursor in one transaction, so a crash replays the page instead of losing or duplicating it.

**Boundaries.** Every account has an explicit ID and `boundary` (`work:Acme`, `personal:phone`, `owner:system`…). Every read names its accounts explicitly; app names and chat labels never decide which company an item belongs to.

## Requirements

- Linux with systemd user services (macOS works for development and tests, not as a managed service).
- **Python 3.12+** (standard library only).
- **Node.js 24.16+ within the 24.x series, or 26.1+** (`>=24.16.0 <25 || >=26.1.0`, matching the pinned OpenClaw engine range). Node 22 and 25 are unsupported. The installer resolves mise/asdf shims and the installed npm package.
- **OpenClaw 2026.10.2**, with the Gateway running and its Telegram channel set up with your bot. Other versions are untested; see [Known limitations](#known-limitations).
- Optional, per feature:
  - `gog` (Google Workspace CLI) for Gmail and Calendar (OAuth per account, file keyring).
  - **Tailscale** for dashboard access (the dashboard only serves your Tailscale identity).
  - **Caddy** only to serve the dashboard on a custom domain (`deploy/Caddyfile`).
  - `wacli` for WhatsApp; a Maton connection for Slack; a Salesflare API key.
  - The OpenClaw Android app paired as a node for phone notifications.

## Install

```bash
./install.sh                      # interactive install, or upgrade
./install.sh --non-interactive    # same, answers from environment variables (below)
./install.sh --no-start           # write files/link plugin; do not start service or reload plugin
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
- **Activate after setup**: check `assistant sources`, resolve source errors and read the security model; then run `./install.sh --activate`. To start with one source, set `ASSISTANT_CONNECTORS=gateway ./install.sh` (Gateway failures only), or choose just `gmail` in the wizard. Add other accounts from `config.example.json` when ready.
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
| `accounts` | `[]` | Sources, below |

Every account has `id`, `service`, `transport` and `boundary`, plus optional `scope` (`work`/`personal`, overrides the label the boundary implies), `poll_seconds` and `exclude` (noise rules, below). `config.example.json` has one complete example per source.

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

- **Gmail:** follows Gmail history after a seven-day import; replays the retained window when history expires. Replies keep their new text whole and only 3,000 characters of quoted history. Sending approved drafts and snoozing threads use separate workers and only scopes already granted.
- **Calendar:** seven past and thirty future days, recurring events expanded, incremental sync token with a daily full refresh. A missing event is "unavailable", not "cancelled". Context for email and preparation, not a reminder feed.
- **Slack:** conversations you already belong to; never joins channels or opens DMs. Per-conversation cursors, overlap rereads for edits, deferred retries for failed thread pages, and provider `Retry-After` honoured across restarts. Polling a large workspace can take hours.
- **Salesflare CRM:** only colleagues' emails that don't involve you (yours come from Gmail) and meetings with notes. With `join_pipelines`, it adds your own user to accounts linked to opportunities in those pipelines (a POST, never another user).
- **Android:** the plugin peeks OpenClaw's event queue for notifications from your phone node, with periodic direct reads to catch gaps. Repeated updates merge into one episode; one-time codes are omitted. Not a complete history.
- **WhatsApp:** reads your existing `wacli` store (`auth status`, `messages list` only), rereading the last seven days.
- **OpenClaw Gateway:** failed agent runs on Telegram, refused hand-offs and failed Telegram sends, grouped per half hour with secrets redacted.

## Reviews

The service starts reviews itself (`review_worker.enabled`). When at least `min_signals` wait, or the oldest has waited `max_wait_minutes`, it starts one isolated turn of your main agent, `agent:main:assistant-review-signals-<run>`, with message delivery off. The plugin adds `USER.md` and `AGENTS.md` to that turn. The agent claims up to 120 signals, reads them through the `assistant_signals` tool, checks decisions, earlier suggestions and related evidence, saves suggestions with `when`/`due` timing, and finishes the analysis. One run at a time with a gap between runs; failed runs back off. Unfinished claims transfer safely to the next run, and the service never restarts the Gateway.

Your decisions and written reasons are summarised into every review prompt, and lasting facts you state are saved (`remember_fact`) and listed, removable, on the dashboard.

## Telegram and the dashboard

**Telegram** uses OpenClaw's existing bot and message route, never a second poller.
- The outbox records intent before sending. A send with an unknown outcome is held as `uncertain` and reconciled against the chat; it is never resent blindly.
- Quiet hours are 23:00–08:00 local time. Non-urgent work cards wait for Monday on weekends. Several cards may go out together; only the first buzzes.
- Buttons: **Do it**, **Change**, **Snooze** (2 hours, tomorrow, 3 days, 1 week, pick a date), **Dismiss** (with a reason), **Mark done**, **Undo** (five minutes). They edit the existing card instead of sending new messages; a finished Do it sends one short Ready notice.
- **Change** opens Telegram's native reply box. Only a reply to that exact prompt, within ten minutes, counts as instructions. `/assistant_feedback <id> <text>` is the fallback.

**The dashboard** shows the same cards grouped as Needs your decision, Ready for you, Preparing and Coming back later, with Work/Personal filters, deadlines, sources and their health, review runs, feedback and learned facts. It can be installed as a phone app and send push notifications mirroring Telegram. It is served on `127.0.0.1:<port>` and reached through Tailscale Serve or Caddy; see [Security model](#security-model).

## Do it

**Do it** queues a preparation: an isolated turn `agent:main:assistant-review-preparation-<job>` of your main agent with `USER.md`, `PREPARATION.md`, the card, your latest note and the full evidence. Up to `preparation_concurrency` run at once and share the OpenClaw browser through a lease. The agent does the work and saves a short result with `prepare` (longer output goes in `memory/assistant/` in the workspace), or records a plain-language blocker. Emails come back as a draft with a **Send** button; Send shows a full preview and only your **Send now** sends that exact revision, through the account's Gmail. A run interrupted by a crash or outage resumes with its history instead of redoing finished steps.

## Security model

Read this before you turn on delivery.

- **Owner only.** Telegram cards go only to `telegram_owner`, and the plugin accepts taps and replies only from that private chat. The dashboard answers only requests that arrive through Tailscale Serve or Caddy on 127.0.0.1 with your `tailscale_owner` identity and the configured host; decisions also need a CSRF-protected same-origin POST. There is no public endpoint. `/health` exposes nothing private.
- **Two tokens.** The local API token (`api-token`, used by the CLI and the agent's tool) can read and save results but cannot record decisions or approve sends; owner actions need the plugin's second key (`owner-token`). The CLI refuses owner actions.
- **Reviews are read-only.** Review turns can only use `assistant_signals`, memory lookups and read-only file tools.
- **Do it gives the agent broad authority.** OpenClaw can't show approval prompts in background runs, so a Do it run can run commands, use the logged-in browser, edit files and act in your accounts, including payments, deletions and signing, as far as the card and your note ask. The plugin blocks only reading credentials and editing the assistant's own code, the OpenClaw config/extensions and systemd units. These are guardrails against mistakes, not a sandbox. Source messages are treated as untrusted evidence, but the agent reads them; prompt injection remains a risk you accept by using Do it. Use a model and accounts you trust with that.
- **Privacy.** Collected content stays in your SQLite database, but reviews and preparations send it to your agent's model provider. Logs contain account IDs, counts and redacted errors only. Source credentials never reach the model.

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

- **Change prompt and OpenClaw versions.** The native Telegram reply prompt (`openclaw-plugin/change-prompt.ts`) uses the Telegram extension API and a version-checked bridge for three client helpers. It resolves the pinned build's exports without hardcoded content hashes. CI loads the real 2026.10.2 transport modules. Other versions fall back to a plain prompt before sending (replies still work); an uncertain send never triggers that fallback. The Gateway SDK entry points used elsewhere (`plugin-sdk/gateway-runtime`, `system-event-runtime`, `plugin-entry`) are public but experimental; check them when upgrading OpenClaw.
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
