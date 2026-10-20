import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_CORE||OPENCLAW+'/node_modules/playwright-core/index.mjs');

// Fleet tab: who is who per member, and one rule component for every kind: Ask me first | Always | Never as pills, how
// much they hear, Only about…, and an example of what the teammate would get. Never scrolls sideways (390px and 1000px,
// light and dark).
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
const FL='fl_'+'a'.repeat(16);
const rules=()=>({relationships:{rule:'ask',details:false},meeting:{rule:'always',details:true},handling:{rule:'ask',details:false},brief:{rule:'never',details:false}});
const view={on:true,kinds:{relationships:'who knows someone',meeting:'meeting context',handling:'who handles shared emails',brief:'brief facts'},ask_meetings:false,
 kind_rules:{relationships:['ask','always','never'],meeting:['ask','always','never'],handling:['ask','always','never'],brief:['ask','always','never']},
 no_details:['handling','brief'],scoped:['relationships','meeting','handling'],scopes:{[FL]:{q:{relationships:{only:['acme.com'],never:['initech'],work_only:true}}}},
 fleets:[{id:FL,kind:'work',label:'Work Fleet',name:'Acme',asks:['relationships','meeting'],
  people:[{member_id:'m',name:'Sam Doe',role:'owner',me:true,answers_with:'Self-hosted: Desk',email:'sam@acme.example'},
   {member_id:'p',name:'Sam Doe',role:'member',me:false,answers_with:'Hosted: sam.example.test',email:'sam.doe@mail.example'}],
  members:[{member_id:'p',name:'Sam Doe (sam.doe@mail.example)',rules:rules()},{member_id:'q',name:'Alexandra Longname-Whitfield',rules:rules()}]}],
 waiting:[],asked:[],log:[],notices:[]};
const calls=[];
const server=createServer(async(req,res)=>{res.setHeader('Content-Type','application/json');
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(req.url==='/api/status'){res.end(JSON.stringify({suggestions:[],connectors:[],suggestion_counts:{},decisions:[],analyses:[],deliveries:[],review_health:{},review_runs:[],fleet:{on:true,waiting:0}}));return}
 if(req.url==='/api/action'){let body='';for await(const b of req)body+=b;const call=JSON.parse(body);calls.push(call);const a=call.args;
  const was=view.fleets[0].members.find(m=>m.member_id===a.member_id).rules[a.kind];
  const scope=a.scope?{only:String(a.scope.only).split(',').map(x=>x.trim().toLowerCase()).filter(Boolean),never:String(a.scope.never).split(',').map(x=>x.trim().toLowerCase()).filter(Boolean),work_only:a.scope.work_only}:{only:[],never:[],work_only:true};
  Object.assign(was,{rule:a.rule||was.rule,details:a.details??was.details});res.end(JSON.stringify({...was,scope}));return}
 if(req.url.startsWith('/api/fleet')){res.end(JSON.stringify(view));return}
 res.statusCode=404;res.end('{}')});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const shots=process.env.FLEET_SHOTS;
