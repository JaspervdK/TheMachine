import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {OPENCLAW} from './openclaw-root.mjs';
const {chromium}=await import(OPENCLAW+'/node_modules/playwright-core/index.mjs');

// Settings, Signatures: one editable signature per mailbox, a preview that runs nothing and loads nothing remote,
// "Find in sent mail" for Outlook, and Save. All mocked.
const dashboard=await readFile(new URL('./dashboard.html',import.meta.url),'utf8');
let signatures=[{account_id:'work-outlook',email:'owner@example.com',service:'outlook',html:'<b>Owner Name</b>',source:'detected',saveable:true},
 {account_id:'work-gmail',email:'owner@example.org',service:'gmail',html:'',source:'',saveable:true}];
const posts=[];
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://fixture.invalid');res.setHeader('Content-Type','application/json');
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end(dashboard);return}
 if(url.pathname==='/api/status'){res.end(JSON.stringify({suggestions:[],promises:[],suggestion_counts:{},decisions:[],analyses:[],deliveries:[],review_health:{},connectors:[],review_runs:[],signatures}));return}
 if(url.pathname==='/api/action'){let body='';for await(const c of req)body+=c;const {action,args}=JSON.parse(body);posts.push({action,args});
  if(action==='detect_signature'){res.end(JSON.stringify({html:'<p>Owner Name<br>Example Inc</p>',found:true}));return}
  if(action==='set_signature'){signatures=signatures.map(s=>s.account_id===args.account_id?{...s,html:args.html,source:'owner'}:s);res.end(JSON.stringify(signatures));return}}
 res.statusCode=404;res.end('{}');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const errors=[];let browser;
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage();
 const remote=[];
 await context.route('**/*',route=>{const u=new URL(route.request().url());if(u.hostname==='127.0.0.1')return route.continue();remote.push(u.href);return route.abort()});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:'+server.address().port);
 await page.locator('#open-settings').click();
 const forms=page.locator('#signatures .sig-form');await forms.first().waitFor();
 assert.equal(await forms.count(),2);
 assert.match(await forms.nth(0).textContent(),/Outlook · Found in your sent mail/);
 assert.match(await forms.nth(1).textContent(),/Gmail · Gmail’s own/);
 assert.equal(await forms.nth(1).locator('[data-detect]').count(),0);
 const frame=forms.nth(0).locator('iframe');
 assert.equal(await frame.getAttribute('sandbox'),'');
 assert.match(await frame.getAttribute('srcdoc'),/<b>Owner Name<\/b>/);
 // Whatever is typed, the preview runs no script and fetches nothing from elsewhere.
 await forms.nth(0).locator('textarea').fill('<b>Typed</b><img src="https://tracker.invalid/p.png"><script>parent.hacked=1</script>');
 assert.match(await frame.getAttribute('srcdoc'),/Typed/);
 await page.waitForTimeout(300);
 assert.equal(await page.evaluate(()=>window.hacked),undefined);
 assert.deepEqual(remote,[]);
 await forms.nth(0).locator('[data-detect]').click();
 await forms.nth(0).locator('[role=status]',{hasText:'Found. Check it, then Save.'}).waitFor();
 assert.equal(await forms.nth(0).locator('textarea').inputValue(),'<p>Owner Name<br>Example Inc</p>');
 await forms.nth(0).locator('.primary').click();
 await page.locator('#notice',{hasText:'Signature saved.'}).waitFor({state:'attached'});
 assert.deepEqual(posts.map(p=>p.action),['detect_signature','set_signature']);
 assert.deepEqual(posts[1].args,{account_id:'work-outlook',html:'<p>Owner Name<br>Example Inc</p>'});
 await page.locator('#signatures .sig-form',{hasText:'Set by you'}).waitFor();
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);
 console.log('signatures dashboard ok');
}finally{await browser?.close();server.close()}
