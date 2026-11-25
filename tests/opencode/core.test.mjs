import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { spawnSync } from "node:child_process"
import { pythonFor, register, warning } from "../../opencode/quotamap/core.mjs"

function fakeCtx() {
  const hooks = {}
  return { hooks, session: { hook: async (name, cb) => { hooks[name] = cb } } }
}
const runReturning = (stdout) => () => ({ stdout })

test("python name depends on the platform unless overridden", () => {
  assert.equal(pythonFor({}, "win32"), "python")
  assert.equal(pythonFor({}, "linux"), "python3")
  assert.equal(pythonFor({ QUOTAMAP_PYTHON: "py" }, "linux"), "py")
})

test("without QUOTAMAP_SCRIPT nothing is registered", async () => {
  const ctx = fakeCtx()
  await register(ctx, {})
  assert.deepEqual(Object.keys(ctx.hooks), [])
})

test("the context hook adds the warning to the system parts", async () => {
  const ctx = fakeCtx()
  await register(ctx, { QUOTAMAP_SCRIPT: "x.py" }, runReturning("quotamap: area backend is over budget\n"), "linux")
  const event = { system: [] }
  ctx.hooks.context(event)
  assert.deepEqual(event.system, [{ type: "text", text: "quotamap: area backend is over budget" }])
})

test("no output means the model context is left alone", async () => {
  const ctx = fakeCtx()
  await register(ctx, { QUOTAMAP_SCRIPT: "x.py" }, runReturning(""), "linux")
  const event = { system: [] }
  ctx.hooks.context(event)
  assert.deepEqual(event.system, [])
})

test("the script runs once per minute, not once per request", async () => {
  const ctx = fakeCtx()
  let calls = 0
  let t = 1000
  const run = () => { calls++; return { stdout: "quotamap: w" } }
  await register(ctx, { QUOTAMAP_SCRIPT: "x.py" }, run, "linux", () => t)
  ctx.hooks.context({ system: [] })
  ctx.hooks.context({ system: [] })
  t += 61_000
  ctx.hooks.context({ system: [] })
  assert.equal(calls, 2)
})

test("a failing python does not break the request", () => {
  assert.equal(warning("x.py", "python", () => ({ stdout: null, error: new Error("ENOENT") })), "")
})

test("end to end with the real script and a seeded cache", async () => {
  const state = mkdtempSync(join(tmpdir(), "qm-"))
  writeFileSync(join(state, "hook_cache.json"), JSON.stringify({ t: Date.now() / 1000, msg: "quotamap: seeded warning" }))
  const script = resolve("scripts/quotamap.py")
  const python = process.platform === "win32" ? "python" : "python3"
  const env = { ...process.env, QUOTAMAP_STATE_DIR: state }
  const probe = spawnSync(python, [script, "hook"], { env, encoding: "utf8" })
  assert.equal(probe.stdout.trim(), "quotamap: seeded warning")
  Object.assign(process.env, { QUOTAMAP_STATE_DIR: state, QUOTAMAP_SCRIPT: script })
  const ctx = fakeCtx()
  await register(ctx)
  const event = { system: [] }
  ctx.hooks.context(event)
  assert.equal(event.system[0].text, "quotamap: seeded warning")
})
