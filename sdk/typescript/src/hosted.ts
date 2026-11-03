// The hosted service (rixi.eutonomous.eu): boxes on EU machines, launched with an account API key.
// The key launches, tokens and shuts boxes down; the box itself is reached directly over TLS
// with a token for that box alone, so the service never carries your traffic.
import { Client, RixiError } from "./client.js";

export interface Policy {
  egress?: { mode: "open" | "allowlist" | "block"; allow?: string[] };
  ingress?: { allow?: string[] };
}

export type ModelAccess =
  | "none" | "governed"
  | { mode: "direct"; openai_base_url: string; api_key: string; anthropic_base_url?: string };

export interface LaunchOptions {
  /** cpu-s (default), cpu-l, gpu-l4, gpu-l40s */
  size?: string;
  zone?: string;
  /** hours until the box shuts itself down (service default otherwise) */
  ttlHours?: number;
  policy?: Policy;
  modelAccess?: ModelAccess;
  /** volume ids, mounted at /workspace/data/<name> */
  volumes?: string[];
  /** when idle: "release" (default) or "stop" and keep the disk */
  onIdle?: "release" | "stop";
}

export interface Box {
  id: string;
  state: string;
  live: boolean;
  url: string;
  hostname: string;
  zone: string;
  instance_type: string;
  eur_per_hour: number;
  [k: string]: any;
}

export interface Volume {
  id: string;
  name: string;
  zone: string;
  size_gb: number;
  state: string;
  box_id: string | null;
  mount: string;
  eur_per_gb_month: number;
}

export class Eutonomous {
  readonly apiUrl: string;
  private readonly apiKey: string;

  constructor(opts: { apiKey?: string; apiUrl?: string } = {}) {
    const key = opts.apiKey ?? process.env.RIXI_API_KEY;
    if (!key) throw new RixiError("an API key is needed: create one at https://rixi.eutonomous.eu/keys");
    this.apiKey = key;
    this.apiUrl = (opts.apiUrl ?? process.env.RIXI_API_URL ?? "https://rixi.eutonomous.eu")
      .replace(/\/+$/, "");
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const r = await fetch(this.apiUrl + path, {
      method, signal: AbortSignal.timeout(30_000),
      headers: { Authorization: `Bearer ${this.apiKey}`,
                 ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await r.text();
    let data: any = text;
    try { data = JSON.parse(text); } catch { /* not JSON */ }
    if (!r.ok) throw new RixiError(`${method} ${path}: ${r.status} ${data?.detail ?? text}`, r.status);
    return data as T;
  }

  templates() { return this.call<{ templates: any[] }>("GET", "/api/templates").then((d) => d.templates); }
  box(id: string) { return this.call<Box>("GET", `/api/boxes/${id}`); }
  release(id: string) { return this.call<Box>("DELETE", `/api/boxes/${id}`); }
  stop(id: string) { return this.call<Box>("POST", `/api/boxes/${id}/stop`, {}); }
  start(id: string) { return this.call<Box>("POST", `/api/boxes/${id}/start`, {}); }
  token(id: string, label = "rixi-sdk") {
    return this.call<{ token: string; expires_at: number }>("POST", `/api/boxes/${id}/tokens`, { label });
  }
  volumes() { return this.call<{ volumes: Volume[] }>("GET", "/api/volumes").then((d) => d.volumes); }
  createVolume(name: string, sizeGb: number, zone?: string) {
    return this.call<Volume>("POST", "/api/volumes", { name, size_gb: sizeGb, ...(zone ? { zone } : {}) });
  }
  deleteVolume(id: string) { return this.call<Volume>("DELETE", `/api/volumes/${id}`); }

  async launch(opts: LaunchOptions = {}): Promise<Box> {
    const body: Record<string, unknown> = { template: opts.size ?? "cpu-s" };
    if (opts.zone) body.zone = opts.zone;
    if (opts.ttlHours) body.ttl = Math.round(opts.ttlHours * 3600);
    if (opts.policy) body.policy = opts.policy;
    if (opts.modelAccess) body.model_access = opts.modelAccess;
    if (opts.volumes?.length) body.volumes = opts.volumes;
    if (opts.onIdle) body.on_idle = opts.onIdle;
    return this.call<Box>("POST", "/api/boxes", body);
  }

  /** Wait until the box is ready (about two minutes from launch). */
  async waitReady(id: string, opts: { timeoutMs?: number; pollMs?: number } = {}): Promise<Box> {
    const deadline = Date.now() + (opts.timeoutMs ?? 15 * 60_000);
    for (;;) {
      const b = await this.box(id);
      if (b.state === "ready") return b;
      if (!b.live || ["releasing", "gone", "failed"].includes(b.state)) {
        throw new RixiError(`box ${id} ended while starting: ${b.end_reason ?? b.state}`);
      }
      if (Date.now() > deadline) throw new RixiError(`box ${id} was not ready in time`);
      await new Promise((r) => setTimeout(r, opts.pollMs ?? 5000));
    }
  }

  /** A Client for a box, with box tokens minted and renewed through this API key. */
  clientFor(box: Box): Client {
    let cached: { token: string; expires_at: number } | null = null;
    return new Client({
      serverUrl: box.url,
      token: async () => {
        if (!cached || cached.expires_at - Date.now() / 1000 < 300) cached = await this.token(box.id);
        return cached.token;
      },
    });
  }
}
