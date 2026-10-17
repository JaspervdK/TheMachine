// OpenClaw Gateway delivery through the installed SDK, not a second Telegram client.
import { gatewayRuntime } from './openclaw-root.mjs';
import { deliveryReceipt } from './telegram-receipt.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
if (!['edit', 'send'].includes(request.action) || request.channel !== 'telegram' ||
    request.accountId !== (process.env.ASSISTANT_TELEGRAM_ACCOUNT || 'default') || !process.env.ASSISTANT_TELEGRAM_OWNER || request.target !== process.env.ASSISTANT_TELEGRAM_OWNER) {
  throw new Error('Only the existing owner Telegram route is allowed');
}
let stage = 'preflight';
try {
const { callGatewayFromCli } = await gatewayRuntime();
await callGatewayFromCli('health', {timeout: '10000'}, {}, {scopes: ['operator.read']});
stage = 'invoking';
const result = await callGatewayFromCli('tools.invoke', { timeout: '40000' }, {
  name: 'message', args: request, sessionKey: 'agent:main:main',
  conversationReadOrigin: 'direct-operator',
}, { scopes: ['operator.admin', 'operator.read', 'operator.write'] });
if (!result.ok && ['not_found', 'forbidden', 'validation_error'].includes(result.error?.code)) {
  stage = 'rejected_before_execution';
}
if (!result.ok || result.output?.isError) throw new Error('Unconfirmed delivery');
const receipt = deliveryReceipt(result.output);
if (!receipt) throw new Error('Missing receipt');
process.stdout.write(JSON.stringify(receipt));
} catch {
  // No protocol text, arguments, URLs or credentials in diagnostics.
  process.stdout.write(JSON.stringify({error: stage === 'preflight' ? 'gateway_preflight_failed' :
    stage === 'rejected_before_execution' ? 'delivery_not_admitted' : 'delivery_outcome_unknown',
    definitely_not_sent: stage !== 'invoking'}));
  process.exitCode = 1;
}
