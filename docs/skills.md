# Skills

A skill is a how-to your Machine's agent follows for one kind of work, for example "Research and answer customer
inquiries". It has three parts: a **name**, **when to use it** (one line), and **instructions** (steps, sources to
check, what never to do; Markdown works).

Skills are kept in **Settings > Skills** on the dashboard. You can write your own, start from the example, approve one
your Machine proposes, or install one a teammate or friend shares with you.

## How OpenClaw uses them

Skills are plain [OpenClaw skills](https://docs.openclaw.ai/tools/skills). Each skill that is on is written to
`<workspace>/skills/the-machine/<name>/SKILL.md` in your OpenClaw workspace. OpenClaw picks them up like any workspace
skill, and its agent reads one when a task matches "when to use it". New and edited skills apply from the agent's next
session; the Gateway doesn't need a restart.

The `the-machine` folder belongs to The Machine: it is rewritten from Settings > Skills, so edit skills there. Your
other skills in the workspace are left alone. If a name is already taken by one of them, The Machine's copy gets a
different folder name (`-2`), so it never replaces yours. A whole `SKILL.md` can be pasted into the instructions box:
the name and description are taken from its header.

## Instructions only

A skill can't change what your Machine may do. Only the name, when to use it and the instructions are written; any
other OpenClaw header field (metadata, environment variables, install steps, tool dispatch, allowed tools) and any
scripts are dropped. A skill runs under your Machine's own rules and approvals, like anything else the agent does:
Do it still asks you, nothing is sent without your Send, and a Fleet's rules stay as you set them.

## Proposed by your Machine

When your agent notices the same kind of work coming back, it can propose a skill for it
(`assistant skills-propose --name ... --description ... --file -`). A proposal stays off and waits in Settings > Skills
with **Approve** and **Dismiss**; at most three wait at a time. Only your tap on the dashboard turns it on.

## Sharing with your Fleet or a friend

Next to each skill, **Share** sends it over The Machine Network:

- to everyone in one of your Fleets (Work or Family), or to one teammate;
- to one friend (see [Friends](fleet.md#friends)).

The Share button appears once the Network carries skills for that Fleet or friend. Only the name, when to use it, the
instructions, a version number, a content hash and an optional note travel. Later edits don't go out by themselves:
the skill shows "Changed since you shared it", and **Share again** sends the new version.

The person you share with sees the skill under Settings > Skills, with who sent it and their note, and a **Needs you**
item. **Read** shows the instructions as text, quoted as the sender's words. Nothing is installed until they tap
**Install**; **Decline** removes it. Your Machine hears "installed" or "declined", and the skill shows who installed
it. A new version from the same person arrives as an update to read and install again; the skill they have stays as it
is until then.

What arrives is checked: it must be in its fixed form, with a hash that matches its content, from someone in that Fleet
or a friend. Anything else is declined. One person can have at most five skills waiting for you at a time, and the
Network limits how many skills one person can share per day.

Sharing a skill is free. Like every question on the Network, a shared skill is sealed for the receiving Machine and
signed with the sending Machine's own key (see [The Machine Network](network.md)), so the Network can't read or change
it and the receiver knows whose Machine sent it.

## Everything OpenClaw has

Below your own skills, Settings > Skills lists every other skill OpenClaw has, grouped as Workspace, Bundled with
OpenClaw and Other. Each shows whether it is ready or what it still needs (a command, a key, another OS). The list comes
from `openclaw skills list`, refreshes every 30 minutes and on **Check again**, and the search box filters it.

These skills are only read here, never edited. **Read** shows their `SKILL.md`. **Turn on/off** sets OpenClaw's own
`skills.entries.<name>.enabled`; the Gateway doesn't need a restart. Your agent already sees every skill that is ready,
in preparations too.

## From ClawHub

**Search ClawHub** uses `openclaw skills search`. Results are third-party: written by other people, not checked by The
Machine. **Read** shows the `SKILL.md` with notes on any scripts, install steps, keys it asks for and ClawHub's scan.
**Install** is only there after reading. It runs `openclaw skills install` for exactly that version, and only if it is
still the `SKILL.md` you read. Such a skill is not limited to instructions like your own: it is OpenClaw's, with
whatever it brings.

## There's a skill for this

When your agent reviews or prepares a card and an installed skill that isn't ready yet, or one on ClawHub, fits what
the card is about, it can add a quiet "There's a skill for this" line to the card (`skill_hint` on `suggest` or
`prepare`). It also shows under Settings > Skills. **Look** opens it; **Not now** dismisses it for good. At most three
a week, nothing is ever installed or turned on by itself.

## From the command line

- `assistant skills` lists your skills and what waits for you.
- `assistant skills-propose` proposes one (see above). Installing, sharing and deciding are on the dashboard only.
