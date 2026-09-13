/**
 * ULID-ish sortable IDs. Not a full ULID implementation — a timestamp prefix
 * plus crypto randomness, good enough for primary keys that sort by creation.
 */
const ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"; // Crockford base32

export function ulid(now: number = Date.now()): string {
  let ts = "";
  let t = now;
  for (let i = 0; i < 10; i++) {
    ts = ENCODING[t % 32] + ts;
    t = Math.floor(t / 32);
  }
  const rand = new Uint8Array(16);
  crypto.getRandomValues(rand);
  let r = "";
  for (let i = 0; i < 16; i++) r += ENCODING[rand[i] % 32];
  return ts + r;
}

/** SHA-256 hex digest of bytes — the provenance anchor for a document. */
export async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
