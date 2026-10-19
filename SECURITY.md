# Security and privacy

## Supported release

Security fixes target the latest `main` revision and its pinned OpenClaw 2026.10.5 dependency.
Older snapshots and other OpenClaw builds are not covered by the compatibility tests.
This is an early release for one owner on a trusted machine, not a public multi-user service.

## Reporting a vulnerability

Use this repository's GitHub **Security → Report a vulnerability** form for a private report.
If that form is unavailable, contact the maintainer through the GitHub profile first and
arrange a private channel. Do not put exploit details or private data in a public issue.
Include affected revisions, a synthetic reproduction and the expected security boundary.
Never include actual tokens, messages or a production database.

## Deployment boundaries

- Keep the service bound to `127.0.0.1`. The dashboard is intended for Tailscale Serve or
  the provided identity-checking reverse proxy. Do not expose the local API directly.
- Keep config, SQLite data, attachments and credentials outside the Git checkout.
  The installer uses a private data directory and mode 600 secret files.
- Reviews and preparations send relevant private evidence to the configured model provider.
  Choose that provider and its data settings accordingly.
- **Do it grants broad account and machine access for the accepted task.** The plugin's
  guards are not a sandbox. Source messages are untrusted and may contain prompt injection.
  Read the README's security model before activating reviews or delivery.
- Emails, Slack and LinkedIn messages, and X replies and messages are sent only after the owner
  approves that exact draft (Send now), and a send with an unclear outcome is checked, never
  repeated. Connectors only read.
- Fresh installs collect configured sources with reviews and delivery disabled.
  `./install.sh --activate` explicitly enables both after setup.
- Backups contain private evidence and may include secrets. Protect them like the live data.

## Publishing a fork

Inspect every tracked file and all reachable history before making a fork public. Scan
for secrets, check commit author/committer emails, and inspect screenshots and sample data.
Use GitHub's noreply address if you do not want to publish your email address.
`.gitignore` and CI reduce accidental leaks; they cannot detect every kind of personal information.

If a real secret was committed, revoke it first. Removing the file in a later commit does
not remove it from history or copies that were already fetched. Then clean the affected
history and follow GitHub's guidance on removing sensitive repository data.
