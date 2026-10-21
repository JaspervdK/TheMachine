import assert from 'node:assert/strict';
import {readFile,mkdir} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(OPENCLAW+'/node_modules/playwright-core/index.mjs');

// Settings > Skills > Import: Find my skills lists what it found, grouped, with a quiet "Recommended" and the reason;
// a built-in skill whose licence says no can't be ticked; the accounts turn runs on and says when a site is signed out;
// the owner ticks and imports; Read shows what would come in and, for a skill that needs its scripts, Install in
// OpenClaw. Upload sends the file marked as an upload. Fits a 390px phone and a 1280px desktop.
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
const LONG='~/Private/some-project-with-a-really-long-folder-name/.claude/skills/answer-customer-questions-from-the-knowledge-base';
const item=(over)=>({key:'claude:x',name:'x',description:'When to use it',from:'claude',from_label:'Claude',source:'~/.claude/skills/x',url:'',group:'yours',files:1,
 scripts:[],script_count:0,depends:false,references:[],left_out:[],flags:[],licence:'',blocked:'',duplicate:'',recommended:false,why:'',...over});
const items=[
 item({key:'claude:~/.claude/skills/meeting-prep',name:'meeting-prep',description:'Before a meeting: brief the owner on who they meet and what is open.',recommended:true,why:'Your cards are mostly meeting preparations'}),
 item({key:'claude:'+LONG,name:'answer-customer-questions-from-the-knowledge-base',description:'When a customer asks how something works.',source:LONG,group:'project',references:['FAQ.md']}),
 item({key:'claude:~/.claude/skills/omarchy',name:'omarchy',description:'Desktop settings on Omarchy.',group:'vendor',link:'/usr/share/omarchy/skills/omarchy',flags:['Renamed tools to OpenClaw\'s: Bash → exec']}),
 item({key:'chatgpt:~/.codex/skills/report',name:'weekly-report',description:'Weekly KPI report.',from:'chatgpt',from_label:'ChatGPT',source:'~/.codex/skills/report',scripts:['scripts/build.py'],script_count:1,depends:true,left_out:['1 script (scripts/build.py)']}),
 item({key:'claude:pdf',name:'pdf',description:'PDF files.',group:'builtin',source:'claude.ai, built in (pdf)',licence:'Proprietary. LICENSE.txt has complete terms',blocked:'Can\'t import: its licence doesn\'t allow it',script_count:8,scripts:['scripts/a.py']}),
];
let list={token:'tok1',items,errors:[],finding:{run_id:'r1',status:'running'},browser:{},local:true};
let skills={skills:[],waiting:0,targets:[],folder:'skills/the-machine',openclaw:true,import_local:true,hints:[]};
const calls=[];
const server=createServer((req,res)=>{
 const url=new URL(req.url,'http://fixture.invalid');res.setHeader('Content-Type','application/json');
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(url.pathname==='/api/status'){res.end(JSON.stringify({suggestions:[],promises:[],suggestion_counts:{},decisions:[],analyses:[],deliveries:[],review_health:{},review_runs:[],connectors:[],skills}));return}
 if(url.pathname==='/api/action'){let body='';req.on('data',c=>body+=c);req.on('end',()=>{const {action,args}=JSON.parse(body);calls.push({action,args,upload:req.headers['x-upload']||''});
   if(action==='skill_installed'){res.end(JSON.stringify({installed:[],checked_at:null,error:''}));return}
   if(action==='skill_import_find'){res.end(JSON.stringify(list));return}
   if(action==='skill_import_status'){list={...list,finding:{run_id:'r1',status:'ok'},browser:{claude:'ok',chatgpt:'signed_out'},
     items:[...list.items,item({key:'claude:account:follow-ups',name:'follow-ups',description:'Chase replies that are late.',source:'claude.ai, your account',url:'https://claude.ai/customize/skills',recommended:true,why:'You get many follow-up cards'})]};
     res.end(JSON.stringify(list));return}
   if(action==='skill_import_read'){const found=list.items.find(i=>i.key===args.key);res.end(JSON.stringify({...found,body:'# Steps\n<b>not bold</b>\n1. Run scripts/build.py',original:'',sha:'abc',installable:found.script_count>0&&!found.blocked}));return}
   if(action==='skill_import_run'){const done=args.keys.map(k=>({key:k,name:list.items.find(i=>i.key===k).name,status:'imported',left_out:[],flags:[]}));
     list={...list,items:list.items.map(i=>args.keys.includes(i.key)?{...i,duplicate:'Already in your skills'}:i)};
     skills={...skills,skills:done.map((d,n)=>({id:'sk_'+n,name:d.name,description:'x',status:'on',origin:'mine',version:1,author:'',note:'',update:false,chars:9,shares:[],changed:false,imported:{from:'claude'}}))};
     res.end(JSON.stringify({results:done,list,...skills}));return}
   if(action==='skill_import_install'){res.end(JSON.stringify({installed_skill:'weekly-report',list,installed:[],checked_at:null,error:''}));return}
   if(action==='skill_import_upload'){res.end(JSON.stringify({...list,token:'tok2',finding:null,items:[item({key:'claude:upload:x.zip/',name:'from-zip',source:'x.zip'})]}));return}
   res.end('{}')});return}
 res.statusCode=404;res.end();
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const errors=[];let browser;
const fits=page=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
const shots=process.env.SCREENSHOTS_DIR;if(shots)await mkdir(shots,{recursive:true});
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 for(const width of [390,1280]){
  list={...list,token:'tok1',items,finding:{run_id:'r1',status:'running'},browser:{}};calls.length=0;skills={...skills,skills:[]};
  const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage();
  await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('#open-settings').click();
  await page.locator('#skill-import-open').click();
  await page.locator('#skill-import:not([hidden])').waitFor();
  await page.locator('#import-find').click();
  await page.locator('[data-import]').first().waitFor();
  // Grouped: yours first, recommended with its reason; the licence-limited built-in can't be ticked and says why.
  assert.deepEqual(await page.locator('#import-list h4').allTextContents(),['Yours 2','In your projects 1','From vendors 1','Built in 1']);
  assert.match(await page.locator('[data-import="claude:~/.claude/skills/meeting-prep"]').textContent(),/Recommended: Your cards are mostly meeting preparations/);
  const pdf=page.locator('[data-import="claude:pdf"]');
  assert.equal(await pdf.locator('input').isDisabled(),true);
  assert.match(await pdf.textContent(),/Can't import: its licence doesn't allow it/);
  assert.match(await page.locator('[data-import="chatgpt:~/.codex/skills/report"]').textContent(),/Needs its 1 script/);
  assert.match(await page.locator('#import-status').textContent(),/Looking in your claude\.ai and ChatGPT accounts/);
  assert.ok(await fits(page),'import list fits at '+width);
  // The accounts turn ends: its skill joins the list, and a signed-out site says what to do.
  await page.locator('[data-import="claude:account:follow-ups"]').waitFor({timeout:9000});
  assert.match(await page.locator('#import-status').textContent(),/Sign in to ChatGPT in your Machine’s browser to include those\./);
  assert.ok(calls.some(c=>c.action==='skill_import_status'&&c.args.token==='tok1'));
  if(shots)await page.locator('#skill-import').screenshot({path:`${shots}/skills-import-${width}.png`});
  // Read: what would come in, shown as text; a skill that needs its scripts offers OpenClaw's install instead.
  await page.locator('[data-import-read="chatgpt:~/.codex/skills/report"]').click();
  await page.locator('#skill-look[open]').waitFor();
  assert.equal(await page.locator('#skill-look-body b').count(),0);
  assert.match(await page.locator('#skill-look-notes').textContent(),/It needs its 1 script \(scripts\/build\.py\)/);
  assert.ok(await page.locator('#skill-look').evaluate(d=>d.getBoundingClientRect().right<=innerWidth));
  if(shots)await page.locator('#skill-look').screenshot({path:`${shots}/skills-import-read-${width}.png`});
  await page.locator('[data-import-install]').click();
  await page.locator('#notice',{hasText:'Installed in OpenClaw with its scripts'}).waitFor();
  assert.deepEqual(calls.find(c=>c.action==='skill_import_install').args,{token:'tok1',key:'chatgpt:~/.codex/skills/report',sha:'abc'});
  // Tick two and import them.
  await page.locator('[data-import="claude:~/.claude/skills/meeting-prep"] input').check();
  await page.locator('[data-import="claude:account:follow-ups"] input').check();
  assert.equal(await page.locator('#import-go').textContent(),'Import 2');
  await page.locator('#import-go').click();
  await page.locator('#notice',{hasText:'Imported 2 skills.'}).waitFor();
  assert.deepEqual(calls.find(c=>c.action==='skill_import_run').args,{token:'tok1',keys:['claude:~/.claude/skills/meeting-prep','claude:account:follow-ups']});
  assert.match(await page.locator('[data-import="claude:account:follow-ups"]').textContent(),/Already in your skills/);
  assert.equal(await page.locator('#skills-list [data-skill]').count(),2);
  // An upload goes as one, marked as an upload, with the chosen origin.
  await page.locator('input[name="import-from"][value="chatgpt"]').check();
  await page.locator('#import-zip').setInputFiles({name:'x.zip',mimeType:'application/zip',buffer:Buffer.from('PK fake')});
  await page.locator('[data-import="claude:upload:x.zip/"]').waitFor();
  const up=calls.find(c=>c.action==='skill_import_upload');
  assert.equal(up.upload,'skill-import');assert.equal(up.args.from,'chatgpt');assert.equal(up.args.data.name,'x.zip');
  assert.equal(Buffer.from(up.args.data.zip,'base64').toString(),'PK fake');
  assert.ok(await fits(page));
  await context.close();
 }
 assert.deepEqual(errors,[]);
 console.log('skills import dashboard ok');
}finally{await browser?.close();server.close()}
