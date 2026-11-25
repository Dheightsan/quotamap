# quotamap

quotamap shows which area of your work is spending your weekly Claude Code quota, and warns you at session start when the week will not last.

```
Week since Sun 10 19:00 UTC (66.0 h in): 38.0 % of the weekly quota (calibrated 0 h ago, 1.7 units per 1 %)
Pace last 24 h: 0.99 %/h (even pace would be 0.60). Projection at reset (Sun 17 19:00 UTC): 139 %.
The quota does NOT last to the reset at this pace.
By area (% of the weekly quota | budget):
  backend                     17.1 % | 30 %
  frontend                    11.0 % | 25 %
  tooling                      5.5 % | 10 %
  docs                         2.2 % | 10 %
  research                     2.2 % | 10 %
Warning: at this pace the weekly quota runs out around Sat 16 03:50 UTC (projection 139 % at reset). To land at 100 %, hold 0.61 %/h instead of 0.99.
Warning: area 'backend' is at 17.1 % of the week and its budget is 30 %.
Warning: area 'tooling' is at 5.5 % of the week and its budget is 10 %.
```

You define areas (`frontend`, `backend`, `docs`...) and a budget for each, as a percent of the weekly quota. quotamap reads your local Claude Code transcripts, puts each session in an area using its first prompt and project folder, and compares the spend with the budget at that point of the week.

## Install

Python 3.9 or newer, standard library only.

**Claude Code plugin**

```
/plugin marketplace add Dheightsan/quotamap
/plugin install quotamap@quotamap
```

That adds a `SessionStart` hook. It prints a warning when there is one and nothing otherwise. Then create a config and give it one official reading:

```
python scripts/quotamap.py init
python scripts/quotamap.py calibrate 14
```

**Without the plugin**, clone the repo and add the hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python /path/to/quotamap/scripts/quotamap.py hook", "timeout": 10 } ] }
    ]
  }
}
```

**OpenCode** plugin: planned. OpenCode does not read Claude Code hooks, and its v2 plugin API is different from what I first wrote against, so there is nothing to install yet. The script is a plain command line tool (`python scripts/quotamap.py report` works from any terminal), and it only measures Claude Code usage.

## Calibration

quotamap counts cost units (list-price ratios between models and token kinds, not dollars). To turn units into a percent of your plan it needs a reading from the current week: the weekly % your plan shows, divided into the units spent up to that moment.

- By hand: `python scripts/quotamap.py calibrate 14`.
- From the status line: on Pro, Max and Ultra, Claude Code passes `rate_limits.seven_day.used_percentage` and its reset time to the `statusLine` command. Use `quotamap.py statusline` as that command, or pipe your own script's input into `quotamap.py record`. Readings from the status line also give the exact reset time of your week.

```json
{ "statusLine": { "type": "command", "command": "python /path/to/quotamap/scripts/quotamap.py statusline" } }
```

Without a reading from this week quotamap shows the split by area and nothing else. No percent, no projection, no warning. My first version of this converted units with a fixed factor taken from one early reading, and a few days later it said the quota was almost gone while the plan showed 14 %. Readings under 3 %, older than 48 hours or from another week are ignored for the same reason.

## Config

`config/quotamap.example.json` has the reset day and hour, model weights, thresholds and the areas. Areas are tried in order and the first match wins: a `[area: name]` tag in the first prompt, then `paths` (substrings of the project folder name), then `keywords` (regular expressions on the first prompt). Anything else goes to `fallback_area`, and subagents go to the area of their parent session.

## Commands

```
quotamap.py report [--json]   full report
quotamap.py hook              warning for SessionStart (reads a cache, never scans while you wait)
quotamap.py calibrate PCT     record the official weekly %
quotamap.py record            read a statusline JSON on stdin and store the weekly %
quotamap.py statusline        same, and print "week 14 % | 5h 31 %"
quotamap.py refresh           recompute the warning cache (the hook starts it in the background)
quotamap.py init              copy the example config to your user config folder
```

The first scan of a big history takes around half a minute. After that parsed files are cached and a report takes a few seconds. State lives in `%LOCALAPPDATA%\quotamap`, `~/Library/Application Support/quotamap` or `~/.local/state/quotamap`. `QUOTAMAP_STATE_DIR` and `QUOTAMAP_CONFIG` move it.

## ccusage

[ccusage](https://github.com/ryoppippi/ccusage) reports usage and cost per day, week, month and session, for Claude Code and other agents including OpenCode. quotamap does something smaller: it splits the weekly quota by area, compares it with budgets you set, and projects the week against the percent your plan shows. Use ccusage if you want dollars.

## Limits

It only sees transcripts on the machine where it runs. A session that changes topic stays in the area of its first prompt. Model weights are rough, so a reading from a few days ago can drift if your model mix changed.

Run the tests with `python -m unittest discover -s tests`.

## License

MIT
