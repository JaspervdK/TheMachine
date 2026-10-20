import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(process.env.PLAYWRIGHT_CORE||OPENCLAW+'/node_modules/playwright-core/index.mjs');

// Fleet tab: who is who per member, how much each teammate hears in plain words, and on a phone the rules are cards
// that never scroll sideways (light and dark).
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
const rules={relationships:{rule:'ask',details:false},meeting:{rule:'always',details:true},handling:{rule:'ask',details:false}};
const view={on:true,kinds:{relationships:'who knows someone',meeting:'meeting context',handling:'who handles shared emails'},ask_meetings:false,
 fleets:[{id:'fl_'+'a'.repeat(16),kind:'work',label:'Work Fleet',name:'Acme',asks:['relationships','meeting'],
  people:[{member_id:'m',name:'Sam Doe',role:'owner',me:true,answers_with:'Self-hosted: Desk',email:'sam@acme.example'},
   {member_id:'p',name:'Sam Doe',role:'member',me:false,answers_with:'Hosted: sam.example.test',email:'sam.doe@mail.example'}],
  members:[{member_id:'p',name:'Sam Doe (sam.doe@mail.example)',rules},{member_id:'q',name:'Alexandra Longname-Whitfield',rules}]}],
 waiting:[],asked:[],log:[],notices:[]};
const calls=[];
const server=createServer(async(req,res)=>{res.setHeader('Content-Type','application/json');
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(req.url==='/api/status'){res.end(JSON.stringify({suggestions:[],connectors:[],suggestion_counts:{},decisions:[],analyses:[],deliveries:[],review_health:{},review_runs:[],fleet:{on:true,waiting:0}}));return}
 if(req.url==='/api/action'){let body='';for await(const b of req)body+=b;calls.push(JSON.parse(body));res.end('{"ok":true}');return}
 if(req.url.startsWith('/api/fleet')){res.end(JSON.stringify(view));return}
 res.statusCode=404;res.end('{}')});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{
 for(const [scheme,width] of [['dark',390],['light',390],['light',1000]]){
  const page=await (await browser.newContext({viewport:{width,height:900},colorScheme:scheme})).newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('nav [data-tab="fleet"]').click();await page.getByText('Your Work Fleet: Acme').waitFor();
  assert.deepEqual(errors,[]);
  const people=await page.locator('.fleet-people li').allInnerTexts();
  assert.match(people[0],/\(you, this Machine\)/);assert.match(people[1],/Hosted: sam\.example\.test/);
  // How much: a plain choice, not a bare "Details" box.
  assert.equal(await page.locator('.fleet-rules input[type=checkbox]').count(),0);
  assert.match(await page.locator('.section-head:has(+ .fleet-rules) p').innerText(),/Without names: only how well you know someone there/);
  assert.deepEqual(await page.locator('[data-fleet-details="p"][data-kind="relationships"] option').allInnerTexts(),['Without names','With names']);
  assert.deepEqual(await page.locator('[data-fleet-details="p"][data-kind="meeting"] option').allInnerTexts(),['Without names','Names and promises']);
  assert.equal(await page.locator('[data-fleet-details="p"][data-kind="meeting"]').inputValue(),'1');
  assert.equal(await page.locator('[data-fleet-details][data-kind="handling"]').count(),0);
  calls.length=0;await page.locator('[data-fleet-details="q"][data-kind="relationships"]').selectOption('1');
  await assert.doesNotReject(async()=>{for(let i=0;i<50&&!calls.length;i++)await page.waitForTimeout(20)});
  assert.deepEqual(calls[0],{action:'fleet_rule',args:{fleet:'fl_'+'a'.repeat(16),member_id:'q',kind:'relationships',details:true}});
  const layout=await page.evaluate(()=>{const t=document.querySelector('.fleet-rules');
   return {fits:document.documentElement.scrollWidth<=innerWidth,inside:[...t.querySelectorAll('select')].every(s=>s.getBoundingClientRect().right<=innerWidth+.5),
    display:getComputedStyle(t).display,label:getComputedStyle(t.querySelector('td[data-label]'),'::before').content}});
  assert.ok(layout.fits&&layout.inside,`${scheme} ${width}px scrolls sideways`);
  if(width<600){assert.equal(layout.display,'block');assert.equal(layout.label,'"Who knows someone"')}
  else assert.equal(layout.display,'table');
  await page.context().close();
 }
 console.log('fleet dashboard: ok');
}finally{await browser.close();server.close()}
