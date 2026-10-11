# Validation — 2026-10-11

Base: upstream v0.3.2 at `56b2ea3602cf8395bdc9d0513a8088f8e38d657e`. Fork version: 0.4.0.

## Scope

Changed the local ledger/parser, quota allocation, UI payload and matching/display, tests and documentation. Startup/relaunch/LaunchAgent scripts are unchanged. No software installation, real-client CDP connection, injection, app restart, or autostart change was performed.

## Checks

- Python suite: 51 tests, including full-turn sums, native/mirror deduplication, embedded-only compaction, inherited records, cached/reasoning subtotals, incomplete append and file replacement, old counters, tool outputs and per-turn context.
- Quota tests: parallel conversation shares; reset boundaries; a turn spanning resets; same-deadline decreases; missing, stale and conflicting snapshots; unknown timestamps; copied logs/accounts; lifetime totals above 100 percentage points.
- Browser smoke test on synthetic HTML: full-turn values, tools, compaction, cycle display, preserved expanded details, repeated injection without duplicate chips, narrow viewport, unknown chat, ambiguous and unmatched replies. No network requests beyond the intercepted test origin.
- Real local log reconciliation: three anonymized samples, 1,060 native records and 51 turns. Every native turn and all three session totals match an independently summed response-ID ledger. Included compaction: 3,367,495 tokens. See `local-validation.json`; no transcripts, identifiers or source paths are published.
- Performance: initial scan of approximately 2,500 files / 3.9 GB; warm refresh uses append-only parsing. See `performance-validation.json` for measured times and peak process memory; active logs may grow during the run.

## Reproduce

```
python3 -m unittest discover -s tests -v
```

Optional isolated UI test, using an already available Playwright installation and Chromium:

```
node tests/test_ui.cjs
```

`NODE_PATH` can point to existing Node packages; `BROWSER_EXECUTABLE` can select an existing Chromium executable; `PYTHON` selects Python; `SCREENSHOT` saves a synthetic screenshot. The test itself does not download dependencies. Python tests that mock DevTools bind a temporary loopback HTTP server.

The real-client installation/visual acceptance step remains unperformed by user request. Weekly percentages are estimates with unquantifiable attribution error; passing the tests validates the documented algorithm, not official quota attribution.
