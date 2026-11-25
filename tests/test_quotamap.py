import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

import quotamap as q  # noqa: E402
import synth  # noqa: E402

H = 3600.0
CFG = q.load_config(HERE.parent / "config" / "quotamap.example.json")


def week_start_for(now):
    return q.week_start(now, CFG, [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.state = Path(self.tmp.name) / "state"
        env = mock.patch.dict(os.environ, {"QUOTAMAP_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["projects_dir"] = str(self.root)
        # fixed "now": 66 h into a week
        self.ws = week_start_for(1_800_000_000)
        self.now = self.ws + 66 * H

    def session(self, project, sid, prompt, start, n, prefix, **kw):
        lines = [synth.user_line(start - 5, prompt)] + synth.heavy_turns(start, n, 60, prefix, **kw)
        return synth.write_session(self.root, project, sid, lines)

    def rows(self):
        sessions = q.scan(self.cfg, self.ws)
        return q.rows_by_area(sessions, self.cfg, self.ws)


class WeekTests(unittest.TestCase):
    def test_week_start_is_a_sunday_19_utc(self):
        import datetime as dt
        d = dt.datetime.fromtimestamp(week_start_for(1_800_000_000), dt.timezone.utc)
        self.assertEqual((d.weekday(), d.hour, d.minute), (6, 19, 0))

    def test_reset_from_statusline_beats_config(self):
        now = 1_800_000_000
        resets = now + 2 * 86400 + 777
        ws = q.week_start(now, CFG, [{"t": now - 10, "pct": 5, "resets_at": resets}])
        self.assertEqual(ws, resets - 7 * 86400)
        # a stale reset time rolls forward whole weeks
        ws2 = q.week_start(now + 9 * 86400, CFG, [{"t": now - 10, "pct": 5, "resets_at": resets}])
        self.assertEqual(ws2, resets - 7 * 86400 + 7 * 86400)

    def test_units_weights_and_unknown_model(self):
        u = {"input_tokens": 1_000_000}
        self.assertAlmostEqual(q.row_units(u, "claude-sonnet-5-5", q.DEFAULT_WEIGHTS), 3.0)
        self.assertAlmostEqual(q.row_units(u, "claude-opus-5-5", q.DEFAULT_WEIGHTS), 6.0)
        self.assertAlmostEqual(q.row_units(u, "some-new-model", q.DEFAULT_WEIGHTS), 3.0)  # default weight, never zero

    def test_cache_write_without_breakdown_counts_as_5m(self):
        u = {"cache_creation_input_tokens": 1_000_000}
        self.assertAlmostEqual(q.row_units(u, "sonnet", q.DEFAULT_WEIGHTS), q.UNIT_W5M)


class AreaTests(Base):
    def test_keywords_tag_paths_and_fallback(self):
        c = self.cfg
        self.assertEqual(q.classify("fix the css of the landing", "-p", c), "frontend")
        self.assertEqual(q.classify("add an api endpoint", "-p", c), "backend")
        self.assertEqual(q.classify("hello", "-home-me-server", c), "backend")          # by project folder
        self.assertEqual(q.classify("[area: docs] fix the css", "-p", c), "docs")        # explicit tag wins
        self.assertEqual(q.classify("[area: nonsense] hello", "-p", c), "other")         # unknown tag ignored
        self.assertEqual(q.classify(None, "-p", c), "other")

    def test_system_reminders_do_not_decide_the_area(self):
        txt = "<system-reminder>css css css landing</system-reminder> please write the changelog"
        self.assertEqual(q.classify(q.prompt_text(txt), "-p", self.cfg), "docs")

    def test_subagent_inherits_parent_area_and_split_is_right(self):
        self.session("proj", "s1", "fix the css", self.ws + H, 10, "a")
        synth.write_session(self.root, "proj", "x", synth.heavy_turns(self.ws + 2 * H, 5, 60, "sub"),
                            sub_of="s1", name="1")
        self.session("proj", "s2", "write the readme", self.ws + 3 * H, 10, "b")
        by = {}
        for _, u, a in self.rows():
            by[a] = by.get(a, 0) + u
        self.assertEqual(set(by), {"frontend", "docs"})
        self.assertGreater(by["frontend"], by["docs"])  # 10 own turns + 5 subagent turns vs 10

    def test_rows_before_the_week_are_ignored(self):
        self.session("proj", "old", "fix the css", self.ws - 5 * H, 10, "o")
        self.assertEqual(self.rows(), [])


class DedupAndCacheTests(Base):
    def test_forked_copy_is_counted_once(self):
        self.session("proj", "orig", "fix the css", self.ws + H, 10, "same")
        self.session("proj", "fork", "fix the css", self.ws + H, 10, "same")  # same message ids: a fork copies history
        self.assertEqual(len(self.rows()), 10)

    def test_scan_uses_cache_and_reparses_changed_files(self):
        p = self.session("proj", "s1", "fix the css", self.ws + H, 10, "a")
        q.scan(self.cfg, self.ws)
        with mock.patch.object(q, "scan_file", wraps=q.scan_file) as m:
            q.scan(self.cfg, self.ws)
            self.assertEqual(m.call_count, 0)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(synth.assistant_line(self.ws + 2 * H, "late", "sonnet")) + "\n")
            os.utime(p, (self.ws + 2 * H, self.ws + 2 * H))  # closing the file stamped the real clock
            q.scan(self.cfg, self.ws)
            self.assertEqual(m.call_count, 1)

    def test_changing_weights_invalidates_the_cache(self):
        self.session("proj", "s1", "fix the css", self.ws + H, 3, "a")
        a = sum(u for _, u, _ in self.rows())
        self.cfg["weights"] = dict(self.cfg["weights"], sonnet=2.0)
        b = sum(u for _, u, _ in self.rows())
        self.assertAlmostEqual(b, 2 * a)


class FalseAlarmTests(Base):

    def heavy_week(self):
        # lots of spend concentrated in the last day, like a busy week
        self.session("proj", "s1", "fix the css", self.ws + 2 * H, 200, "a")
        self.session("proj", "s2", "fix the css", self.now - 20 * H, 400, "b", model="claude-opus-5-5")
        return self.rows()

    def test_no_reading_means_no_alarm_at_all(self):
        res = q.analyze(self.heavy_week(), [], self.cfg, self.now)
        self.assertFalse(res["calibrated"])
        self.assertEqual(res["alerts"], [])
        self.assertGreater(res["units"], 0)

    def test_reading_from_another_week_is_ignored(self):
        rows = self.heavy_week()
        old = [{"t": self.ws - 3 * 86400, "pct": 2.0}, {"t": self.ws - 2 * 86400, "pct": 40.0}]
        res = q.analyze(rows, old, self.cfg, self.now)
        self.assertFalse(res["calibrated"])
        self.assertEqual(res["alerts"], [])

    def test_tiny_reading_is_not_trusted(self):
        rows = self.heavy_week()
        res = q.analyze(rows, [{"t": self.ws + 1.2 * H, "pct": 2.0}], self.cfg, self.now)  # 2 % +-25 % rounding
        self.assertFalse(res["calibrated"])
        self.assertEqual(res["alerts"], [])

    def test_stale_reading_is_not_trusted(self):
        rows = self.heavy_week()
        res = q.analyze(rows, [{"t": self.ws + 5 * H, "pct": 6.0}], self.cfg, self.now)  # 61 h old, limit is 48
        self.assertFalse(res["calibrated"])

    def test_low_official_reading_no_warning(self):
        rows = self.heavy_week()
        spent = sum(u for t, u, _ in rows if t <= self.now - H)
        readings = [{"t": self.now - H, "pct": 14.0}]
        self.cfg["areas"]["frontend"]["budget_pct"] = 50
        res = q.analyze(rows, readings, self.cfg, self.now)
        self.assertTrue(res["calibrated"])
        self.assertAlmostEqual(res["factor"], spent / 14.0)
        self.assertLess(res["pct"], 20)
        self.assertEqual(res["alerts"], [])

    def test_real_overspend_does_warn(self):
        rows = self.heavy_week()
        spent = sum(u for t, u, _ in rows if t <= self.now - H)
        readings = [{"t": self.now - H, "pct": 70.0}]  # the plan says 70 % with the same spend: it will not last
        res = q.analyze(rows, readings, self.cfg, self.now)
        self.assertTrue(any("runs out" in a for a in res["alerts"]), res["alerts"])
        self.assertGreater(res["projection"], 110)

    def test_no_projection_warning_in_the_first_hours(self):
        self.session("proj", "s1", "fix the css", self.ws + 0.1 * H, 50, "e")
        now = self.ws + 3 * H
        rows = q.rows_by_area(q.scan(self.cfg, self.ws), self.cfg, self.ws)
        res = q.analyze(rows, [{"t": now - 60, "pct": 3.0}], self.cfg, now)
        self.assertTrue(res["calibrated"])
        self.assertFalse(any("runs out" in a for a in res["alerts"]))

    def test_area_budget(self):
        rows = self.heavy_week()
        readings = [{"t": self.now - H, "pct": 50.0}]
        res = q.analyze(rows, readings, self.cfg, self.now)
        self.assertTrue(any("'frontend'" in a for a in res["alerts"]))  # all spend is in frontend, budget 25 %
        cfg2 = json.loads(json.dumps(self.cfg))
        cfg2["areas"]["frontend"]["budget_pct"] = 100
        res2 = q.analyze(rows, readings, cfg2, self.now)
        self.assertFalse(any("'frontend'" in a for a in res2["alerts"]))


class CliAndStateTests(Base):
    def run_cli(self, *argv, stdin=""):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            rc = q.main(["--config", str(self.cfg_path)] + list(argv))
        return rc, out.getvalue()

    def setUp(self):
        super().setUp()
        self.cfg_path = Path(self.tmp.name) / "cfg.json"
        self.cfg_path.write_text(json.dumps({"projects_dir": str(self.root), "areas": self.cfg["areas"]}), encoding="utf-8")
        self.session("proj", "s1", "fix the css", self.ws + 2 * H, 100, "a")

    def test_report_before_and_after_calibrate(self):
        now = synth.iso(self.now)
        rc, out = self.run_cli("--now", now, "report")
        self.assertEqual(rc, 0)
        self.assertIn("Not calibrated", out)
        rc, out = self.run_cli("--now", now, "calibrate", "14")
        self.assertIn("Recorded 14.0", out)
        self.assertIn("% of the weekly quota", out)
        self.assertIn("frontend", out)

    def test_calibrate_rejects_nonsense(self):
        rc, _ = self.run_cli("calibrate", "250")
        self.assertEqual(rc, 2)

    def test_statusline_records_official_percent_and_prints_a_line(self):
        payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 31.0, "resets_at": 1},
                                              "seven_day": {"used_percentage": 14.2, "resets_at": 1_800_400_000}}})
        rc, out = self.run_cli("--now", synth.iso(self.now), "statusline", stdin=payload)
        self.assertEqual(out.strip(), "week 14 % | 5h 31 %")
        rs = q.load_readings()
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["pct"], 14.2)
        self.assertEqual(rs[0]["resets_at"], 1_800_400_000)

    def test_statusline_without_limits_is_silent(self):
        rc, out = self.run_cli("statusline", stdin='{"model": {"id": "x"}}')
        self.assertEqual(out.strip(), "")
        self.assertEqual(q.load_readings(), [])

    def test_record_is_throttled(self):
        payload = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 14.0, "resets_at": 1_800_400_000}}})
        for _ in range(3):
            self.run_cli("--now", synth.iso(self.now), "record", stdin=payload)
        self.assertEqual(len(q.load_readings()), 1)

    def test_hook_prints_cached_warning_and_starts_refresh_when_old(self):
        q.write_json(self.state / "hook_cache.json", {"t": self.now - 2 * H, "msg": "quotamap: test warning"})
        with mock.patch.object(q, "spawn_refresh") as sp:
            rc, out = self.run_cli("--now", synth.iso(self.now), "hook")
        self.assertEqual(out.strip(), "quotamap: test warning")
        sp.assert_called_once()

    def test_hook_is_silent_without_cache_or_when_cache_is_too_old(self):
        with mock.patch.object(q, "spawn_refresh") as sp:
            rc, out = self.run_cli("--now", synth.iso(self.now), "hook")
            self.assertEqual(out, "")
            sp.assert_called_once()
            q.write_json(self.state / "hook_cache.json", {"t": self.now - 9 * H, "msg": "quotamap: old"})
            rc, out = self.run_cli("--now", synth.iso(self.now), "hook")
            self.assertEqual(out, "")

    def test_refresh_writes_the_cache_the_hook_reads(self):
        q.add_reading(self.now - H, 70.0)
        rc, _ = self.run_cli("--now", synth.iso(self.now), "refresh")
        c = q.read_json(self.state / "hook_cache.json", {})
        self.assertIn("quotamap:", c["msg"])

    def test_report_json_is_valid(self):
        rc, out = self.run_cli("--now", synth.iso(self.now), "report", "--json")
        self.assertIn("units", json.loads(out))

    def test_default_config_without_a_file_does_not_crash(self):
        cfg = q.load_config(Path(self.tmp.name) / "missing.json")
        self.assertIn("everything", cfg["areas"])


if __name__ == "__main__":
    unittest.main()
