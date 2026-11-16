// The SDK against a real rixi server process — plain, and with an AES key.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
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

/** A certificate as `rixi up` makes one for a box: [key path, cert path, cert PEM]. */
function boxCertificate() {
  const dir = mkdtempSync(join(tmpdir(), "rixi-tls-"));
  const out = spawnSync(SERVER_PY, ["-c", `
import sys
sys.path.insert(0, ${JSON.stringify(join(ROOT, "src"))})
from rixi.cloud.profiles import generate_tls_cert
key, cert = generate_tls_cert("box1")
open(sys.argv[1] + "/k.pem", "w").write(key)
open(sys.argv[1] + "/c.pem", "w").write(cert)
`, dir]);
  assert.equal(out.status, 0, String(out.stderr));
  return [join(dir, "k.pem"), join(dir, "c.pem"), readFileSync(join(dir, "c.pem"), "utf8")];
}

async function startServer(extra = [], tls = null) {
  const port = await freePort();
  const ws = mkdtempSync(join(tmpdir(), "rixi-ws-"));
  const proc = spawn(SERVER_PY, [join(ROOT, "server", "rixi_server.py"), "--port", String(port),
    "--workspace", ws, "--log-dir", mkdtempSync(join(tmpdir(), "rixi-log-")), ...extra,
    ...(tls ? ["--tls-cert", tls[1], "--tls-key", tls[0]] : [])],
    { cwd: join(ROOT, "server"), stdio: "ignore" });
  const url = `${tls ? "https" : "http"}://127.0.0.1:${port}`;
  const probe = new Client({ serverUrl: url, tlsCert: tls ? tls[2] : undefined });
  for (let i = 0; i < 100; i++) {
    try { await probe.health(); return { proc, url, ws }; } catch { /* booting */ }
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


test("a box with a certificate of its own is reached with that certificate and no other", async () => {
  const [key, certPath, cert] = boxCertificate();
  const [, , otherCert] = boxCertificate();              // same name, another key
  const kf = join(mkdtempSync(join(tmpdir(), "rixi-key-")), "aes.key");
  const aes = randomBytes(32).toString("base64");
  writeFileSync(kf, aes);
  const box = await startServer(["--aes-key", kf, "--require-encryption"], [key, certPath, cert]);
  servers.push(box.proc);
  const c = new Client({ serverUrl: box.url, aesKey: aes, tlsCert: cert });
  await agentLoop(c);                                    // everything, over the pinned connection
  await assert.rejects(() => new Client({ serverUrl: box.url, aesKey: aes, tlsCert: otherCert }).health());
  await assert.rejects(() => new Client({ serverUrl: box.url, aesKey: aes }).health());   // nobody signed it
  assert.throws(() => new Client({ serverUrl: "http://127.0.0.1:9000", tlsCert: cert }), RixiError);
});
