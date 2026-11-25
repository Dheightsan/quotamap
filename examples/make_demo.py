"""Builds a synthetic week of transcripts and prints the quotamap report for it. All numbers are made up, nothing here is real usage.

    python examples/make_demo.py            # prints the report used in the README
"""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import quotamap as q  # noqa: E402
import synth  # noqa: E402

H = 3600.0
# area -> (turns spread over the first 42 h, turns in the last 24 h); prompt that lands the session in that area
PLAN = {
    "backend": (60, 95, "add an api endpoint for invoices"),
    "frontend": (45, 55, "fix the css of the landing page"),
    "tooling": (10, 40, "write a hook and a plugin for settings"),
    "docs": (12, 8, "update the readme and the changelog"),
    "research": (14, 6, "investigate and compare two libraries"),
}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ["QUOTAMAP_STATE_DIR"] = str(tmp / "state")
        cfg = q.load_config(ROOT / "config" / "quotamap.example.json")
        cfg["projects_dir"] = str(tmp / "projects")
        ws = q.week_start(1_800_000_000, cfg, [])
        now = ws + 66 * H
        for i, (area, (early, late, prompt)) in enumerate(PLAN.items()):
            lines = [synth.user_line(ws + 3 * H + i, prompt)]
            lines += synth.heavy_turns(ws + 4 * H, early, 42 * H / early, area[:3] + "e")
            lines += synth.heavy_turns(now - 24 * H, late, 23 * H / late, area[:3] + "l")
            synth.write_session(tmp / "projects", "demo-" + area, "s-" + area, lines)
        q.add_reading(now - 60, 38.0)
        print(q.render(q.compute(cfg, now), cfg, now))


if __name__ == "__main__":
    main()
