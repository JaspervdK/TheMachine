# Getting started

From nothing to your first cards in about 15 minutes. Each step says how to check it worked.

## Before you start

- [ ] **OpenClaw** 2026.10.2 or later, with the Gateway running and its Telegram channel talking to your bot. Check: your bot answers you in Telegram.
- [ ] **Node.js** 24.16+ (24.x) or 26.1+, and **Python 3.12+**.
- [ ] **Tailscale** on this machine and your phone, logged in (`sudo tailscale up`), for the dashboard.
- [ ] Your own user account, not root, with a systemd user session (a normal login; over SSH that works too).
- [ ] Per source you want: `gog` for Gmail and Calendar, `wacli` for WhatsApp, the OpenClaw Android app paired as a node for phone notifications, a [Maton](https://maton.ai) Slack connection, a Salesflare API key, `gh` for GitHub. You can add any of them later.

## 1. Install

```bash
git clone https://github.com/JeroenCorthout/TheMachine.git ~/.openclaw/workspace/tools/personal-assistant
cd ~/.openclaw/workspace/tools/personal-assistant
./install.sh
```

The wizard fills in what it can find, so most questions only need Enter:

- **Telegram ID:** taken from OpenClaw (your owner entry or approved senders). If OpenClaw doesn't know you yet, the installer asks you to message your bot, reads your ID from the pairing request and offers to approve it.
- **Tailscale login and dashboard host:** from `tailscale status` (on a tagged machine, type the login you use on your phone).
- **Company:** from your email address; answer "personal" if these are private accounts.
- **Sources:** suggested from what is installed and linked here (gog, a linked WhatsApp, a paired Android phone).
- **Email:** from the Google accounts gog already knows.

## 2. Connect your sources

Right after the service starts, the installer walks through each source:

| Source | What happens |
|---|---|
| Gmail, Calendar | Picks your gog OAuth client (or helps you add one), stores a keyring password (typed or generated, mode 600) and signs you in with **read-only** Gmail and Calendar access. Calendar reuses the same sign-in. |
| Slack | Asks for your Maton API key (hidden) and connection ID, then finds your team, user and email itself. |
| Salesflare | Asks for an API key (hidden), then finds your user and team. |
| WhatsApp | Finds the wacli store, offers to link it (QR code) and records which account it is. |
| Android | Picks your paired Android phone from OpenClaw. For each source you connect that also has a phone app (Gmail, Slack, WhatsApp, …) it asks whether to skip that app's notifications; the default keeps them, since the phone may show other inboxes too. |
| Gateway log | Finds the Gateway's systemd unit. |
| Plugins | `github`, `stripe`, `x`, `linkedin`, `rss`: not part of the installer; `assistant connect github` (and so on) asks for the few fields each needs. |

Every source is read once before it is saved, so a wrong key or account shows up immediately, with what to do about it. Afterwards you see the first sync per source: how many signals came in and from how far back.

Skipped one? `assistant connect gmail` (or `slack`, `crm`, `whatsapp`, `android`, `github`, …) does the same any time. `assistant connect` on its own lists what is still missing.

## 3. Open the dashboard

The installer offers to publish the dashboard on your tailnet (`tailscale serve`, at `https://<machine>.<tailnet>.ts.net:8443/`). No domain or certificate to set up, and only your Tailscale login can open it. If Serve is not enabled on your tailnet yet, it prints the link to enable it. On your phone, open the address it prints and add it to your home screen.

## 4. Tell the agent who you are

The installer offers a starter `USER.md` in your OpenClaw workspace with your name, companies and email (added under OpenClaw's template if that is all it has). Reviews and drafts read it every time.

Then it offers to fill in the rest from what you actually write: your agent reads your own recent messages once (nothing is sent) and drafts your role, languages, how you write on each channel (email, Slack, WhatsApp, …), what matters to you and what to leave alone. Then it reads your first and latest messages with your closest people and notes what each is to you ("cofounder", "accountant", "customer at Globex") where the messages show it; you can change or rename anyone in the Graph tab, and `assistant roles` runs that step again later. You see the draft first; it's saved only if you say yes, and it never overwrites what you wrote. Redo it later with `assistant profile`, or edit `USER.md` yourself. Check: `USER.md` has an "Observed from my messages" section.

## 5. Preview, then turn it on

The installer offers a **preview**: your agent reviews the last 7 days once and shows the cards in the terminal and on the dashboard. Nothing is sent and nothing is done in your accounts.

If the cards look useful, say yes to **turning on reviews and Telegram cards**. Read [Security model](../README.md#security-model) first: **Do it** lets the agent act with your accounts, only after you tap it. You can also do both later with `./install.sh --preview` and `./install.sh --activate`.

## Check everything

```bash
assistant doctor
```

One pass over the service, the OpenClaw bridge, your Telegram approval, every source's last sync, reviews, the dashboard and `USER.md`. Every problem comes with the command that fixes it. It contacts none of your accounts; it only reads what is on this machine and checks that your dashboard address answers (your own domain counts, not just `tailscale serve`).

## Later

- Add or redo a source: `assistant connect <source>`. With one already there it asks whether to redo it or add another (a second company, a partner's calendar); the new one reuses your client, keys and settings and follows your naming. `assistant connect` on its own also shows accounts this machine has that nothing reads yet.
- Sending approved emails needs `gmail.send` on top of read-only access; add it with gog when you want it. Connecting never asks for more than read access.
- Upgrade: `git pull && ./install.sh`. Your config, data and secrets are kept.
