// A rixi box over HTTP: commands, files, long-running tasks and preview URLs.
import { frames, importKey, Key, objects, seal, unseal } from "./crypto.js";

export type Token = string | (() => string | Promise<string>);

export interface ClientOptions {
  /** e.g. https://b-1234abcd.run.eutonomous.eu, or http://127.0.0.1:9000 */
  serverUrl: string;
  /** a bearer token, or a function returning a fresh one per request */
  token?: Token;
  /** base64 of the 32-byte AES key, for boxes that seal their traffic (rixi up) */
  aesKey?: string;
  /** per-request timeout in ms for calls that are not streams (default 30 s) */
  timeoutMs?: number;
}

export interface ExecOptions {
  cwd?: string;
  env?: Record<string, string>;
  /** seconds before the command is killed (default 600 on the box) */
  timeout?: number;
  stdin?: string;
}

export interface ExecResult {
  exitCode: number | null;
  status: string;          // completed | failed | timeout
  stdout: string;
  stderr: string;
  durationS: number | null;
  ok: boolean;
}

export type OutputEvent =
  | { kind: "stdout"; text: string }
  | { kind: "stderr"; text: string }
  | { kind: "exit"; exitCode: number | null; status?: string };

export interface TaskInfo {
  status: string;
  task_name: string;
  kind: "task" | "exec";
  exit_code: number | null;
  started_at: number;
}

export interface Preview {
  port: number;
  public: boolean;
  url: string;
  share_url: string;
  created_at: number;
}

export interface FileEntry {
  name: string;
  type: "file" | "dir";
  size: number;
}

export class RixiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "RixiError";
  }
}

const enc = new TextEncoder();
const dec = new TextDecoder();

export class Client {
  readonly serverUrl: string;
  private readonly token?: Token;
  private readonly keyPromise: Promise<Key>;
  private readonly timeoutMs: number;

