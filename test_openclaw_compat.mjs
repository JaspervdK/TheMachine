// Load the real pinned SDK, without a Gateway or a Telegram send. Stub-only tests
// cannot detect packaging changes, missing modules, or renamed minified exports.
import {FIXTURE,ROOT} from './test-env.mjs';
import assert from 'node:assert/strict';
import {gatewayRuntime,OPENCLAW} from './openclaw-root.mjs';
import {telegramInternals} from './openclaw-plugin/change-prompt.ts';
import {readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
const version=JSON.parse(await readFile(OPENCLAW+'/package.json','utf8')).version;
assert.equal(version,'2026.10.2','Run compatibility tests with the pinned OpenClaw release');
const telegram=await telegramInternals();
for(const name of ['resolveTelegramAccount','sendMessageTelegram','getOrCreateAccountThrottler',
                  'createTelegramClientFetch','asTelegramClientFetch','resolveTelegramTransport','makeProxyFetch','Bot'])
  assert.equal(typeof telegram[name],'function',name);
assert.equal(typeof (await gatewayRuntime()).callGatewayFromCli,'function');
assert.equal(typeof (await import(OPENCLAW+'/dist/plugin-sdk/runtime-config-snapshot.js')).getRuntimeConfig,'function');
for(const file of ['runtime-config-snapshot','system-event-runtime','plugin-entry'])
  assert.ok(Object.keys(await import(OPENCLAW+'/dist/plugin-sdk/'+file+'.js')).length);
// Exercise real capability admission too, always in a separate disposable OpenClaw home.
const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!k.startsWith('OPENCLAW_')));
Object.assign(env,{HOME:FIXTURE,OPENCLAW_STATE_DIR:FIXTURE+'/state',OPENCLAW_CONFIG_PATH:FIXTURE+'/openclaw.json'});
delete env.XDG_CONFIG_HOME; delete env.XDG_DATA_HOME;
execFileSync(process.execPath,[OPENCLAW+'/openclaw.mjs','plugins','install','--link','--force',
  '--accept-capabilities',ROOT+'/openclaw-plugin'],{env,timeout:120000,stdio:'pipe'});
console.log('Pinned SDK and Telegram transport loaded; real plugin installation passed in a disposable home. No Gateway or send.');
