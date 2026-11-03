// The SDK against a real rixi server process — plain, and with an AES key.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import net from "node:net";
import { Client, RixiError } from "../dist/index.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SERVER_PY = process.env.RIXI_SERVER_PYTHON ??
  join(ROOT, "server", ".pixi", "envs", "default", "bin", "python");

const freePort = () => new Promise((resolve) => {
  const s = net.createServer().listen(0, "127.0.0.1", () => {
    const { port } = s.address(); s.close(() => resolve(port));
  });
});

async function startServer(extra = []) {
  const port = await freePort();
  const ws = mkdtempSync(join(tmpdir(), "rixi-ws-"));
  const proc = spawn(SERVER_PY, [join(ROOT, "server", "rixi_server.py"), "--port", String(port),
    "--workspace", ws, "--log-dir", mkdtempSync(join(tmpdir(), "rixi-log-")), ...extra],
    { cwd: join(ROOT, "server"), stdio: "ignore" });
  const url = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${url}/health`)).ok) return { proc, url, ws }; } catch { /* booting */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  proc.kill();
  throw new Error("server did not start");
}

const servers = [];
let plain, sealed, aesKey;
before(async () => {
  plain = await startServer();
  aesKey = randomBytes(32).toString("base64");
  const kf = join(mkdtempSync(join(tmpdir(), "rixi-key-")), "aes.key");
  writeFileSync(kf, aesKey);
  sealed = await startServer(["--aes-key", kf, "--require-encryption"]);
  servers.push(plain.proc, sealed.proc);
});
after(() => servers.forEach((p) => p.kill()));

async function collect(gen) {
  let out = "", err = "", end = null;
  for await (const ev of gen) {
    if (ev.kind === "stdout") out += ev.text;
    else if (ev.kind === "stderr") err += ev.text;
    else end = ev;
  }
  return { out, err, end };
}

async function agentLoop(c) {
  await c.writeFile("job/main.py", "import json\nprint(json.dumps({'answer': 6 * 7}))\n");
  const r = await c.exec("python3 main.py > out.json && echo done >&2", { cwd: "job" });
  assert.equal(r.exitCode, 0);
  assert.equal(r.stderr, "done\n");
  assert.equal((await c.readText("job/out.json")).trim(), '{"answer": 42}');
  assert.deepEqual((await c.listFiles("job")).map((e) => e.name).sort(), ["main.py", "out.json"]);
  const failed = await c.exec(["python3", "-c", "import sys; print('x'); sys.exit(3)"]);
  assert.equal(failed.exitCode, 3); assert.equal(failed.stdout, "x\n"); assert.equal(failed.ok, false);

  const tid = await c.spawn("read who; echo hi $who; sleep 0.3; echo done; echo warn >&2");
  assert.equal((await c.tasks())[tid].status, "running");
  await c.sendInput(tid, "ada\n");
  const first = await collect(c.attach(tid));
  assert.equal(first.out, "hi ada\ndone\n"); assert.equal(first.err, "warn\n");
  assert.equal(first.end.exitCode, 0);
  assert.match(await c.output(tid), /hi ada/);
  assert.equal((await collect(c.attach(tid, { history: false }))).out, "");
  await c.restart(tid);
  await c.sendInput(tid, "bob\n");
  assert.equal((await collect(c.attach(tid))).out, "hi bob\ndone\n");
  await c.kill(tid);
  assert.equal(tid in (await c.tasks()), false);
  await c.deleteFile("job");
}

test("an agent loop and long-running work, plain", async () => {
  await agentLoop(new Client({ serverUrl: plain.url }));
});

test("the same, sealed with the AES key", async () => {
  await agentLoop(new Client({ serverUrl: sealed.url, aesKey }));
});

test("a sealed box refuses a client without the key", async () => {
  const c = new Client({ serverUrl: sealed.url });
  await assert.rejects(c.exec("true"), (e) => e instanceof RixiError && e.status === 400);
});

test("previews need a named box", async () => {
  const c = new Client({ serverUrl: plain.url });
  await assert.rejects(c.openPreview(8000), (e) => e instanceof RixiError && e.status === 404);
});
