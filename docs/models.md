# Bring your own model

The Machine contains no model and no model settings. Everything that needs a model goes through your own OpenClaw agent: reviews, Do it preparations and the native Change prompt. Pick and configure the model in OpenClaw, and The Machine uses it.

## Which agent and model it uses

Reviews and preparations run as isolated sessions of your **main** OpenClaw agent:

- `agent:main:assistant-review-signals-<run>` for reviews
- `agent:main:assistant-review-preparation-<job>` for Do it

They resolve their model the same way your chats do: `agents.defaults.model.primary`, then `agents.defaults.model.fallbacks` in order (or a per-agent `agents.entries.main.model`). See OpenClaw's [Models](https://docs.openclaw.ai/concepts/models) and [Model providers](https://docs.openclaw.ai/concepts/model-providers) docs.

```json5
// ~/.openclaw/openclaw.json
{
  agents: {
    defaults: {
      model: {
        primary: "openai/gpt-6.1-sol",
        fallbacks: ["anthropic/claude-sonnet-5-5", "ollama/gemma4"],
      },
    },
  },
}
```

Check what you have with `openclaw models list` and `openclaw models status`. No restart of The Machine is needed after a model change.

## What the model has to be good at

| Job | Needs |
|---|---|
| **Reviews** | Long context: up to 120 signals per run, read in pages through the `assistant_signals` tool. Reliable tool calls and judgement: most signals deserve no card. |
| **Do it** | Agentic work: many tool calls, the browser, files and commands, then a short result and a correct `next_actions` JSON (for example an email draft with exact recipients). |
| **Drafts** | Writing in your voice. The service feeds your own edits of earlier drafts back as examples, so drafts improve as you edit them. |

In practice: use a current frontier model with strong tool use as the primary. Small or older models tend to suggest too much, skip pages of a review, or save malformed next actions. The service rejects invalid ones and the run then records a blocker instead of sending anything, but you'll see more "Needs your help" cards.

## Local and self-hosted models

Any OpenClaw provider works, including Ollama, LM Studio, vLLM or LiteLLM in front of your own GPUs. Points to check:

- **Context window:** at least 128K tokens is comfortable for reviews. With less, lower the claim size by having fewer signals wait (`review_worker.min_signals`) or by excluding noisy sources (see Configuration in the README).
- **Tool calling:** the model must support function/tool calls through your provider. Test with one Do it on a simple card before turning on prepare-ahead.
- **Speed:** a preparation may make dozens of calls. Runs are cut off after about 20 minutes; a slow local model may hit that on bigger tasks.

A mixed setup works well: a frontier model as `primary`, a local model as the last fallback, so reviews keep going during a provider outage.

## Privacy and cost

- Collected content stays in your SQLite database, but reviews and preparations send the signals they read to your model provider. With a local model, nothing leaves your machine.
- Each review reads up to 120 signals; each Do it reads the card's full evidence. The Friday recap reports the week's token usage, so you can see what a setup costs before committing to it.
- Prepare-ahead (cards that arrive already drafted) spends tokens on cards you may dismiss. Turn it off with `"prepare_ahead": false`, or cap it with `"prepare_ahead_per_day"` (default 10) in `config.json`.

## Known limitation

The Machine always uses your **main** agent. Using a separate agent (and so a separate model) for The Machine only is not supported yet; it would mean making the session key prefix configurable in `preparation-run.mjs` and the review runner. Contributions welcome.
