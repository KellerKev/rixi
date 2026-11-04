# rixi-sdk — Rixi from TypeScript / JavaScript

Run commands, move files, drive long-running processes and open preview URLs on a Rixi box — a
hosted one on EU machines (rixi.eutonomous.eu), or any rixi server you run yourself. No runtime
dependencies: Node 18+ (`fetch`, WebCrypto).

```ts
import { Sandbox } from "rixi-sdk";

// RIXI_API_KEY from https://rixi.eutonomous.eu/keys
await using sb = await Sandbox.create({ size: "cpu-s", policy: { egress: { mode: "block" } } });
await sb.writeFile("main.py", "print(6 * 7)");
console.log((await sb.exec("python3 main.py")).stdout);      // 42 — the box goes at scope end

const id = await sb.spawn("npm run dev");                    // a background process
for await (const ev of sb.attach(id)) if (ev.kind === "stdout") process.stdout.write(ev.text);
const { share_url } = await sb.preview(5173);                // https://5173.b-<id>.run.eutonomous.eu/
```

`await using` needs TypeScript 5.2+; otherwise call `await sb.destroy()` in a `finally`.

## Pieces

| | |
|---|---|
| `Sandbox` | a hosted box as one object: `create({size, ttlHours, policy, modelAccess, volumes, onIdle, storage})`, `exec`, `execStream`, `spawn`, `attach`, `tasks`, `output`, `sendInput`, `kill`, files, `preview`, `stop`, `resume`, `destroy` |
| `Eutonomous` | the hosted API with your key: `launch`, `waitReady`, `box`, `release`, `stop`, `start`, `volumes`, `createVolume`, `deleteVolume`, `buckets`, `createBucket`, `setBucketExpiry`, `deleteBucket`, `s3Keys`, `createS3Key`, `revokeS3Key`, `storageInfo`, `clientFor(box)` |
| `Client` | any rixi server: `new Client({serverUrl, token, aesKey})` — the same commands, files, tasks and previews; with `aesKey` every body is sealed (AES-256-GCM), as on `rixi up` boxes |

The data plane is direct: your code talks to the box over TLS with a token for that box only.
The API key never leaves your process.

Not in this SDK yet: shipping a Pixi project with its environment (`rixi run` / `start` in the
Python SDK and CLI) and the interactive terminal (`rixi shell`).

## Develop

```bash
npm install
npm test        # builds, then runs against a real rixi server (server/.pixi env, or RIXI_SERVER_PYTHON)
```
