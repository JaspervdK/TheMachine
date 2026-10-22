import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(OPENCLAW+'/node_modules/playwright-core/index.mjs');

// A card whose result waits on the owner's answer (needs_you, written as a question) leads with Answer: it opens the
// Change editor under the question, and Send answer saves it as a Change note. Mark done steps back. A question line
// keeps its "?" (no "?."). A needs line that isn't a question, or one with a draft to send, keeps its usual buttons.
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
const now=new Date().toISOString();
const cards=[
 {id:'q1',title:'Get the quarterly report ready',body:'Filled in every field except the cash-out date.',status:'prepared',scope:'work',created_at:now,event_ids:[1],
  outcome:JSON.stringify({state:'needs_you',needs:'How should infinite runway go in the date-only cash-out field?'})},
 {id:'t1',title:'Renew the parking permit',body:'Filled in the form.',status:'prepared',scope:'personal',created_at:now,event_ids:[2],
  outcome:JSON.stringify({state:'needs_you',needs:'Pay in the bank app.'})},
 {id:'d1',title:'Reply to Noor about the contract',body:'Drafted a reply for the likelier answer.',status:'prepared',scope:'work',created_at:now,event_ids:[3],
  outcome:JSON.stringify({state:'needs_you',needs:'Hold firm on the price?'}),
  next_actions:[{kind:'email',revision:'v1',label:'Send to Noor',to:['noor@example.com'],to_name:'Noor',subject:'Contract',body:'Hi Noor'}]}];
const posts=[];
const state=()=>({suggestions:cards,promises:[],suggestion_counts:{},decisions:[],analyses:[],deliveries:[],review_health:{},review_runs:[],review_live:{runs:[]},connectors:[],fleet:{on:false,fleets:[]}});
const server=createServer(async(req,res)=>{let body='';for await(const c of req)body+=c;const url=new URL(req.url,'http://fixture.invalid');res.setHeader('Content-Type','application/json');
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(url.pathname==='/api/status'){res.end(JSON.stringify(state()));return}
 if(url.pathname==='/api/action'&&req.method==='POST'){posts.push(JSON.parse(body||'{}'));res.end('{"status":"instructions"}');return}
 res.statusCode=url.pathname.startsWith('/brand/')?404:200;res.end('{}')});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const errors=[];let browser;
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 for(const width of [1280,390]){
  posts.length=0;
  const context=await browser.newContext({viewport:{width,height:900}});
  await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);await page.locator('#task-q1').waitFor();
  const line=id=>page.locator(`#task-${id} .status-line`).textContent();
  const primary=id=>page.locator(`#task-${id} .actions button.primary`).allTextContents();
  // The question keeps its own mark; a plain line gets one full stop
  assert.equal(await line('q1'),'Needs you: How should infinite runway go in the date-only cash-out field?');
  assert.equal(await line('t1'),'Needs you: Pay in the bank app.');
  // Answer leads; Mark done is there but not primary; no separate Change (Answer is that path)
  assert.deepEqual(await primary('q1'),['Answer']);
  assert.equal(await page.locator('#task-q1 [data-choice="complete"]').getAttribute('class'),'ghost');
  assert.equal(await page.locator('#task-q1 [data-choice="change"]').count(),0);
  // Not a question: Mark done still leads. A draft to send: Review and send leads, no Answer
  assert.deepEqual(await primary('t1'),['Mark done']);
  assert.deepEqual(await primary('d1'),['Review and send to Noor']);
  assert.equal(await page.locator('#task-d1 [data-choice="answer"]').count(),0);
  // The button row fits the card on a phone
  assert.ok(await page.locator('#task-q1 .actions').evaluate(a=>[...a.children].every(c=>c.getBoundingClientRect().right<=a.closest('article').getBoundingClientRect().right+1)));
  // Answer opens the answer box under the question
  await page.locator('#task-q1 [data-choice="answer"]').click();
  assert.equal(await page.locator('#task-q1 .answer-editor label').textContent(),'Your answer');
  assert.ok(await page.locator('#task-q1 .status-line').isVisible());
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'feedback-q1');
  const send=page.locator('#task-q1 [data-choice="other"]');
  assert.equal(await send.textContent(),'Send answer');
  // Empty: asks for the answer, sends nothing
  await send.click();assert.match(await page.locator("#toast").textContent(),/Type your answer first/);assert.equal(posts.length,0);
  // Ctrl+Enter sends it the Change way
  await page.locator('#feedback-q1').fill('Use 31 Dec 2099 and say so in the notes.');await page.locator('#feedback-q1').press('Control+Enter');
  await page.waitForFunction(()=>/carry on/.test(document.querySelector('#toast')?.textContent||''));
  const sent=posts.find(p=>p.action==='decide');
  assert.equal(sent.args.suggestion_id,'q1');assert.equal(sent.args.choice,'other');assert.equal(sent.args.feedback,'Use 31 Dec 2099 and say so in the notes.');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('#task-q1').screenshot({path:`/tmp/answer-card-${width}.png`});
  await context.close();
 }
 assert.deepEqual(errors,[]);
}finally{await browser?.close();server.close()}
console.log('answer question dashboard ok');
