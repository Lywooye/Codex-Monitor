---
name: codex-monitor
description: Start, inspect, or repair the local Codex Monitor overlay for context-window and token usage in ChatGPT or Codex Desktop.
---

# Codex Monitor

Use this skill when the user asks to show Codex context usage, token usage, session totals, per-reply token chips, sidebar hover metrics, or to restart/repair the Codex Monitor overlay.

## Boundary

Codex Monitor is local and read-only. It does not patch `ChatGPT.app`, `Codex.app`, `app.asar`, session JSONL files, auth files, or Codex settings. It reads local Codex session logs and injects temporary DOM elements into a Codex renderer launched with a loopback-only Chrome DevTools port.

## Install From GitHub

Use the repository URL:

```text
https://github.com/Lywooye/Codex-Monitor
```

Codex CLI equivalent:

```bash
codex plugin marketplace add https://github.com/Lywooye/Codex-Monitor --ref main
codex plugin add codex-monitor@codex-monitor
```

The plugin install only makes this skill and the local scripts available. To show the overlay, start the monitor from the installed plugin root.

## Start Monitor

From the plugin root:

```bash
./scripts/start_codex_monitor.sh 9222
```

This discovers either the current `ChatGPT.app` bundle or the legacy `Codex.app`, reopens it with a local DevTools port, injects the monitor, and loops so the overlay is restored after renderer restarts.
The injector refreshes the session payload every 10 seconds by default while the in-page observer handles ordinary UI changes.
For responsiveness, sidebar hover summaries cover the latest 100 sessions while per-message chip details are parsed for the latest 6 sessions by default. The selected session is included even outside that window. All local sessions remain in the quota denominator; initial history indexing is streamed and subsequent reads are incremental.
If the requested port is occupied, the scripts reuse it only when it owns a Codex renderer; otherwise they choose the next available local port.
Injected labels use Chinese when the Codex interface language starts with `zh`; English is the fallback for every other language.

## Auto Start

```bash
./scripts/install_launch_agent.sh 9222
```

This installs a macOS LaunchAgent that keeps Codex Monitor running after login, Codex restart, and Codex updates.
In LaunchAgent mode, Monitor waits while Codex is closed; it does not reopen Codex after a normal user quit.

```bash
./scripts/uninstall_launch_agent.sh
```

This stops and removes the LaunchAgent.

## Inject Once

```bash
./run_once.sh 9222
```

Use this when Codex is already running with `--remote-debugging-port=9222`.

## Inspect From CLI

```bash
python3 ./scripts/context_token_inspector.py --latest --format footer
python3 ./scripts/context_token_inspector.py --latest --format hover
python3 ./scripts/context_token_inspector.py --limit 20 --format table
```

## Upgrade Handling

If the desktop app upgrades or restarts, injected DOM elements disappear because they are intentionally temporary. Run `./scripts/start_codex_monitor.sh 9222` again, or keep the LaunchAgent installed so it can reconnect and re-inject after the renderer returns. The injector selects the main Codex window instead of utility renderers and replaces stale versioned observers without resetting the saved Monitor position, collapse state, or unit. An active response may defer the required app relaunch until the next start.

## Interpretation

- Context usage is the latest request input divided by its reported context window.
- Turn usage sums native response-ID-deduplicated records for a task, including recorded compaction; token_count mirrors are not added again. Legacy turns use advancing cumulative deltas.
- Session totals include all observed turns in the same thread. A known legacy cumulative prefix is included in the total but cannot be assigned to a turn/time. Native inherited fork records are excluded; separate agent threads are not merged.
- Tool counts use call IDs, with names and returned counts. Returned does not mean successful; tool arguments/results are not shown.
- Weekly quota is an estimate, never exact attribution: divide each observed same-cycle account percentage increment by local token shares. Show current %, per-cycle breakdown, lifetime percentage points, coverage and gaps. Do not bridge resets or invent a zero baseline.
- Other devices, model weighting, snapshot rounding/delay and unreadable history cause unknown error. Do not claim a numerical confidence interval or that token coverage equals quota accuracy.
- Chips appear only on a turn's last assistant message, matched within the identified chat by turn ID or unique text. Do not guess by position.

See `docs/ACCOUNTING.md` for details. Installation, app relaunch and autostart remain separate actions from read-only inspection.
