import { spawnSync } from "node:child_process"

const CACHE_MS = 60_000

export function pythonFor(env, platform) {
  return env.QUOTAMAP_PYTHON || (platform === "win32" ? "python" : "python3")
}

// text printed by `quotamap.py hook`, empty when there is nothing to warn about
export function warning(script, python, run = spawnSync) {
  const r = run(python, [script, "hook"], { encoding: "utf8", timeout: 3000, windowsHide: true })
  return ((r && r.stdout) || "").trim()
}

export async function register(ctx, env = process.env, run = spawnSync, platform = process.platform, now = Date.now) {
  const script = env.QUOTAMAP_SCRIPT
  if (!script) return
  const python = pythonFor(env, platform)
  let last = { at: -Infinity, text: "" }
  await ctx.session.hook("context", (event) => {
    if (now() - last.at > CACHE_MS) last = { at: now(), text: warning(script, python, run) }
    if (last.text) event.system.push({ type: "text", text: last.text })
  })
}
