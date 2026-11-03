// The wire format shared with the Python SDK and the rixi server:
//   frame = len(4, big-endian) || payload,  payload = nonce(12) || AES-256-GCM(data) with a key
// Without a key the payload is the plain data (still length-prefixed).

const NONCE_LEN = 12;

export type Key = CryptoKey | null;

export async function importKey(base64Key?: string | null): Promise<Key> {
  if (!base64Key) return null;
  const raw = Uint8Array.from(Buffer.from(base64Key, "base64"));
  if (raw.length !== 32) throw new Error("the AES key must be 32 bytes (base64)");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function seal(key: Key, data: Uint8Array): Promise<Uint8Array> {
  if (!key) return data;
  const nonce = crypto.getRandomValues(new Uint8Array(NONCE_LEN));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key,
                                                        data as BufferSource));
  const out = new Uint8Array(NONCE_LEN + ct.length);
  out.set(nonce, 0);
  out.set(ct, NONCE_LEN);
  return out;
}

export async function unseal(key: Key, blob: Uint8Array): Promise<Uint8Array> {
  if (!key) return blob;
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: blob.subarray(0, NONCE_LEN) as BufferSource }, key,
    blob.subarray(NONCE_LEN) as BufferSource);
  return new Uint8Array(pt);
}

/** Decode a stream of length-prefixed frames into decrypted payloads. */
export async function* frames(key: Key, body: ReadableStream<Uint8Array>): AsyncGenerator<Uint8Array> {
  const reader = body.getReader();
  let buf = new Uint8Array(0);
  let need: number | null = null;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      const merged = new Uint8Array(buf.length + value.length);
      merged.set(buf, 0);
      merged.set(value, buf.length);
      buf = merged;
      for (;;) {
        if (need === null) {
          if (buf.length < 4) break;
          need = new DataView(buf.buffer, buf.byteOffset, 4).getUint32(0, false);
          buf = buf.subarray(4);
        }
        if (buf.length < need) break;
        const enc = buf.subarray(0, need);
        buf = buf.subarray(need);
        need = null;
        yield await unseal(key, enc);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

const decoder = new TextDecoder();

/** Concatenated JSON objects inside one decrypted payload. */
export function* objects(payload: Uint8Array): Generator<Record<string, any>> {
  let text = decoder.decode(payload).trim();
  while (text) {
    let depth = 0, inStr = false, esc = false, end = -1;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (inStr) {
        if (esc) esc = false;
        else if (c === "\\") esc = true;
        else if (c === '"') inStr = false;
      } else if (c === '"') inStr = true;
      else if (c === "{") depth++;
      else if (c === "}" && --depth === 0) { end = i + 1; break; }
    }
    if (end < 0) return;
    yield JSON.parse(text.slice(0, end));
    text = text.slice(end).trim();
  }
}
