#!/usr/bin/env python3
"""quotamap: where the weekly Claude Code quota goes, by area.

usage: python quotamap.py {report,hook,calibrate,record,statusline,refresh,init}
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOUR = 3600.0
WEEK_H = 168.0
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# weight per model family, sonnet = 1
DEFAULT_WEIGHTS = {"opus": 2.0, "sonnet": 1.0, "haiku": 0.33, "fable": 4.0, "default": 1.0}
# units per million tokens: input, output, cache read, write 5m, write 1h
UNIT_IN, UNIT_OUT, UNIT_READ, UNIT_W5M, UNIT_W1H = 3.0, 15.0, 0.3, 3.75, 6.0

DEFAULT_CONFIG = {
    "reset": {"weekday": "sun", "hour_utc": 19},
    "projects_dir": None,
    "weights": DEFAULT_WEIGHTS,
    "warn": {
        "min_hours_into_week": 6,        # too noisy before this
        "projection_margin_pct": 10,     # warn at 100 + margin
        "area_margin": 1.3,              # area over budget by this factor
        "min_area_pct": 2,
        "min_calibration_pct": 3,        # smaller readings round too much
        "max_calibration_age_h": 48,
        "hook_cache_fresh_min": 60,
        "hook_cache_show_h": 6,
    },
    "fallback_area": "other",
    "areas": {"everything": {"budget_pct": 100, "keywords": []}},
}


def state_dir() -> Path:
    env = os.environ.get("QUOTAMAP_STATE_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "quotamap"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "quotamap"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "quotamap"


def config_path() -> Path:
    env = os.environ.get("QUOTAMAP_CONFIG")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "quotamap" / "config.json"


def load_config(path: Path | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = path or config_path()
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for k, v in user.items():
        if k in ("warn", "weights", "reset") and isinstance(v, dict):
            cfg[k].update(v)
        elif not k.startswith("_"):
            cfg[k] = v
    return cfg


def projects_dir(cfg: dict) -> Path:
    return Path(cfg["projects_dir"]).expanduser() if cfg.get("projects_dir") else Path.home() / ".claude" / "projects"


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def week_start(now: float, cfg: dict, readings: list) -> float:
    """epoch of the last weekly reset; the statusline value beats the config"""
    for r in reversed(readings):
        ra = r.get("resets_at")
        if ra:
            base = ra - 7 * 86400
            return base + 7 * 86400 * ((now - base) // (7 * 86400))
    wd = WEEKDAYS.index(str(cfg["reset"]["weekday"])[:3].lower())
    hour = int(cfg["reset"]["hour_utc"])
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    d -= dt.timedelta(days=(d.weekday() - wd) % 7)
    if d.timestamp() > now:
        d -= dt.timedelta(days=7)
    return d.timestamp()


def weight_for(model, weights: dict) -> float:
    m = (model or "").lower()
    for k, w in weights.items():
        if k != "default" and k in m:
            return float(w)
    return float(weights.get("default", 1.0))


def row_units(u: dict, model, weights: dict) -> float:
    cc = u.get("cache_creation") or {}
    w1h, w5m = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
    if w1h is None and w5m is None:
        w1h, w5m = 0, u.get("cache_creation_input_tokens") or 0
    tokens = ((u.get("input_tokens") or 0) * UNIT_IN + (u.get("output_tokens") or 0) * UNIT_OUT
              + (u.get("cache_read_input_tokens") or 0) * UNIT_READ + (w5m or 0) * UNIT_W5M + (w1h or 0) * UNIT_W1H)
    return tokens * weight_for(model, weights) / 1e6


SYSTEM_RE = re.compile(r"<system-reminder>.*?</system-reminder>|<command-[a-z-]+>.*?</command-[a-z-]+>"
                       r"|This session is being continued from a previous conversation[^\n]*", re.S)


def prompt_text(content) -> str:
    if not isinstance(content, str):
        content = " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")
    return SYSTEM_RE.sub(" ", content).strip()


def parse_ts(s) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def scan_file(path: str, weights: dict, want_prompt: bool, floor_ts: float) -> dict:
    prompt, rows = None, []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {"prompt": None, "rows": []}
    with fh:
        for n, line in enumerate(fh):
            is_asst = '"assistant"' in line and '"usage"' in line
            if not is_asst and not (want_prompt and prompt is None and '"user"' in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or {}
            if e.get("type") == "user":
                if prompt is None and not e.get("isMeta"):
                    c = msg.get("content")
                    if "tool_result" in str(c)[:300]:
                        continue
                    t = prompt_text(c)
                    if t:
                        prompt = t[:3000]
                continue
            if e.get("type") != "assistant" or not msg.get("usage"):
                continue
            ts = parse_ts(e.get("timestamp"))
            if ts is None or ts < floor_ts:
                continue
            key = "%s|%s" % (msg.get("id"), e.get("requestId")) if msg.get("id") else "%s:%d" % (path, n)
            rows.append([ts, row_units(msg["usage"], msg.get("model"), weights), key])
    return {"prompt": prompt, "rows": rows}


def scan(cfg: dict, floor_ts: float, cache_path: Path | None = None) -> list:
    """sessions touched since floor_ts; parsed files cached by size and mtime"""
    root = projects_dir(cfg)
    cache_path = cache_path or state_dir() / "scan_cache.json"
    cache = read_json(cache_path, {})
    if cache.get("_weights") != cfg["weights"] or cache.get("_floor", floor_ts) > floor_ts:
        cache = {"_weights": cfg["weights"]}
    cache["_floor"] = floor_ts
    sessions, seen = [], set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("tool-results", "memory")]
        for f in filenames:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < floor_ts:
                continue
            rel = os.path.relpath(p, root).split(os.sep)
            project = rel[0]
            is_sub = "subagents" in rel
            sid = rel[1] if is_sub and len(rel) > 1 else Path(p).stem
            hit = cache.get(p)
            if hit and hit["size"] == st.st_size and hit["mtime"] == st.st_mtime:
                data = hit["data"]
            else:
                data = scan_file(p, cfg["weights"], not is_sub, floor_ts)
                cache[p] = {"size": st.st_size, "mtime": st.st_mtime, "data": data}
            seen.add(p)
            sessions.append({"sid": sid, "project": project, "sub": is_sub, "prompt": data["prompt"], "rows": data["rows"]})
    for p in [k for k in cache if not k.startswith("_") and k not in seen]:
        del cache[p]
    try:
        write_json(cache_path, cache)
    except OSError:
        pass
    return sessions


TAG_RE = re.compile(r"\[area:\s*([^\]]+?)\s*\]", re.I)


def classify(prompt: str | None, project: str, cfg: dict) -> str:
    areas = cfg["areas"]
    text = (prompt or "")[:3000]
    m = TAG_RE.search(text)
    if m and m.group(1) in areas:
        return m.group(1)
    low = text.lower()
    for name, a in areas.items():
        if any(s.lower() in project.lower() for s in a.get("paths", [])):
            return name
    for name, a in areas.items():
        if any(re.search(k, low) for k in a.get("keywords", [])):
            return name
    return cfg["fallback_area"]


def rows_by_area(sessions: list, cfg: dict, since: float) -> list:
    """(ts, units, area) rows; subagents go to the parent area, duplicates count once"""
    area_of = {}
    for s in sessions:
        if not s["sub"]:
            area_of[(s["project"], s["sid"])] = classify(s["prompt"], s["project"], cfg)
    seen, out = set(), []
    for s in sorted(sessions, key=lambda s: s["sub"]):
        area = area_of.get((s["project"], s["sid"]), cfg["fallback_area"])
        for ts, units, key in s["rows"]:
            if ts < since or key in seen:
                continue
            seen.add(key)
            out.append((ts, units, area))
    return out


def usable_readings(readings: list, ws: float, now: float, warn: dict) -> list:
    lo = warn["min_calibration_pct"]
    max_age = warn["max_calibration_age_h"] * HOUR
    return [r for r in readings if ws <= r["t"] <= now and r["pct"] >= lo and now - r["t"] <= max_age]


def find_factor(rows: list, readings: list, ws: float, now: float, warn: dict):
    """units per 1 %, median of the last 3 readings of this week, or None"""
    ok = usable_readings(readings, ws, now, warn)[-3:]
    fs = []
    for r in ok:
        spent = sum(u for t, u, _ in rows if t <= r["t"])
        if spent > 0:
            fs.append(spent / r["pct"])
    if not fs:
        return None, None
    fs.sort()
    return fs[len(fs) // 2], ok[-1]


def analyze(rows: list, readings: list, cfg: dict, now: float) -> dict:
    warn = cfg["warn"]
    ws = week_start(now, cfg, readings)
    hours = max((now - ws) / HOUR, 0.01)
    left = WEEK_H - hours
    total = sum(u for _, u, _ in rows)
    by_area = collections.Counter()
    for _, u, a in rows:
        by_area[a] += u
    out = {"week_start": ws, "hours_in": hours, "units": total, "units_by_area": dict(by_area),
           "calibrated": False, "alerts": []}
    factor, last = find_factor(rows, readings, ws, now, warn)
    if factor is None:
        return out
    pct = total / factor
    last24 = sum(u for t, u, _ in rows if t >= now - 86400) / factor
    rate = last24 / min(24.0, hours)
    proj = pct + rate * left
    out.update(calibrated=True, factor=factor, calibrated_at=last["t"], pct=pct, rate=rate, even_rate=100 / WEEK_H,
               projection=proj, reset_at=ws + WEEK_H * HOUR,
               pct_by_area={a: u / factor for a, u in by_area.items()},
               runs_out_at=(now + (100 - pct) / rate * HOUR) if rate > 0 and pct < 100 else None)
    if hours >= warn["min_hours_into_week"] and proj >= 100 + warn["projection_margin_pct"]:
        out["alerts"].append("at this pace the weekly quota runs out around %s (projection %.0f %% at reset). "
                             "To land at 100 %%, hold %.2f %%/h instead of %.2f."
                             % (fmt_time(out["runs_out_at"]) if out["runs_out_at"] else "before reset", proj,
                                max(100 - pct, 0) / left, rate))
    frac = hours / WEEK_H
    for a, p in sorted(out["pct_by_area"].items(), key=lambda kv: -kv[1]):
        budget = cfg["areas"].get(a, {}).get("budget_pct")
        if budget is not None and p > warn["min_area_pct"] and p > budget * max(frac, 0.15) * warn["area_margin"]:
            out["alerts"].append("area '%s' is at %.1f %% of the week and its budget is %g %%." % (a, p, budget))
    return out


def fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%a %d %H:%M UTC")


def render(res: dict, cfg: dict, now: float) -> str:
    L = ["Week since %s (%.1f h in)" % (fmt_time(res["week_start"]), res["hours_in"])]
    if not res["calibrated"]:
        L.append("Not calibrated this week: no usable official reading, so no % and no projection.")
        L.append("Give one: quotamap calibrate <weekly % shown by your plan>, or wire the statusline (README). Best at 3 % or more.")
        tot = res["units"] or 1
        L.append("Share of this week's usage by area (cost units %.0f):" % res["units"])
        for a, u in sorted(res["units_by_area"].items(), key=lambda kv: -kv[1]):
            L.append("  %-26s %5.1f %%" % (a, 100 * u / tot))
        return "\n".join(L)
    age = (now - res["calibrated_at"]) / HOUR
    L[0] += ": %.1f %% of the weekly quota (calibrated %.0f h ago, %.1f units per 1 %%)" % (res["pct"], age, res["factor"])
    L.append("Pace last 24 h: %.2f %%/h (even pace would be %.2f). Projection at reset (%s): %.0f %%."
             % (res["rate"], res["even_rate"], fmt_time(res["reset_at"]), res["projection"]))
    proj, margin = res["projection"], cfg["warn"]["projection_margin_pct"]
    L.append("The quota lasts to the reset at this pace." if proj <= 100 else
             "Borderline: the projection is slightly over 100 %." if proj < 100 + margin else
             "The quota does NOT last to the reset at this pace.")
    L.append("By area (% of the weekly quota | budget):")
    for a, p in sorted(res["pct_by_area"].items(), key=lambda kv: -kv[1]):
        b = cfg["areas"].get(a, {}).get("budget_pct")
        L.append("  %-26s %5.1f %% | %s" % (a, p, ("%g %%" % b) if b is not None else "-"))
    for x in res["alerts"]:
        L.append("Warning: " + x)
    return "\n".join(L)


def load_readings() -> list:
    return read_json(state_dir() / "readings.json", [])


def add_reading(t: float, pct: float, resets_at=None, min_gap_s: float = 0) -> bool:
    rs = load_readings()
    if rs and min_gap_s and t - rs[-1]["t"] < min_gap_s and abs(rs[-1]["pct"] - pct) < 1:
        return False
    rs.append({"t": t, "pct": float(pct), **({"resets_at": resets_at} if resets_at else {})})
    cut = t - 21 * 86400
    write_json(state_dir() / "readings.json", [r for r in rs if r["t"] >= cut][-300:])
    return True


def compute(cfg: dict, now: float) -> dict:
    readings = load_readings()
    ws = week_start(now, cfg, readings)
    sessions = scan(cfg, ws)
    return analyze(rows_by_area(sessions, cfg, ws), readings, cfg, now)


def refresh(cfg: dict, now: float) -> None:
    res = compute(cfg, now)
    msg = " ".join("quotamap: " + a for a in res["alerts"])
    write_json(state_dir() / "hook_cache.json", {"t": now, "msg": msg})


def spawn_refresh() -> None:
    lock = state_dir() / "refresh.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 600:
            return
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "refresh"], **kw)


def hook(cfg: dict, now: float) -> str:
    """cached warning; starts a background refresh when the cache is old"""
    warn = cfg["warn"]
    c = read_json(state_dir() / "hook_cache.json", {})
    age = now - c.get("t", 0)
    if age >= warn["hook_cache_fresh_min"] * 60:
        spawn_refresh()
    return c.get("msg", "") if age < warn["hook_cache_show_h"] * HOUR else ""


def statusline_fields(data: dict):
    w = ((data.get("rate_limits") or {}).get("seven_day")) or {}
    pct = w.get("used_percentage")
    ra = w.get("resets_at")
    if isinstance(ra, str):
        ra = parse_ts(ra)
    return (float(pct) if isinstance(pct, (int, float)) else None), ra


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="quotamap", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="path to the config JSON (default: QUOTAMAP_CONFIG or the user config folder)")
    ap.add_argument("--now", help="ISO time to use as 'now' (testing and screenshots)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    sub.add_parser("hook")
    c = sub.add_parser("calibrate")
    c.add_argument("pct", type=float)
    for name in ("record", "statusline", "refresh", "init"):
        sub.add_parser(name)
    a = ap.parse_args(argv)
    cfg = load_config(Path(a.config) if a.config else None)
    now = parse_ts(a.now) if a.now else time.time()
    cmd = a.cmd or "report"

    if cmd == "report":
        res = compute(cfg, now)
        print(json.dumps(res, indent=1, default=str) if getattr(a, "json", False) else render(res, cfg, now))
    elif cmd == "hook":
        try:
            txt = hook(cfg, now)
        except Exception:
            txt = ""
        if txt:
            print(txt)
    elif cmd == "refresh":
        try:
            refresh(cfg, now)
        finally:
            (state_dir() / "refresh.lock").unlink(missing_ok=True)
    elif cmd == "calibrate":
        if not 0 < a.pct <= 100:
            print("pct must be between 0 and 100", file=sys.stderr)
            return 2
        add_reading(now, a.pct)
        res = compute(cfg, now)
        print("Recorded %.1f %%." % a.pct)
        print(render(res, cfg, now))
    elif cmd in ("record", "statusline"):
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        pct, ra = statusline_fields(data)
        if pct is not None:
            add_reading(now, pct, ra, min_gap_s=600)
        if cmd == "statusline":
            five = ((data.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
            parts = []
            if pct is not None:
                parts.append("week %.0f %%" % pct)
            if isinstance(five, (int, float)):
                parts.append("5h %.0f %%" % five)
            print(" | ".join(parts))
    elif cmd == "init":
        src = Path(__file__).resolve().parent.parent / "config" / "quotamap.example.json"
        dst = config_path()
        if dst.exists():
            print("Already exists: %s" % dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print("Wrote %s. Edit areas and budgets there." % dst)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
