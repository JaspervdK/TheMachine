// Installed Gateway SDK. This bridge permits only a notification snapshot of the one paired
// Android node named in the account's `node_id` (passed as the first argument).
import {gatewayRuntime} from './openclaw-root.mjs';
const approvedNode = process.argv[2] || '';
try {
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(approvedNode)) throw new Error('android_identity_mismatch');
  const {callGatewayFromCli} = await gatewayRuntime();
  const inventory = await callGatewayFromCli('node.list', {timeout: '15000'}, {}, {scopes: ['operator.read']});
  const node = inventory.nodes?.find(n => n.nodeId === approvedNode);
  if (!node || node.platform !== 'android' || !node.paired) throw new Error('android_identity_mismatch');
  if (!node.connected) throw new Error('android_offline');
  if (!node.commands?.includes('notifications.list') || node.permissions?.notificationListener !== true)
    throw new Error('android_notifications_unavailable');
  const result = await callGatewayFromCli('node.invoke', {timeout: '20000'}, {
    nodeId: approvedNode, command: 'notifications.list', params: {}, timeoutMs: 15000,
    idempotencyKey: crypto.randomUUID()
  }, {scopes: ['operator.read', 'operator.write']});
  if (!result.ok || result.nodeId !== approvedNode || result.command !== 'notifications.list')
    throw new Error('android_notifications_unavailable');
  const payload = result.payload ?? JSON.parse(result.payloadJSON);
  console.log(JSON.stringify(payload));
} catch (error) {
  const allowed = ['android_identity_mismatch', 'android_offline', 'android_notifications_unavailable'];
  console.error(allowed.includes(error?.message) ? error.message : 'android_gateway_unavailable');
  process.exitCode = 1;
}