try{
 for(const [scheme,width] of [['dark',390],['light',390],['dark',1000],['light',1000]]){
  view.fleets[0].members.forEach(m=>m.rules=rules());
  const page=await (await browser.newContext({viewport:{width,height:900},colorScheme:scheme})).newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('nav [data-tab="fleet"]').click();await page.getByText('Your Work Fleet: Acme').waitFor();
  assert.deepEqual(errors,[]);
  const people=await page.locator('.fleet-people li').allInnerTexts();
  assert.match(people[0],/\(you, this Machine\)/);assert.match(people[1],/Hosted: sam\.example\.test/);
  // Every kind: the same three pills, no select menus, no bare Details box.
  assert.equal(await page.locator('.fleet-rules select, .fleet-rules input[type=checkbox]:not([data-scope])').count(),0);
  const p=page.locator('.fleet-rule[data-member="p"]'),R=(m,k)=>page.locator(`.fleet-rule[data-member="${m}"][data-kind="${k}"]`);
  for(const k of ['relationships','meeting','handling','brief'])
   assert.deepEqual(await R('p',k).locator('[data-fleet-rule]').evaluateAll(x=>x.map(i=>i.parentElement.innerText)),['Ask me first','Always','Never'],k);
  assert.equal(await R('p','handling').locator('[data-fleet-rule]:checked').getAttribute('value'),'ask');
  assert.deepEqual(await R('p','meeting').locator('[data-fleet-details]').evaluateAll(x=>x.map(i=>i.parentElement.innerText)),['Without names','Names and promises']);
  assert.equal(await R('p','handling').locator('[data-fleet-details]').count(),0);
  // Only about… is folded away, only for kinds that apply it, and says what is set.
  assert.equal(await R('p','brief').locator('.fleet-scope').count(),0);
  const q=page.locator('.fleet-rule[data-member="q"][data-kind="relationships"]');
  assert.equal(await q.locator('details.fleet-scope').evaluate(d=>d.open),false);
  assert.match(await q.locator('summary').innerText(),/only acme\.com; not initech/);
  // The example says what this teammate would get.
  const preview=k=>R('p',k).locator('.fleet-preview').innerText();
  assert.match(await preview('relationships'),/^For example: You’re asked first\. If you answer, Sam sees: you know 3 people at Acme/);
  assert.match(await preview('meeting'),/Sam sees, without asking you: .*Ann Lee.*promises/);
  assert.match(await preview('brief'),/Sam gets “no”/);
  assert.match(await q.locator('.fleet-preview').innerText(),/people at acme\.com.*Only about acme\.com, anything else asks you first; never about initech/);
  // Choosing saves, and the example follows.
  calls.length=0;await R('p','relationships').locator('[data-fleet-rule][value="always"]').check({force:true});
  await page.waitForFunction(()=>/without asking you/.test(document.querySelector('.fleet-rule[data-member="p"][data-kind="relationships"] .fleet-preview').textContent));
  assert.deepEqual(calls[0],{action:'fleet_rule',args:{fleet:FL,member_id:'p',kind:'relationships',rule:'always'}});
  await R('p','relationships').locator('[data-fleet-details][value="1"]').check({force:true});
  await page.waitForFunction(()=>/Ann Lee/.test(document.querySelector('.fleet-rule[data-member="p"][data-kind="relationships"] .fleet-preview').textContent));
  assert.deepEqual(calls[1].args,{fleet:FL,member_id:'p',kind:'relationships',details:true});
  await R('p','relationships').locator('summary').click();
  await R('p','relationships').locator('[data-scope="never"]').fill('Globex, rival.example');
  await R('p','relationships').locator('[data-scope="never"]').press('Tab');
  await page.waitForFunction(()=>/never about globex/i.test(document.querySelector('.fleet-rule[data-member="p"][data-kind="relationships"] .fleet-preview').textContent));
  assert.deepEqual(calls[2].args,{fleet:FL,member_id:'p',kind:'relationships',scope:{only:'',never:'Globex, rival.example',work_only:true}});
  assert.match(await R('p','relationships').locator('summary').innerText(),/not globex, rival\.example/);
  const layout=await page.evaluate(()=>{const r=document.querySelector('.fleet-rules');
   return {fits:document.documentElement.scrollWidth<=innerWidth,inside:[...r.querySelectorAll('.seg, .fleet-preview, .fleet-scope input')].every(s=>s.getBoundingClientRect().right<=innerWidth+.5),
    stacked:getComputedStyle(r.querySelector('.fleet-rule')).gridTemplateColumns.split(' ').length===1}});
  assert.ok(layout.fits&&layout.inside,`${scheme} ${width}px scrolls sideways`);
  assert.equal(layout.stacked,width<600);
  if(shots){await page.evaluate(()=>document.querySelector('.fleet-rule[data-member="q"][data-kind="relationships"] details').open=true);await page.locator('.fleet-rules').screenshot({path:`${shots}/${scheme}-${width}.png`})}
  await page.context().close();
 }
 console.log('fleet dashboard: ok');
}finally{await browser.close();server.close()}
