// Telegram's opaque callback wire format. Keep the bridge independent of
// OpenClaw's generated dist filenames, which change between releases.
function checksum(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(36).slice(0, 5).padStart(5, '0');
}

export function buildCallback(value: string) {
  return `tgcb1:${checksum(value)}:${value}`;
}
