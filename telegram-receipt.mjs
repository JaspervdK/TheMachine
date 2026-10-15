// The installed message contract permits direct IDs and result.messageId.
// Inspect only documented result envelopes, not arbitrary strings or history.
export function deliveryReceipt(output) {
  const candidates = [output?.details];
  for (const item of output?.content || []) {
    if (item.type === 'text') { try { candidates.push(JSON.parse(item.text)); } catch {} }
  }
  for (const payload of candidates) {
    if (!payload || payload.ok === false || payload.sentBeforeError ||
        ['failed', 'partial_failed', 'suppressed'].includes(payload.deliveryStatus)) continue;
    const result = payload.result ?? payload;
    if (result?.ok === false || result?.sentBeforeError ||
        ['failed', 'partial_failed', 'suppressed'].includes(result?.deliveryStatus)) continue;
    const id = result.messageId ?? result.message_id;
    if ((typeof id === 'string' && /^\d+$/.test(id)) || (Number.isSafeInteger(id) && id > 0)) {
      return {messageId: String(id)};
    }
  }
  return null;
}
