import assert from 'node:assert/strict';
import { readFile, writeFile, mkdtemp, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const fixtureDir=await mkdtemp(join(tmpdir(),'assistant-handler-'));
// Exercise the bridge against the installed Telegram decoder, independently
// of our encoder. Generated chunk names are discovered only in this test.
import {OPENCLAW} from './openclaw-root.mjs';
const coreDist=join(OPENCLAW,'dist');
const callbackChunk=(await readdir(coreDist)).find(name=>/^native-command-callback-data-.*\.mjs$/.test(name));
assert.ok(callbackChunk,'installed Telegram callback codec is available');
const {a:decodeCallback}=await import(join(coreDist,callbackChunk));
let source=await readFile(new URL('./openclaw-plugin/index.ts',import.meta.url),'utf8');
source=source.replace("import { buildCallback } from './telegram-callback.ts';",`import { buildCallback } from ${JSON.stringify(new URL('./openclaw-plugin/telegram-callback.ts',import.meta.url).pathname)};`);
const start=source.indexOf('async function settings()'),end=source.indexOf('export default definePluginEntry');
source=source.slice(0,start)+'async function settings(){return globalThis.fixtureConfig;}\nfunction ownerChat(){return String(globalThis.fixtureConfig.telegram_owner);}\nasync function call(action,args={}){return globalThis.fixtureCall(action,args); }\n'+source.slice(end);
for(const line of ["import { readFile } from 'node:fs/promises';","import { homedir } from 'node:os';","import { join } from 'node:path';"])source=source.replace(line,'');
source=source.replace("import { sendChangePrompt } from './change-prompt.ts';",'const sendChangePrompt=async()=>"456";');
source=source.replace("import { registerAndroidFeed } from './android-feed.ts';",'const registerAndroidFeed=()=>{};');
source=source.replace("import { registerHeartbeatFailureGate } from './heartbeat-failure-gate.ts';",'const registerHeartbeatFailureGate=()=>{};');
source=source.replace("import { registerReviewRecovery, currentReviewRun } from './review-recovery.ts';",'const registerReviewRecovery=()=>{};const currentReviewRun=()=>undefined;');
source=source.replace("import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';",'const definePluginEntry=v=>v;').replace('export default definePluginEntry','globalThis.fixturePlugin=definePluginEntry');
const fixturePath=join(fixtureDir,'handler.ts');await writeFile(fixturePath,source);
const id='aaaaaaaaaaaaaaaa';let handler, beforeDispatch, choices=[], edits=[], replies=[];
globalThis.fixtureConfig={telegram_owner:'1000001',dashboard_host:'machine.example.ts.net',dashboard_path:'/assistant/'};
let item={id,status:'snoozed'};
globalThis.fixtureCall=async(action,args)=>{if(action==='refresh_card')return {queued:true};if(action==='status')return {suggestions:[item],deliveries:[{suggestion_id:id,message_id:'123',previous_message_ids:'["old"]'}]};choices.push(args);if(action==='decide')item.status=args.choice==='complete'?'completed':args.choice==='reject'?'rejected':args.choice==='snooze'?'snoozed':'accepted';return {status:item.status,snoozed_until:'2026-10-10T07:00:00Z'};};
await import(fixturePath);
globalThis.fixturePlugin.register({runtime:{config:{current:()=>({})}},registerTool(){},registerInteractiveHandler(h){handler=h.handler},on(name,h){if(name==='before_dispatch')beforeDispatch=h},registerCommand(){}});
const ctx=(choice,message='123')=>({auth:{isAuthorizedSender:true},isGroup:false,accountId:'default',senderId:'1000001',callback:{data:`assistant:${id}:${choice}`,chatId:'1000001',messageId:message},callbackId:'unique',respond:{editButtons:async x=>edits.push(x),reply:async x=>replies.push(x),editMessage:async x=>edits.push(x),clearButtons:async()=>{}}});
await handler(ctx('snooze'));assert.equal(choices.length,0);let buttons=edits.pop().buttons.flat();assert.equal(buttons.length,6);assert.ok(buttons.some(b=>b.url?.startsWith('https://machine.example.ts.net/assistant/#snooze-custom=')));assert.ok(buttons.filter(b=>b.callback_data).every(b=>Buffer.byteLength(b.callback_data)<=64&&decodeCallback(b.callback_data)));
await handler(ctx('back'));assert.deepEqual(edits.pop().buttons.flat().map(b=>b.text),['Bring back','Change time','Not needed']);
await handler(ctx('snooze_tomorrow','old'));assert.equal(choices.pop().snooze_option,'tomorrow');
item.status='prepared';const repliesBeforeDone=replies.length;await handler(ctx('complete'));assert.equal(choices.pop().choice,'complete');assert.deepEqual(edits.pop().buttons,[]);assert.equal(replies.length,repliesBeforeDone);
item.status='completed';await handler(ctx('accept'));assert.equal(choices.length,0);assert.equal(replies.length,repliesBeforeDone);
item.status='pending';await handler(ctx('back'));assert.equal(edits.pop().buttons.flat()[0].text,'Do it');
item.status='completed';item.undo_until=new Date(Date.now()+300000).toISOString();await handler(ctx('undo'));assert.equal(choices.pop().choice,'undo');
item.status='accepted';item.preparation_status='blocked';await handler(ctx('back'));assert.equal(edits.pop().buttons.flat()[0].text,'Try again');item.preparation_reason='Sign in to Stripe in the open tab, then tap Continue.';await handler(ctx('back'));assert.equal(edits.pop().buttons.flat()[0].text,'Continue');delete item.preparation_reason;delete item.preparation_status;delete item.undo_until;
const unauthorized=ctx('accept');unauthorized.senderId='someone-else';await handler(unauthorized);assert.equal(choices.length,0);
item.status='pending';const baseline=replies.length;await handler(ctx('accept'));choices.pop();await handler(ctx('reject'));choices.pop();item.status='pending';await handler(ctx('snooze_2h'));choices.pop();assert.equal(replies.length,baseline);
item.status='pending';await handler(ctx('other'));const prompt=choices.pop();assert.equal(prompt.card_message_id,'123');assert.equal(prompt.managed_prompt,true);assert.equal(choices.length,0);assert.equal(replies.length,baseline);
{const saved=globalThis.fixtureCall;globalThis.fixtureCall=async(action,args)=>action==='request_feedback'?{awaiting_feedback:true,duplicate:true,earlier_prompt:'uncertain'}:saved(action,args);
item.status='pending';const before=replies.length;await handler(ctx('other'));await handler(ctx('other'));assert.equal(replies.length,before);globalThis.fixtureCall=saved;}
item.status='prepared';item.next_action={kind:'email',revision:'abcdef123456',from:'owner@example.com',to:['recipient@example.com'],cc:[],bcc:[],subject:'Verified subject',body:'Exact draft'};
await handler(ctx('back'));buttons=edits.pop().buttons.flat();assert.equal(buttons[0].text,'Send');assert.ok(buttons.every(b=>!b.callback_data||Buffer.byteLength(b.callback_data)<=64));
const beforeSend=choices.length;await handler(ctx('send_abcdef123456'));const review=edits.pop();assert.match(review.text,/From: owner@example.com/);assert.match(review.text,/Exact draft/);assert.equal(choices.length,beforeSend);
await handler(ctx('confirm_abcdef123456'));assert.equal(choices.pop().revision,'abcdef123456');edits.pop();
await handler(ctx('confirm_000000000000'));assert.equal(choices.length,beforeSend);edits.pop();delete item.next_action;
item.next_action={kind:'email',revision:'abcdef123456',from:'owner@example.com',to:[],to_options:['will@vendor.example','ann@example.com'],cc:[],bcc:[],subject:'Pick',body:'Draft'};
{await handler(ctx('send_abcdef123456'));const pick=edits.pop();assert.match(pick.text,/Who should get this email/);const b=pick.buttons.flat();
 assert.deepEqual(b.map(x=>x.text),['Send to will@vendor.example','Send to ann@example.com','Choose several','Cancel']);assert.ok(b.every(x=>!x.callback_data||Buffer.byteLength(x.callback_data)<=64));
 const before=choices.length;await handler(ctx('confirm_abcdef123456'));assert.equal(choices.length,before);edits.pop();  // An old plain Send now shows the choice again.
 await handler(ctx('confirm_abcdef123456_5'));assert.equal(choices.length,before);edits.pop();
 await handler(ctx('confirm_abcdef123456_1'));assert.deepEqual(choices.pop().recipients,['ann@example.com']);edits.pop();}
delete item.next_action;
item.next_actions=[{kind:'email',label:'Send reply',revision:'abcdef123456',from:'owner@example.com',to:['recipient@example.com'],subject:'Exact reply',body:'Approved text'},
 {kind:'prepare',label:'Investigate further',revision:'fedcba654321',instructions:'Check the exact affected mapping locally'},
 {kind:'open',label:'Open the Zap',revision:'111111111111',url:'https://example.com/zap'}];
await handler(ctx('back'));const menu=edits.pop().buttons;assert.ok(menu.every(row=>row.length<=3));assert.deepEqual(menu.flat().slice(0,2).map(b=>b.text),['Send reply','Open the Zap']);
const beforeWrongKind=choices.length;await handler(ctx('send_fedcba654321'));await handler(ctx('next_abcdef123456'));assert.equal(choices.length,beforeWrongKind);
item.next_actions=item.next_actions.slice(1);
await handler(ctx('next_fedcba654321'));assert.equal(choices.pop().revision,'fedcba654321');assert.ok(!edits.pop().buttons.flat().some(b=>b.text==='Investigate further'));
{const saved=globalThis.fixtureCall;globalThis.fixtureCall=async(action,args)=>{if(action==='confirm_action')throw Object.assign(new Error('Newer result'),{status:400});return saved(action,args)};
 await handler(ctx('confirm_abcdef123456'));globalThis.fixtureCall=saved;}
delete item.next_actions;
item.next_action={kind:'email_demo',label:'Send',revision:'abcdef123456',from:'the-machine@example.invalid',to:['alex@example.invalid'],subject:'Demo reply',body:'Thanks for the proposal.'};
await handler(ctx('send_abcdef123456'));const demoPreview=edits.pop();assert.match(demoPreview.text,/nothing will be sent/);assert.deepEqual(demoPreview.buttons.flat().map(b=>b.text),['Back']);
const demoChoices=choices.length;await handler(ctx('confirm_abcdef123456'));assert.equal(choices.length,demoChoices);delete item.next_action;
delete item.next_action;
item.status='pending';await handler(ctx('reject'));assert.equal(choices.length,0);assert.deepEqual(edits.pop().buttons.flat().map(b=>b.text),['Not relevant','Wrong facts','Other reason…','No reason','Cancel']);
await handler(ctx('dismiss_handled'));{const d=choices.pop();assert.equal(d.choice,'reject');assert.equal(d.feedback,'Already handled');}
item.status='pending';await handler(ctx('dismiss_none'));{const d=choices.pop();assert.equal(d.choice,'reject');assert.equal(d.feedback,'');}
item.status='pending';await handler(ctx('dismiss_other'));{const d=choices.pop();assert.equal(d.kind,'dismiss');assert.equal(d.managed_prompt,true);}
item.status='pending';item.snooze_choices=[['2h','2 hours'],['tomorrow','Monday · 9:00'],['1w','1 week · 9:00']];await handler(ctx('snooze'));assert.deepEqual(edits.pop().buttons.flat().map(b=>b.text),['2 hours','Monday · 9:00','1 week · 9:00','Choose date','Cancel']);delete item.snooze_choices;
const countBeforeStale=choices.length;
item.status='instructions';await handler(ctx('undo'));assert.equal(choices.length,countBeforeStale);assert.equal(edits.pop().buttons.flat().at(-1).text,'Not needed');
item.status='completed';item.undo_until=new Date(Date.now()-1000).toISOString();await handler(ctx('undo'));assert.equal(choices.length,countBeforeStale);assert.deepEqual(edits.pop().buttons,[]);
item.status='pending';await handler(ctx('complete'));await handler(ctx('retry'));assert.equal(choices.length,countBeforeStale);
const savedCall=globalThis.fixtureCall;globalThis.fixtureCall=async(action,args)=>{if(action==='decide')throw Object.assign(new Error('A newer decision replaced this action'),{status:400});return savedCall(action,args)};
item.status='completed';item.undo_until=new Date(Date.now()+300000).toISOString();await handler(ctx('undo'));assert.equal(choices.length,countBeforeStale);assert.equal(edits.pop().buttons.flat()[0].text,'Undo');
globalThis.fixtureCall=async(action,args)=>{if(action==='decide')throw Object.assign(new Error('Server unavailable'),{status:503});return savedCall(action,args)};
await assert.rejects(handler(ctx('undo')),/Server unavailable/);
globalThis.fixtureCall=async(action,args)=>{assert.equal(action,'feedback_reply');assert.equal(args.timestamp,1791614021000);assert.equal(args.reply_to_id,'2982');return {handled:true};};
const handled=await beforeDispatch({content:'I fixed it already',timestamp:1791614021000},{channelId:'telegram',accountId:'default',senderId:'1000001',conversationId:'1000001',sessionKey:'agent:main:main',messageId:'2984',replyToId:'2982'});assert.deepEqual(handled,{handled:true});
console.log('Telegram handler: snooze menu, typed callback sizes, old recorded cards, completion and ownership passed.');

await rm(fixtureDir,{recursive:true,force:true});
