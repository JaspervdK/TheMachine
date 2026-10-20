# Configuration

[Documentation index](../README.md). Current reference; dated repair evidence lives in [History](history.md).

## Configuration

`config.json` is plain JSON; paths should be absolute (the installer expands `~`). Restart the service after changes. Top-level keys:

| Key | Default | Meaning |
|---|---|---|
| `owner_name` | — | Your name; preparations sign emails with your first name |
| `timezone` | `Europe/Amsterdam` (the installer sets this computer's) | IANA zone for your work hours, digests, snoozes and recaps (the dashboard's Work hours, or `assistant hours --timezone`) |
| `telegram_owner` | empty | Your Telegram chat ID; the only chat that gets cards and whose taps count. Empty: Telegram off, cards go to the dashboard app's notifications |
| `telegram_account` | `default` | The OpenClaw Telegram account (bot) that sends cards, when you run several; the installer sets it |
| `tailscale_owner` | — (required) | The Tailscale login allowed to open the dashboard |
| `dashboard_host` | — (required) | Host name the dashboard answers on, with `:port` if not 443 (the installer uses `<machine>.ts.net:8443`) |
| `dashboard_path` | `/` | Path the dashboard is served under on `dashboard_host` (`/assistant/` with Tailscale Serve `--set-path`) |
| `dashboard_aliases`, `dashboard_alias_path` | `[]`, `/assistant/` | Extra host names and their path |
| `port` | `8787` | Local API and dashboard port on 127.0.0.1 |
| `database` | — | SQLite file; tokens and keys sit next to it |
| `credentials` | — | Name → file path of each secret: `api-token`, `owner-token`, `gog-password`, `google-broker` (with a broker), and one per source credential. systemd `LoadCredential` with the same names takes precedence |
| `app_dir` | `~/.openclaw/workspace/tools/personal-assistant` | This checkout; the plugin reads `AGENTS.md`/`PREPARATION.md` here and protects it from edits |
| `fix_dir` | `~/.cache/the-machine/fixes` | Where Do it clones The Machine to fix it ([The Machine fixes itself](../README.md#the-machine-fixes-itself)) |
| `self_test`, `self_test_seconds`, `self_fix_push` | `scripts/test.py`, `1200`, `true` | The test command Merge now runs in the fix (a list), its time limit, and whether a merged fix is pushed |
| `self_fix_auto`, `self_fix_auto_push` | `true`, `false` | Small proven fixes to its own errors [merge by themselves](../README.md#the-machine-fixes-itself), with Undo merge for 24 hours; whether those are also pushed upstream |
| `openclaw`, `node` | `openclaw` / next to it | CLI paths for the Node helpers |
| `openclaw_package_dir` | `<node prefix>/lib/node_modules/openclaw` | Installed OpenClaw package (Gateway SDK) |
| `openclaw_state`, `agent_db` | OpenClaw defaults | OpenClaw state and agent transcript databases (read-only: Telegram receipts, progress steps) |
| `gog`, `wacli` | `gog`, `wacli` | Binaries for Google and WhatsApp |
| `google_broker` | — | `url` of a Google token broker, for accounts with `"transport": "broker"` (below) |
| `heartbeat_job_id` | `""` | Optional OpenClaw cron job ID of your main heartbeat, for its health line |
| `manage_url` | `""` | Optional https page where this install is set up (a hosted machine); the dashboard links to it from Settings (Account) and Add a connection |
| `needs_you_file` | unset | Optional JSON list of `{id, text, url, action}` notes from a hosting layer; the dashboard shows them under Needs you, with sources that are not fully set up and connections that need signing in again |
| `browser_url` | unset | Optional page that shows the assistant's browser (a hosted machine's viewer, or `./install.sh --browser-viewer`'s); a card waiting on the browser links to it |
| `browser_cdp_url` | `http://127.0.0.1:9222` | That browser's DevTools address on this machine; **Open the browser** brings the tab a task uses to the front through it (also when it opens a viewer) |
| `browser_vnc` | unset | Optional VNC server showing this computer's screen, as a unix socket path or a `127.0.0.1:port`; the dashboard then shows that screen live at `/browser/` (see [browser setup](sources.md)) |
| `browser_novnc` | `~/.local/share/machine-browser/web` | The noVNC files the dashboard serves for `browser_vnc` (`python3 browser_viewer.py web DIR [NOVNC]` builds them) |
| `browser_lease_script` | `integrations/browser-lease.py` | Lease helper Do it runs use for the shared browser |
| `delivery_enabled` | `false` | Send cards to Telegram (leave off while you test) |
| `delivery_batch_size` | `5` | Cards per delivery pass |
| `digest_at` | `08:30` | Morning digest time; also when work held over a day off goes out |
| `work_hours` | Mon–Fri, `08:00`–`23:00`, mornings `09:00` | [Work hours](../README.md#work-hours): `days` (1 = Monday … 7, or `"mon-fri"`), `start`/`end` (nothing is sent outside them; may wrap past midnight), `morning` (when Tomorrow, 3 days and 1 week snoozes and returning threads land) |
| `bootstrap_days` | `7` | History collected on first import |
| `poll_seconds` | `300` | Default source poll interval (per account `poll_seconds` overrides) |
| `http_timeout` | `15` | Dashboard request timeout |
| `slack_thread_retention_days` | `30` | How long known Slack threads are rechecked |
| `slack_read_seconds` | `180` | At most one Slack history or thread read this often (never under 65 s); see below |
| `slack_active_minutes` / `slack_recent_hours` / `slack_quiet_hours` | `60` / `6` / `24` | How long a Slack conversation rests after a read: someone wrote in it today / this week / longer ago |
| `slack_dm_check_minutes` | `10` | How often a direct message is checked for a new message (a cheap call; a history read follows only when there is one) |
| `preparation_concurrency` | `3` | Do it runs at once |
| `prepare_ahead`, `prepare_ahead_per_day` | `true`, `10` | [Prepared ahead](../README.md#prepared-ahead): cards you'd very likely say yes to arrive done up to their last step |
| `prompt_receipts` | `<data>/prompt-receipts` | Telegram Change-prompt receipts |
| `attachment_dir`, `attachment_sources` | `<data>/attachments`; workspace `data/assistant-attachments`, `memory/assistant` | Where email attachments are stored, and the only folders they may come from |
| `excluded_email_subjects` | calendar "Accepted:" replies | Regexes; matching email subjects are never stored |
| `booking_links` | `{}` | `{"purpose": "url"}`; used when a draft offers to meet |
| `review_worker` | `enabled: false` | `enabled`, `first_look` (true: the automatic first preview, below; false turns it off), `min_signals` (40), `max_wait_minutes` (60), `gap_seconds` (600), `timeout_seconds` (1500), `outage_grace_seconds` (600), `retry_seconds` (60), `max_retry_seconds` (1800) |
| `weekly_recap` | `enabled: false` | `enabled`, `weekday` (1–7, default 5), `at` (`16:00`) |
| `embeddings` | `enabled: false` | Optional [related signals](../README.md#related-signals-optional-local-embeddings): `url`, `model`, `dims` (256), `floor` (0.6), `query_floor` (0.4) |
| `enrichment` | off | Optional [organisation data](../README.md#organisation-data-optional-enrichment): `provider` or `url`, `headers`, `key_file`/`key_env`, `fields`, `per_day` (50) |
| `accounts` | `[]` | Sources, below |

Every account has `id`, `service`, `transport` and `boundary`, plus optional `scope` (`work`/`personal`, overrides the label the boundary implies), `poll_seconds` and `exclude` (noise rules, below). `config.example.json` has one complete example per built-in source; each plugin in `connectors/` (GitHub, Stripe, X, LinkedIn, RSS) shows its own at the top of its file.

**Noise rules (`exclude`).** A list of rules; an item matching all conditions of any rule is never stored, and stored copies are removed. Conditions: `channels` (Slack channel names), `bots_only`, `packages` (Android package names), `chats` (WhatsApp chat IDs), `senders` (email address substrings), `routine_recurring` (unchanged instances of recurring calendar events), `titles` (exact titles) and `text` (regexes over title and body).

**Built-in noise rules**, on top of yours (`"default_exclude": false` on an account turns them off):

- **Android:** the phone's own status notices ("… is displaying over other apps", "Charging this device via USB", USB/wireless debugging, serial console; storage and security warnings still come through). App notifications stay even if one account from that service is connected: the phone may also use personal mailboxes or other workspaces not collected here. When you connect a source that also has a phone app (Gmail, Slack, WhatsApp, …), setup asks once whether to skip that app's notifications; yes adds an `exclude` rule with its `packages` to the Android account, which you can also write yourself.
- **Calendar:** unchanged instances of recurring placeholders: Lunch, Block(ed), Busy, Focus (time), Deep work, Home, Office, Commute, Gym, Stand up / Standup / Daily standup, Happy birthday!. A moved or one-off "Lunch with Ann" still counts.
