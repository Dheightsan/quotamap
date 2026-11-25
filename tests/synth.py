"""synthetic transcripts for the tests and the demo"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user_line(ts: float, text: str) -> dict:
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text}}


def assistant_line(ts: float, msg_id: str, model: str, inp=100, out=500, cread=0, cwrite=0, req=None) -> dict:
    return {"type": "assistant", "timestamp": iso(ts), "requestId": req or "req_" + msg_id,
            "message": {"id": msg_id, "role": "assistant", "model": model,
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread,
                                  "cache_creation_input_tokens": cwrite,
                                  "cache_creation": {"ephemeral_5m_input_tokens": cwrite, "ephemeral_1h_input_tokens": 0}}}}


def write_session(root: Path, project: str, sid: str, lines: list, sub_of: str | None = None, name: str = "") -> Path:
    """Main session: <root>/<project>/<sid>.jsonl. Subagent: <root>/<project>/<sub_of>/subagents/agent-<name>.jsonl."""
    d = root / project / sub_of / "subagents" if sub_of else root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / ((("agent-" + name) if sub_of else sid) + ".jsonl")
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    last = max(parse(x["timestamp"]) for x in lines)
    os.utime(p, (last, last))  # mtime = last message, like a real transcript
    return p


def parse(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def heavy_turns(start: float, n: int, step: float, prefix: str, model="claude-sonnet-5-5", cread=400_000, out=4_000):
    """n assistant messages `step` seconds apart, each a long-context turn (mostly cache reads)."""
    return [assistant_line(start + i * step, "%s%04d" % (prefix, i), model, inp=200, out=out, cread=cread, cwrite=2_000)
            for i in range(n)]
