// Keep heartbeat failure notices silent unless the
// heartbeat fails 5 times in a row. One-off failures (a plugin reload racing a
// notification, a short internet drop) recover on the next run by themselves.
// A successful heartbeat-main run since the previous failure resets the streak.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { homedir } from 'node:os';
import { DatabaseSync } from 'node:sqlite';

const LEAD = '⚠️ Heartbeat check failed before it could produce an update';
const LIMIT = 5;
const WORKSPACE = process.env.OPENCLAW_WORKSPACE || `${homedir()}/.openclaw/workspace`;
const STATE = `${WORKSPACE}/data/heartbeat-failure-gate.json`;
const STATE_DB = `${homedir()}/.openclaw/state/openclaw.sqlite`;
// The OpenClaw cron job id of the main heartbeat, from the assistant config (`heartbeat_job_id`).
// Without it the streak is never reset by a later success; notices still pass every LIMIT failures.
function heartbeatJob(): string {
  try {
    const path = process.env.ASSISTANT_CONFIG || `${homedir()}/.local/share/personal-assistant/config.json`;
    return String(JSON.parse(readFileSync(path, 'utf8')).heartbeat_job_id || '');
  } catch { return ''; }
}

type GateState = { streak: number; lastMs: number };

function load(): GateState {
  try { return { streak: 0, lastMs: 0, ...JSON.parse(readFileSync(STATE, 'utf8')) }; } catch { return { streak: 0, lastMs: 0 }; }
}
function save(state: GateState) {
  try { mkdirSync(dirname(STATE), { recursive: true }); writeFileSync(STATE, JSON.stringify(state) + '\n'); } catch {}
}
function lastHeartbeatSuccessMs(): number {
  let db: any;
  try {
    db = new DatabaseSync(STATE_DB, { readOnly: true });
    const row: any = db.prepare(
      "select max(ended_at) as at from task_runs where source_id = ? and status = 'succeeded'").get(heartbeatJob());
    return Number(row?.at) || 0;
  } catch { return 0; } finally { try { db?.close(); } catch {} }
}

export function registerHeartbeatFailureGate(api: any) {
  api.on('message_sending', async (event: any) => {
    const text = String(event?.content ?? '');
    if (!text.trimStart().startsWith(LEAD)) return;
    const state = load();
    if (lastHeartbeatSuccessMs() > state.lastMs) state.streak = 0;
    state.streak += 1;
    state.lastMs = Date.now();
    save(state);
    if (state.streak % LIMIT !== 0) {
      return { cancel: true, cancelReason: `heartbeat_failure_${state.streak}_of_${LIMIT}` };
    }
    return { content: text.replace(LEAD, `${LEAD} (${state.streak} times in a row)`) };
  });
}
