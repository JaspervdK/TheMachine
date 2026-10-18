import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(OPENCLAW+'/node_modules/playwright-core/index.mjs');

// Who's who: a review's roles with the line they came from, ✓ to confirm, Edit to change; relations and organisation roles;
// open cards ranked by who they're about.
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
const made=new Date(Date.now()-3600e3).toISOString(),older=new Date(Date.now()-7200e3).toISOString();
const suggestions=[{id:'aaaaaaaaaaaaaaaa',title:'Newsletter tweak',body:'Small thing.',status:'pending',event_ids:[1],scope:'work',created_at:made,weight:0.2},
 {id:'bbbbbbbbbbbbbbbb',title:'Answer your cofounder',body:'About the board deck.',status:'pending',event_ids:[2],scope:'work',created_at:older,weight:3.4}];
let whos=[{id:'email:pat@client.example',kind:'person',name:'Pat Doe',role:'CFO at Client Ltd',quote:'As the CFO at Client Ltd I look after the renewal',event_id:41,set_at:'2026-10-18'},
 {id:'org:client.example',kind:'org',name:'Client Ltd',role:'customer',quote:null,event_id:null,set_at:'2026-10-18'}];
let pat={id:'email:pat@client.example',kind:'person',name:'Pat Doe',strength:2,closeness:'regular',last_at:made,signals:4,ids:['pat@client.example'],links:[],
 role:'CFO at Client Ltd',role_by:'review',role_quote:'As the CFO at Client Ltd I look after the renewal',role_event_id:41,
 relations:[{a:'email:ann@client.example',a_name:'Ann Assist',relation:'assistant of',b:'email:pat@client.example',b_name:'Pat Doe',by:'review'}],open:{},recent:[],around:[]};
const org={id:'org:client.example',kind:'org',name:'Client Ltd',strength:3,last_at:made,signals:3,role:'customer',role_by:'review',links:[],open:{},recent:[],around:[],
 about:'Logistics, 51-200 people, Ghent',profile:{industry:'Logistics',about:'Client Ltd moves boxes.',linkedin_url:'https://www.linkedin.com/company/client',website:'javascript:alert(1)'}};
const calls=[],errors=[],opened=[];
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://fixture.invalid');res.setHeader('Content-Type','application/json');
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(url.pathname==='/api/status'){res.end(JSON.stringify({suggestions,promises:[],connectors:[],suggestion_counts:{pending:2},decisions:[],analyses:[],deliveries:[],review_health:{},review_runs:[]}));return}
 if(url.pathname==='/api/graph'){const node=url.searchParams.get('node');res.end(JSON.stringify(node==='org:client.example'?org:node?pat:{projects:[],people:[],quiet:[],new:[],orgs:[{...org}],whos_who:whos}));return}
 if(url.pathname==='/api/event'||url.pathname==='/api/events'){opened.push(url.search);res.end(JSON.stringify([]));return}
 if(url.pathname==='/api/action'){let body='';for await(const b of req)body+=b;const call=JSON.parse(body);calls.push(call);
  if(call.action==='graph_role'){whos=whos.filter(w=>w.id!==call.args.node_id);if(call.args.node_id===pat.id)pat={...pat,role:call.args.role,role_by:'owner'}}
  if(call.action==='graph_relation')pat={...pat,relations:[]};
  res.end('{}');return}
 if(url.pathname.startsWith('/brand/')||url.pathname.startsWith('/assets/')){res.statusCode=404;res.end();return}
 res.end('{}');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
let browser;
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage();
 await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:'+server.address().port);
 // The card about the cofounder comes first, though it is older.
 await page.locator('#task-bbbbbbbbbbbbbbbb').waitFor();
 const order=await page.locator('#suggestions article[id^="task-"]').evaluateAll(a=>a.map(x=>x.id));
 assert.deepEqual(order.slice(0,2),['task-bbbbbbbbbbbbbbbb','task-aaaaaaaaaaaaaaaa']);
 await page.locator('nav [data-tab="people"]').click();
 const list=page.locator('#people-view');
 await list.locator('.whos').first().waitFor();
 let text=await list.textContent();
 assert.ok(text.includes('Who’s who')&&text.includes('“As the CFO at Client Ltd I look after the renewal”'));
 assert.ok(text.includes('Customer · Logistics · 3 people'));  // the organisation's role and industry in its list  // the organisation's role in its list
 const widths=[];
 for(const width of [320,390,768]){
  await page.setViewportSize({width,height:844});
  const sizes=await page.evaluate(()=>({page:document.documentElement.scrollWidth,viewport:innerWidth}));
  assert.ok(sizes.page<=sizes.viewport,'overflow at '+width);widths.push({width,...sizes});
  if(process.env.ROLES_SHOT)await page.screenshot({path:process.env.ROLES_SHOT+'-'+width+'.png',fullPage:true});
 }
 await page.setViewportSize({width:390,height:844});
 await list.locator('[data-role-confirm="org:client.example"]').click();
 for(let i=0;i<100&&!calls.length;i++)await page.waitForTimeout(20);
 assert.deepEqual(calls[0],{action:'graph_role',args:{node_id:'org:client.example',role:'customer'}});
 for(let i=0;i<100&&await list.locator('.whos').count()!==1;i++)await page.waitForTimeout(20);
 assert.equal(await list.locator('.whos').count(),1);
 // Edit opens the person with the role form ready.
 await list.locator('[data-role-edit-for="email:pat@client.example"]').click();
 await page.locator('.role-form:not([hidden]) input').waitFor();
 assert.equal(await page.locator('.role-form input').inputValue(),'CFO at Client Ltd');
 text=await list.textContent();
 assert.ok(text.includes('“As the CFO at Client Ltd I look after the renewal”')&&text.includes('Ann Assist is assistant of Pat Doe'));
 await page.locator('.role-form input').fill('finance lead at Client Ltd');
 await page.locator('.role-form button').click();
 for(let i=0;i<100&&calls.length<2;i++)await page.waitForTimeout(20);
 assert.deepEqual(calls[1],{action:'graph_role',args:{node_id:'email:pat@client.example',role:'finance lead at Client Ltd'}});
 await page.locator('.role-row b',{hasText:'finance lead'}).waitFor();
 assert.equal(await page.locator('[data-role-confirm]').count(),0);  // the owner's word: nothing to confirm
 await page.locator('[data-unrelate]').click();
 for(let i=0;i<100&&calls.length<3;i++)await page.waitForTimeout(20);
 assert.deepEqual(calls[2],{action:'graph_relation',args:{node_id:'email:ann@client.example',related_to:'email:pat@client.example',relation:''}});
 for(let i=0;i<100&&await page.locator('.relation').count();i++)await page.waitForTimeout(20);
 assert.equal(await page.locator('.relation').count(),0);
 // An organisation has a role row too.
 await page.evaluate(()=>people('org:client.example'));
 await page.locator('.graph-detail h3',{hasText:'Client Ltd'}).waitFor();
 assert.ok((await page.locator('.role-row').first().textContent()).includes('To you: customer'));
 // What the enrichment service says about it, with only real links.
 const detail=await page.locator('.graph-detail').textContent();
 assert.ok(detail.includes('Logistics, 51-200 people, Ghent')&&detail.includes('Client Ltd moves boxes.'));
 assert.equal(await page.locator('.graph-detail a[href^="https://www.linkedin.com"]').count(),1);
 assert.equal(await page.locator('.graph-detail a[href^="javascript"]').count(),0);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({widths,actions:calls.length,page_errors:errors}));
}finally{if(browser)await browser.close();await new Promise(r=>server.close(r))}
