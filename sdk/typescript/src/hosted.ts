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
  /** instead of size: the cheapest box with at least this */
  require?: { vcpu?: number; ram_gb?: number; gpu?: string; gpu_ram_gb?: number; zone?: string };
  /** S3 on the box (AWS_* variables, a key that dies with it): true for all your buckets */
  storage?: true | { buckets?: string[]; write?: boolean };
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

export interface Bucket {
  id: string;
  name: string;
  region: string;
  state: string;
  size_gb: number;
  objects: number;
  egress_gb: number;
  expire_days: number | null;
  created_at: number;
  [k: string]: any;
}

export interface S3Key {
  access_key: string;
  name: string;
  buckets: string[] | null;
  write: boolean;
  box_id: string | null;
  /** only when the key is created */
  secret_key?: string;
  endpoint_url?: string;
  region?: string;
  [k: string]: any;
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

  // S3 storage: buckets reached at `endpoint_url` with keys from createS3Key
  storageInfo() {
    return this.call<{ endpoint_url: string; regions: string[] }>("GET", "/api/buckets")
      .then((d) => ({ endpoint_url: d.endpoint_url, regions: d.regions }));
  }
  buckets() { return this.call<{ buckets: Bucket[] }>("GET", "/api/buckets").then((d) => d.buckets); }
  async bucket(nameOrId: string): Promise<Bucket> {
    const b = (await this.buckets()).find((x) => x.name === nameOrId || x.id === nameOrId);
    if (!b) throw new RixiError(`no bucket ${nameOrId}`, 404);
    return b;
  }
  createBucket(name: string, opts: { region?: string; expireDays?: number } = {}) {
    return this.call<Bucket>("POST", "/api/buckets", {
      name, ...(opts.region ? { region: opts.region } : {}),
      ...(opts.expireDays ? { expire_days: opts.expireDays } : {}) });
  }
  async setBucketExpiry(nameOrId: string, expireDays: number | null) {
    const b = await this.bucket(nameOrId);
    return this.call<Bucket>("PATCH", `/api/buckets/${b.id}`, { expire_days: expireDays });
  }
  /** Without force the bucket must be empty; with it, everything in it is deleted. */
  async deleteBucket(nameOrId: string, opts: { force?: boolean } = {}) {
    const b = await this.bucket(nameOrId);
    return this.call<Bucket>("DELETE", `/api/buckets/${b.id}${opts.force ? "?force=1" : ""}`);
  }
  s3Keys() { return this.call<{ keys: S3Key[] }>("GET", "/api/s3-keys").then((d) => d.keys); }
  /** The answer carries secret_key, shown this once. */
  createS3Key(name: string, opts: { buckets?: string[]; write?: boolean } = {}) {
    return this.call<S3Key>("POST", "/api/s3-keys", {
      name, write: opts.write ?? true, ...(opts.buckets?.length ? { buckets: opts.buckets } : {}) });
  }
  revokeS3Key(accessKey: string) { return this.call<S3Key>("DELETE", `/api/s3-keys/${accessKey}`); }

  async launch(opts: LaunchOptions = {}): Promise<Box> {
    const body: Record<string, unknown> = opts.require && !opts.size
      ? { require: opts.require } : { template: opts.size ?? "cpu-s" };
    if (opts.zone) body.zone = opts.zone;
    if (opts.ttlHours) body.ttl = Math.round(opts.ttlHours * 3600);
    if (opts.policy) body.policy = opts.policy;
    if (opts.modelAccess) body.model_access = opts.modelAccess;
    if (opts.volumes?.length) body.volumes = opts.volumes;
    if (opts.onIdle) body.on_idle = opts.onIdle;
    if (opts.storage) body.storage = opts.storage === true ? {} : opts.storage;
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
