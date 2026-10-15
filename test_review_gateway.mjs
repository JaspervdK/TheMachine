import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
// Run the actual helper with only the SDK import replaced. No live Gateway calls.
const source=(await readFile(new URL('./preparation-run.mjs',import.meta.url),'utf8')).replace(/^import \{gatewayRuntime\}.*;\nconst \{callGatewayFromCli\}=await gatewayRuntime\(\);$/m,'');
const execute=new (Object.getPrototypeOf(async function(){}).constructor)('callGatewayFromCli','process',source);
const id='a'.repeat(32);
async function run(request,replies){
  const calls=[];let output='';
  await execute(async(method,options,args,scope)=>{
    calls.push({method,options,args,scope});
    const value=replies.shift();if(value instanceof Error)throw value;return value;
  },{stdin:{async *[Symbol.asyncIterator](){yield JSON.stringify(request)}},stdout:{write(value){output+=value}}});
  return {calls,result:JSON.parse(output)};
}
let r=await run({kind:'review',job_id:id,action:'stop'},[{ok:true,status:'no-active-run',abortedRunId:null}]);
assert.equal(r.result.status,'lost');
assert.deepEqual(r.calls[0].args,{key:'agent:main:assistant-review-signals-'+id,agentId:'main',runId:id,clearQueued:true});
r=await run({kind:'review',job_id:id,action:'stop'},[{ok:true,status:'aborted',abortedRunId:id},{status:'timeout'}]);
assert.equal(r.result.status,'pending');  // abort admission is not terminal proof
r=await run({kind:'review',job_id:id,action:'stop'},[{ok:true,status:'aborted',abortedRunId:id},{status:'timeout',endedAt:123}]);
assert.equal(r.result.status,'timed_out');
for(const response of [{ok:true,status:'aborted',abortedRunId:'b'.repeat(32)},{ok:true,status:'no-active-run',warning:'cleanup pending'},new Error('offline')]){
  r=await run({kind:'review',job_id:id,action:'stop'},[response]);assert.equal(r.result.status,'unavailable');
}
r=await run({kind:'review',job_id:id,action:'status'},[{status:'timeout'}]);assert.equal(r.result.status,'pending');
r=await run({kind:'review',job_id:id,action:'status'},[{status:'timeout',endedAt:123}]);assert.equal(r.result.status,'timed_out');
// Actual installed wait fields survive as small categories, never raw private text.
for(const [response,error,phase] of [
  [{status:'error',error:'LLM idle timeout (120s): no response from model; private token=abc'},'model_idle_timeout','model'],
  [{status:'error',error:'before_tool_call hook failed: PluginInstanceUnavailableError secret=abc'},'bridge_error','bridge'],
  [{status:'error',error:'ENOTFOUND private.example.com'},'connection_error','connection'],
  [{status:'error',error:'HTTP 429 rate limit; private token=abc'},'rate_limited','model'],
  [{status:'timeout',endedAt:123,error:'timed out',timeoutPhase:'queue'},'runtime_timeout','queue'],
  [{status:'error',error:'unexpected private email text'},'runtime_error','runtime'],
  [{status:'aborted'},'runtime_aborted','runtime'],
]){
  r=await run({kind:'review',job_id:id,action:'status'},[response]);
  assert.equal(r.result.error,error);assert.equal(r.result.error_phase,phase);
  assert.ok(!JSON.stringify(r.result).includes('private'));assert.ok(!JSON.stringify(r.result).includes('abc'));
}
r=await run({kind:'review',job_id:id,action:'status'},[{status:'pending',timeoutPhase:'queue'}]);
assert.equal(r.result.status,'pending');assert.equal(r.result.error,undefined);
r=await run({kind:'review',job_id:id,action:'start',message:'Review'},[{runId:id}]);
assert.equal(r.result.status,'admitted');assert.equal(r.calls[0].args.idempotencyKey,id);
assert.equal(r.calls[0].args.deliver,false);assert.equal(r.calls[0].args.disableMessageTool,true);
// Try again on a stuck preparation stops only its exact isolated session and run.
r=await run({kind:'preparation',job_id:id,action:'stop'},[{ok:true,status:'no-active-run',abortedRunId:null}]);
assert.equal(r.result.status,'lost');assert.equal(r.calls[0].args.key,'agent:main:assistant-review-preparation-'+id);assert.equal(r.calls[0].args.runId,id);
await assert.rejects(()=>run({kind:'other',job_id:id,action:'stop'},[]));
console.log('Exact-run recovery, wait-only timeout, cancellation proof and admission idempotency passed.');