  constructor(opts: ClientOptions) {
    this.serverUrl = opts.serverUrl.replace(/\/+$/, "");
    this.token = opts.token;
    this.keyPromise = importKey(opts.aesKey);
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  // ── plumbing ────────────────────────────────────────────────────────────
  private async headers(extra: Record<string, string> = {}): Promise<Record<string, string>> {
    const t = typeof this.token === "function" ? await this.token() : this.token;
    return t ? { Authorization: `Bearer ${t}`, ...extra } : { ...extra };
  }

  private async request(method: string, path: string, init: {
    body?: Uint8Array; headers?: Record<string, string>; stream?: boolean;
    query?: Record<string, string>;
  } = {}): Promise<Response> {
    const url = new URL(this.serverUrl + path);
    for (const [k, v] of Object.entries(init.query ?? {})) url.searchParams.set(k, v);
    const r = await fetch(url, {
      method, body: init.body as BodyInit | undefined, headers: await this.headers(init.headers),
      signal: init.stream ? undefined : AbortSignal.timeout(this.timeoutMs),
    });
    if (!r.ok) {
      let detail = await r.text();
      try { const j = JSON.parse(detail); detail = j.detail ?? j.error ?? detail; } catch { /* text */ }
      throw new RixiError(`${method} ${path} failed: ${r.status} ${detail}`, r.status);
    }
    return r;
  }

  /** A JSON body, sealed when the box has a key. */
  private async jsonBody(obj: unknown): Promise<{ body: Uint8Array; headers: Record<string, string> }> {
    const key = await this.keyPromise;
    const raw = enc.encode(JSON.stringify(obj));
    if (key) {
      return { body: await seal(key, raw),
               headers: { "X-Rixi-Encrypted": "1", "Content-Type": "application/octet-stream" } };
    }
    return { body: raw, headers: { "Content-Type": "application/json" } };
  }

  private async bytes(r: Response): Promise<Uint8Array> {
    const data = new Uint8Array(await r.arrayBuffer());
    if (r.headers.get("X-Rixi-Encrypted") === "1") {
      const key = await this.keyPromise;
      if (!key) throw new RixiError("the box sealed its answer but this client has no key");
      return unseal(key, data);
    }
    return data;
  }

  private async json<T>(r: Response): Promise<T> {
    return JSON.parse(dec.decode(await this.bytes(r))) as T;
  }

  // ── health ──────────────────────────────────────────────────────────────
  async health(): Promise<Record<string, any>> {
    return (await this.request("GET", "/health")).json();
  }

  // ── commands ────────────────────────────────────────────────────────────
  /** Run a command in the workspace and wait for it. A string runs through bash. */
  async exec(cmd: string | string[], opts: ExecOptions = {}): Promise<ExecResult> {
    const out: ExecResult = { exitCode: null, status: "", stdout: "", stderr: "", durationS: null,
                              ok: false };
    for await (const ev of this.execStream(cmd, opts)) {
      if (ev.kind === "stdout") out.stdout += ev.text;
      else if (ev.kind === "stderr") out.stderr += ev.text;
      else { out.exitCode = ev.exitCode; out.status = ev.status ?? ""; }
    }
    out.ok = out.exitCode === 0;
    return out;
  }

  /** Run a command and receive its output as it arrives, then its exit code. */
  async *execStream(cmd: string | string[], opts: ExecOptions = {}): AsyncGenerator<OutputEvent> {
    const { body, headers } = await this.jsonBody({ cmd, ...opts });
    const r = await this.request("POST", "/exec", { body, headers, stream: true });
    const key = await this.keyPromise;
    for await (const payload of frames(key, r.body!)) {
      for (const o of objects(payload)) {
        if ("stdout" in o) yield { kind: "stdout", text: o.stdout };
        else if ("stderr" in o) yield { kind: "stderr", text: o.stderr };
        else if ("exit_code" in o) yield { kind: "exit", exitCode: o.exit_code, status: o.status };
      }
    }
  }

  // ── long-running work ───────────────────────────────────────────────────
  /** Start a command in the background; returns its task id at once. */
  async spawn(cmd: string | string[], opts: Omit<ExecOptions, "stdin"> & { stdin?: string } = {}):
      Promise<string> {
    const { body, headers } = await this.jsonBody({ cmd, background: true, ...opts });
    return (await this.json<{ task_id: string }>(
      await this.request("POST", "/exec", { body, headers }))).task_id;
  }

  async tasks(): Promise<Record<string, TaskInfo>> {
    return (await this.request("GET", "/tasks")).json();
  }

  async task(id: string): Promise<Record<string, any>> {
    return (await this.request("GET", `/task/${encodeURIComponent(id)}`)).json();
  }

  /** Everything a task has printed so far. */
  async output(id: string): Promise<string> {
    const r = await (await this.request("GET", `/task/${encodeURIComponent(id)}/output`)).json();
    return (r.output_history ?? []).map((e: { content: string }) => e.content + "\n").join("");
  }

  /** Follow a task until it ends (replaying earlier output unless history is false). Stop
   *  iterating to detach; the task keeps running. */
  async *attach(id: string, opts: { history?: boolean } = {}): AsyncGenerator<OutputEvent> {
    const r = await this.request("GET", `/task/${encodeURIComponent(id)}/stream`, {
      stream: true, query: { history: String(opts.history ?? true) } });
    const key = await this.keyPromise;
    for await (const payload of frames(key, r.body!)) {
      for (const o of objects(payload)) {
        if ("output" in o) yield { kind: "stdout", text: o.output };
        else if ("stderr" in o) yield { kind: "stderr", text: o.stderr };
        else if ("exit_code" in o) yield { kind: "exit", exitCode: o.exit_code };
      }
    }
  }

  /** Write to a task's stdin. A string goes as-is (add the newline a line reader needs);
   *  an object goes as one JSON line. */
  async sendInput(id: string, data: string | Record<string, unknown>): Promise<void> {
    const key = await this.keyPromise;
    const raw = typeof data === "string";
    let body: Uint8Array = enc.encode(raw ? data : JSON.stringify(data));
    let ctype = raw ? "text/plain" : "application/json";
    if (key) { body = await seal(key, body); ctype = "application/octet-stream"; }
    await this.request("POST", `/task/${encodeURIComponent(id)}/input`, {
      body, headers: { "Content-Type": ctype }, query: raw ? { raw: "1" } : {} });
  }

  async restart(id: string): Promise<void> {
    await this.request("POST", `/task/${encodeURIComponent(id)}/restart`);
  }

  async kill(id: string): Promise<void> {
    await this.request("DELETE", `/task/${encodeURIComponent(id)}`);
  }

  // ── files ───────────────────────────────────────────────────────────────
  async writeFile(path: string, data: string | Uint8Array): Promise<{ path: string; bytes: number }> {
    const key = await this.keyPromise;
    let body: Uint8Array = typeof data === "string" ? enc.encode(data) : data;
    const headers: Record<string, string> = { "Content-Type": "application/octet-stream" };
    if (key) { body = await seal(key, body); headers["X-Rixi-Encrypted"] = "1"; }
    return (await this.request("PUT", "/files", { body, headers, query: { path } })).json();
  }

  async readFile(path: string): Promise<Uint8Array> {
    return this.bytes(await this.request("GET", "/files", { query: { path } }));
  }

  async readText(path: string): Promise<string> {
    return dec.decode(await this.readFile(path));
  }

  async listFiles(path = "."): Promise<FileEntry[]> {
    return (await this.json<{ entries: FileEntry[] }>(
      await this.request("GET", "/files/list", { query: { path } }))).entries;
  }

  async deleteFile(path: string): Promise<void> {
    await this.request("DELETE", "/files", { query: { path } });
  }

  // ── preview URLs (hosted / named boxes) ─────────────────────────────────
  /** Serve a port at https://<port>.<box>/ — private (share link) unless public. */
  async openPreview(port: number, opts: { public?: boolean } = {}): Promise<Preview> {
    const { body, headers } = await this.jsonBody({ port, public: !!opts.public });
    return this.json<Preview>(await this.request("POST", "/previews", { body, headers }));
  }

  async previews(): Promise<Preview[]> {
    return this.json<Preview[]>(await this.request("GET", "/previews"));
  }

  async closePreview(port: number): Promise<void> {
    await this.request("DELETE", `/previews/${port}`);
  }
}
