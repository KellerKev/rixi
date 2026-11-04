// The hosted API and Sandbox over a fake service (fetch is replaced; no network).
import { test } from "node:test";
import assert from "node:assert/strict";
import { Eutonomous, Sandbox } from "../dist/index.js";

function fakeService() {
  const calls = [];
  const box = { id: "bx1", state: "booting", live: true, url: "https://b-bx1.run.test",
                hostname: "b-bx1.run.test", zone: "fr-par-2", instance_type: "DEV1-M",
                eur_per_hour: 0.02 };
  let polls = 0;
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url); const method = init.method ?? "GET";
    calls.push({ method, path: u.pathname, body: init.body ? JSON.parse(init.body) : undefined,
                 auth: init.headers?.Authorization });
    const json = (o, status = 200) => new Response(JSON.stringify(o), { status,
      headers: { "Content-Type": "application/json" } });
    if (u.pathname === "/api/boxes" && method === "POST") return json(box, 202);
    if (u.pathname === "/api/boxes/bx1" && method === "GET") {
      polls += 1; return json({ ...box, state: polls > 1 ? "ready" : "booting" });
    }
    if (u.pathname === "/api/boxes/bx1" && method === "DELETE") return json({ ...box, state: "releasing" });
    if (u.pathname === "/api/boxes/bx1/tokens") return json({ token: "box-token", expires_at: Date.now() / 1000 + 3600 });
    if (u.pathname === "/api/volumes" && method === "POST") return json({ id: "v1", ...JSON.parse(init.body) }, 202);
    if (u.pathname === "/api/buckets" && method === "GET")
      return json({ buckets: [{ id: "bk1", name: "results", region: "fr-par" }],
                    endpoint_url: "https://s3.test", regions: ["fr-par"] });
    if (u.pathname === "/api/buckets" && method === "POST") return json({ id: "bk2", ...JSON.parse(init.body) }, 201);
    if (u.pathname === "/api/buckets/bk1") return json({ id: "bk1", name: "results", state: "deleting" });
    if (u.pathname === "/api/s3-keys" && method === "POST")
      return json({ access_key: "RXK", secret_key: "sec", ...JSON.parse(init.body) }, 201);
    return json({ detail: "nope" }, 404);
  };
  return calls;
}

test("a sandbox launches with its options, waits, and is destroyed at scope end", async () => {
  const calls = fakeService();
  // what `await using sb = …` compiles to: the dispose hook runs at the end of the scope
  const sb = await Sandbox.create({ apiKey: "rxk_test", apiUrl: "https://rixi.test",
    size: "gpu-l4", policy: { egress: { mode: "block" } }, volumes: ["v1"], onIdle: "stop" });
  try {
    assert.equal(sb.url, "https://b-bx1.run.test");
    const launch = calls.find((c) => c.method === "POST" && c.path === "/api/boxes");
    assert.deepEqual(launch.body, { template: "gpu-l4", ttl: 7200, policy: { egress: { mode: "block" } },
                                    volumes: ["v1"], on_idle: "stop" });
    assert.equal(launch.auth, "Bearer rxk_test");
  } finally {
    await sb[Symbol.asyncDispose]();
  }
  assert.ok(calls.some((c) => c.method === "DELETE" && c.path === "/api/boxes/bx1"));
});

test("a missing key says where to get one", () => {
  delete process.env.RIXI_API_KEY;
  assert.throws(() => new Eutonomous(), /rixi.eutonomous.eu\/keys/);
});

test("volumes", async () => {
  const calls = fakeService();
  const api = new Eutonomous({ apiKey: "rxk_test", apiUrl: "https://rixi.test" });
  const v = await api.createVolume("data", 50, "fr-par-2");
  assert.equal(v.id, "v1");
  assert.deepEqual(calls.at(-1).body, { name: "data", size_gb: 50, zone: "fr-par-2" });
});

test("buckets, keys, and storage on a box", async () => {
  const calls = fakeService();
  const api = new Eutonomous({ apiKey: "rxk_test", apiUrl: "https://rixi.test" });
  assert.equal((await api.storageInfo()).endpoint_url, "https://s3.test");
  await api.createBucket("logs", { expireDays: 7 });
  assert.deepEqual(calls.at(-1).body, { name: "logs", expire_days: 7 });
  await api.deleteBucket("results", { force: true });
  assert.equal(calls.at(-1).method, "DELETE");
  await assert.rejects(api.deleteBucket("nope"), /no bucket nope/);
  const k = await api.createS3Key("ci", { buckets: ["results"], write: false });
  assert.equal(k.secret_key, "sec");
  assert.deepEqual(calls.at(-1).body, { name: "ci", write: false, buckets: ["results"] });
  await api.launch({ storage: true });
  assert.deepEqual(calls.at(-1).body, { template: "cpu-s", storage: {} });
});
