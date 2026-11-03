// A throwaway hosted box as one object: create, drive, destroy.
//
//   await using sb = await Sandbox.create({ size: "cpu-s", policy: { egress: { mode: "block" } } });
//   await sb.writeFile("main.py", "print(6 * 7)");
//   console.log((await sb.exec("python3 main.py")).stdout);   // 42 — destroyed at scope end
import { Client, ExecOptions, ExecResult, OutputEvent, Preview } from "./client.js";
import { Box, Eutonomous, LaunchOptions } from "./hosted.js";

export class Sandbox {
  private destroyed = false;

  private constructor(readonly api: Eutonomous, public box: Box, readonly client: Client) {}

  static async create(opts: LaunchOptions & { apiKey?: string; apiUrl?: string } = {}):
      Promise<Sandbox> {
    const api = new Eutonomous({ apiKey: opts.apiKey, apiUrl: opts.apiUrl });
    const launched = await api.launch({ ttlHours: 2, ...opts });
    try {
      const box = await api.waitReady(launched.id);
      return new Sandbox(api, box, api.clientFor(box));
    } catch (err) {
      await api.release(launched.id).catch(() => undefined);   // nothing left billing
      throw err;
    }
  }

  get id(): string { return this.box.id; }
  get url(): string { return this.box.url; }

  exec(cmd: string | string[], opts?: ExecOptions): Promise<ExecResult> {
    return this.client.exec(cmd, opts);
  }
  execStream(cmd: string | string[], opts?: ExecOptions): AsyncGenerator<OutputEvent> {
    return this.client.execStream(cmd, opts);
  }
  spawn(cmd: string | string[], opts?: Omit<ExecOptions, "stdin">): Promise<string> {
    return this.client.spawn(cmd, opts);
  }
  attach(taskId: string, opts?: { history?: boolean }): AsyncGenerator<OutputEvent> {
    return this.client.attach(taskId, opts);
  }
  tasks() { return this.client.tasks(); }
  output(taskId: string) { return this.client.output(taskId); }
  sendInput(taskId: string, data: string | Record<string, unknown>) {
    return this.client.sendInput(taskId, data);
  }
  kill(taskId: string) { return this.client.kill(taskId); }
  writeFile(path: string, data: string | Uint8Array) { return this.client.writeFile(path, data); }
  readFile(path: string) { return this.client.readFile(path); }
  readText(path: string) { return this.client.readText(path); }
  listFiles(path?: string) { return this.client.listFiles(path); }
  preview(port: number, opts?: { public?: boolean }): Promise<Preview> {
    return this.client.openPreview(port, opts);
  }

  /** Power off, keeping the disk and its files; compute stops billing. */
  async stop(): Promise<void> { this.box = await this.api.stop(this.id); }

  /** Start a stopped sandbox again and wait until it is ready. */
  async resume(): Promise<void> {
    await this.api.start(this.id);
    this.box = await this.api.waitReady(this.id);
  }

  async destroy(): Promise<void> {
    if (this.destroyed) return;
    await this.api.release(this.id);
    this.destroyed = true;
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.destroy();
  }
}
