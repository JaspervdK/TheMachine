// One service-started turn in the existing main agent, through the installed Gateway:
// a preparation for an accepted suggestion, a signal review, or a profile draft (assistant profile). Each runs in its own
// isolated session with message delivery off. No model/client credentials here.
import {gatewayRuntime} from './openclaw-root.mjs';
const {callGatewayFromCli}=await gatewayRuntime();
let input=''; for await (const chunk of process.stdin) input+=chunk;
const request=JSON.parse(input);
const kind=request.kind || 'preparation';
if (!/^[a-f0-9]{32}$/.test(request.job_id || '') || !['start','status','stop'].includes(request.action) || !['preparation','review','profile'].includes(kind)) {
  throw new Error('Invalid request');
}
const session={preparation:'agent:main:assistant-review-preparation-', review:'agent:main:assistant-review-signals-', profile:'agent:main:assistant-review-profile-'}[kind];
const sessionId=request.session_job_id || request.job_id;
if (!/^[a-f0-9]{32}$/.test(sessionId) || (kind!=='preparation' && sessionId!==request.job_id)) throw new Error('Invalid session');
// Do it turns make and test real changes (2026-10-13), so they get as long as a review.
const runSeconds={preparation:1200, review:1200, profile:900}[kind];
const scope={scopes:['operator.admin','operator.read','operator.write']};
// Retain useful failure categories, never raw provider text, endpoints or parameters.
function failure(result,status){
  if(status==='ok'||status==='admitted'||status==='pending')return {};
  const text=[result?.error?.message,result?.error,result?.errorMessage,result?.summary].filter(v=>typeof v==='string').join(' ');
  let error=status==='timed_out'?'runtime_timeout':status==='aborted'?'runtime_aborted':status==='lost'?'runtime_lost':'runtime_error';
  let phase='runtime';
  if(/LLM idle timeout/i.test(text)){error='model_idle_timeout';phase='model';}
  else if(/before_tool_call|PluginInstanceUnavailable|failurePhase[^a-z]*bridge/i.test(text)){error='bridge_error';phase='bridge';}
  else if(/rate.?limit|\b429\b/i.test(text)){error='rate_limited';phase='model';}
  else if(/ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|connection (?:reset|closed)|network/i.test(text)){error='connection_error';phase='connection';}
  else if(/timeout|timed out/i.test(text)){error='runtime_timeout';}
  else if(/abort|cancel/i.test(text)){error='runtime_aborted';}
  if(['queue','provider','model','tool','gateway_draining'].includes(result?.timeoutPhase))phase=result.timeoutPhase;
  return {error,error_phase:phase};
}
try {
  let result;
  if (request.action==='start') {
    if (typeof request.message!=='string' || request.message.length>20000) throw new Error('Invalid prompt');
    result=await callGatewayFromCli('agent',{timeout:'20000'}, {
      agentId:'main', sessionKey:session+sessionId,
      message:request.message, idempotencyKey:request.job_id, timeout:runSeconds,
      deliver:false, disableMessageTool:true,
    },scope);
    if (result?.runId!==request.job_id) throw new Error('Unconfirmed admission');
    process.stdout.write(JSON.stringify({run_id:request.job_id,status:'admitted'}));
  } else if (request.action==='stop') {
    // After the normal runtime plus an outage grace period, stop only our exact
    // isolated turn. A successful no-active-run receipt also reconciles a run
    // lost in a Gateway restart. Unknown/failed responses never release custody.
    result=await callGatewayFromCli('sessions.abort',{timeout:'15000'}, {
      key:session+sessionId, agentId:'main', runId:request.job_id, clearQueued:true,
    },scope);
    const stopped=result?.ok===true && !result?.warning && result?.status==='aborted' && result?.abortedRunId===request.job_id;
    const absent=result?.ok===true && !result?.warning && result?.status==='no-active-run' && !result?.abortedRunId;
    let status=absent?'lost':'unavailable', outcome;
    if (stopped) {
      outcome=await callGatewayFromCli('agent.wait',{timeout:'15000'}, {runId:request.job_id,timeoutMs:0},scope);
      // Cancellation was admitted, but don't claim the old runtime has finished
      // until its terminal outcome (or the next exact no-active-run receipt).
      status=outcome?.endedAt && ['ok','error','aborted','timeout'].includes(outcome?.status) ? 'timed_out':'pending';
    }
    process.stdout.write(JSON.stringify({run_id:request.job_id,status,...failure(outcome,status)}));
  } else {
    result=await callGatewayFromCli('agent.wait',{timeout:'15000'}, {runId:request.job_id,timeoutMs:0},scope);
    const status=['ok','error','aborted'].includes(result?.status)?result.status:
        result?.status==='timeout' && result?.endedAt ? 'timed_out':'pending';
    process.stdout.write(JSON.stringify({run_id:request.job_id,status,...failure(result,status)}));
  }
} catch {
  // An admission timeout is ambiguous: retain the SAME idempotency key.
  process.stdout.write(JSON.stringify({run_id:request.job_id,status:'unavailable'}));
}
