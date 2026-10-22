// Keys, tokens and passwords never stay in the agent's session transcripts. OpenClaw keeps every tool result and
// reply on disk in the clear (they are not sealed like the app's messages, and backups and exports carry them), and
// later turns read them back. When a tool printed something shaped like a credential (a provider's token, a JWT, a
// private key, a "password=" or "Authorization:" value, a cookie header), only a placeholder is written. This is about
// what is kept, not what the agent may do: the turn that ran the tool is not changed. Codex-native tool records are
// written by Codex itself and are not covered.
const SHAPES: [RegExp, string][] = [
  [/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g, '[redacted private key]'],
  [/\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, '[redacted token]'],
  [/\b(?:xox[abcdeoprs]|xapp)-[A-Za-z0-9-]{10,}/g, '[redacted token]'],
  [/\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,})/g, '[redacted token]'],
  [/\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}/g, '[redacted token]'],
  [/\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}/g, '[redacted token]'],
  [/\bya29\.[A-Za-z0-9_-]{20,}|\b1\/\/0[A-Za-z0-9_-]{20,}/g, '[redacted token]'],
  [/\bAKIA[0-9A-Z]{16}\b/g, '[redacted token]'],
  [/\b\d{8,10}:AA[A-Za-z0-9_-]{30,}/g, '[redacted token]'],
  [/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}/gi, '$1 [redacted]'],
  [/\b(authorization|proxy-authorization|x-api-key)(\s*:\s*)(?!(?:Bearer|Basic)\s)[^\s"']{8,}/gi, '$1$2[redacted]'],
  [/\b((?:set-)?cookie)(\s*:\s*)[^\r\n"']+/gi, '$1$2[redacted]'],
  [/\b(password|passwd|passphrase)(["']?\s*[:=]\s*["']?)(?!\[redacted)[^\s"'`,;&<>]{6,}/gi, '$1$2[redacted]'],
  [/\b(client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|auth[_-]?token|id[_-]?token)(["']?\s*[:=]\s*["']?)(?!\[redacted)[^\s"'`,;&<>]{8,}/gi, '$1$2[redacted]'],
];

export function redactSecrets(text: string): string {
  let out = text;
  for (const [shape, replacement] of SHAPES) out = out.replace(shape, replacement);
  return out;
}

const scrub = (value: any): any => typeof value === 'string' ? redactSecrets(value)
  : Array.isArray(value) ? value.map(scrub)
  : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, scrub(v)])) : value;

// Only what a person or the model reads: text parts and tool-call arguments. Thinking blocks, signatures and ids stay
// exactly as they are (a provider checks them when the session is replayed).
export function redactMessage(message: any): any {
  if (!message || typeof message !== 'object') return message;
  let changed = false;
  const part = (item: any) => {
    if (typeof item === 'string') {
      const clean = redactSecrets(item);
      changed ||= clean !== item;
      return clean;
    }
    if (item?.type === 'text' && typeof item.text === 'string') {
      const clean = redactSecrets(item.text);
      if (clean === item.text) return item;
      changed = true;
      return { ...item, text: clean };
    }
    if (item?.type === 'toolCall' && item.arguments && typeof item.arguments === 'object') {
      const clean = scrub(item.arguments);
      if (JSON.stringify(clean) === JSON.stringify(item.arguments)) return item;
      changed = true;
      return { ...item, arguments: clean };
    }
    return item;
  };
  const content = Array.isArray(message.content) ? message.content.map(part) : part(message.content);
  return changed ? { ...message, content } : message;
}

export function registerSecretRedaction(api: any) {
  const keep = (event: any) => {
    const message = redactMessage(event?.message);
    return message !== event?.message ? { message } : undefined;
  };
  api.on('tool_result_persist', keep);
  api.on('before_message_write', keep);
}
