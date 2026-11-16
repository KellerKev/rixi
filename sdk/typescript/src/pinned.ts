// HTTPS to a box that has a certificate of its own making (`rixi up`): the one certificate the
// profile holds is trusted, whatever address the box is reached at, and no other — not even one
// a certificate authority signed. Node only (a browser cannot be told which certificate to trust).

export interface PinnedInit {
  method: string;
  headers: Record<string, string>;
  body?: Uint8Array;
  signal?: AbortSignal;
}

export async function pinnedFetch(cert: string, url: URL, init: PinnedInit): Promise<Response> {
  if (url.protocol !== "https:") throw new Error("tlsCert needs an https:// serverUrl");
  const https = await import("node:https");
  const { Readable } = await import("node:stream");
  return new Promise<Response>((resolve, reject) => {
    const req = https.request(url, {
      method: init.method, headers: init.headers, signal: init.signal,
      ca: cert,                               // this certificate, instead of the usual authorities
      checkServerIdentity: () => undefined,   // it names no address; holding its key is the check
    }, (res) => {
      const headers = new Headers();
      for (const [name, value] of Object.entries(res.headers)) {
        if (Array.isArray(value)) value.forEach((v) => headers.append(name, v));
        else if (value !== undefined) headers.set(name, value);
      }
      const status = res.statusCode ?? 502;
      const empty = init.method === "HEAD" || status === 204 || status === 304;
      if (empty) res.resume();
      resolve(new Response(empty ? null : Readable.toWeb(res) as unknown as ReadableStream,
                           { status, headers }));
    });
    req.on("error", reject);
    if (init.body) req.write(init.body);
    req.end();
  });
}
