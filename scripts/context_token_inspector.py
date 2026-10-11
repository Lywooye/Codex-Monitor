#!/usr/bin/env python3
"""Inspect Codex Desktop token_count events in local session JSONL files."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from usage_accounting import Ledger


DEFAULT_ROOTS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
]
THREAD_ID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def comma(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def pct(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round((numerator / denominator) * 100, 1)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def summarize_session(path: str | Path) -> dict[str, Any]:
    return parse_session_detail(path)["summary"]


def summarize_session_fast(path: str | Path) -> dict[str, Any]:
    # A final token_count alone misses remote compaction. Use the same ledger.
    return session_index(path)["summary"]


_INDEX_CACHE = {}


def append_complete_rows(ledger, path, offset=0):
    # Stream the full-history index: retaining decoded tool outputs and replayed
    # compaction history can otherwise require gigabytes on the first scan.
    with path.open('rb') as handle:
        handle.seek(offset)
        while line := handle.readline():
            if not line.endswith(b'\n'):
                break
            offset = handle.tell()
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                ledger.append((row,))
    return offset


def session_index(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    stat = path.stat()
    key = str(path)
    signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _INDEX_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[3]
    appending = cached and signature[:2] == cached[0][:2] and stat.st_size > cached[0][3]
    ledger = cached[2] if appending else Ledger(keep_messages=False)
    offset = cached[1] if appending else 0
    offset = append_complete_rows(ledger, path, offset)
    result = ledger.result()
    result["summary"]["path"] = str(path)
    result["summary"]["thread_id"] = result["summary"].get("thread_id") or infer_thread_id(path)
    result["summary"]["token_events"] = len(result["calls"])
    compact = {"summary": result["summary"], "calls": result["calls"],
        "snapshots": result["snapshots"], "thread_id": result["summary"]["thread_id"],
        "account": ledger.meta.get("creator_account_id") or "unknown"}
    _INDEX_CACHE[key] = (signature, offset, ledger, compact)
    return compact


def parse_session_detail(path: str | Path, summary=None) -> dict[str, Any]:
    path = Path(path).expanduser()
    detail = {"_ledger": Ledger(), "_path": str(path)}
    detail["_read_offset"] = append_complete_rows(detail["_ledger"], path)
    extend_session_detail(detail, ())
    detail["summary"]["thread_id"] = detail["summary"].get("thread_id") or infer_thread_id(path)
    return detail


def extend_session_detail(detail: dict[str, Any], rows: Iterable[dict[str, Any]], summary=None) -> dict[str, Any]:
    ledger = detail.setdefault("_ledger", Ledger())
    ledger.append(rows)
    detail.update(ledger.result())
    detail["summary"]["path"] = detail.get("_path")
    detail["summary"]["token_events"] = len(detail["calls"])
    detail["_current_turn_index"] = len(ledger.turns)
    for message in detail["messages"]:
        if message.get("token_usage"):
            message["token_footer"] = format_reply_footer(message["token_usage"])
    return detail


def read_jsonl_from_offset(path: str | Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    path = Path(path).expanduser()
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    if not data:
        return [], offset

    # Leave an in-progress final line for the next refresh instead of silently
    # dropping it if the app is writing at the same moment we read the file.
    final_newline = data.rfind(b"\n")
    if final_newline < 0:
        return [], offset
    complete = data[: final_newline + 1]
    rows: list[dict[str, Any]] = []
    for line in complete.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, offset + len(complete)


def render_markdownish(text: str) -> str:
    escaped = html.escape(text)
    escaped = escaped.replace("\n", "<br>")
    return escaped


def infer_thread_id(path: Path) -> str:
    name = path.stem
    matches = THREAD_ID_PATTERN.findall(name)
    if matches:
        return matches[-1]
    if "-" not in name:
        return name
    return name.rsplit("-", 1)[-1]


def session_files(paths: Iterable[str], limit: int | None = None) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(os.path.expanduser(raw))
        if path.is_file() and path.suffix == ".jsonl":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.jsonl"))

    files = sorted(set(files), key=lambda candidate: candidate.stat().st_mtime, reverse=True)
    if limit is not None:
        return files[:limit]
    return files


def format_reply_footer(summary: dict[str, Any]) -> str:
    context = comma(summary.get("latest_context_tokens"))
    window = comma(summary.get("context_window"))
    percent = summary.get("latest_context_percent")
    percent_text = f"{percent:.1f}%" if isinstance(percent, float) else "-"
    return (
        f"context: {context} / {window} ({percent_text}) | "
        f"turn: {comma(summary.get('latest_turn_total_tokens'))} tokens "
        f"(in {comma(summary.get('latest_turn_input_tokens'))}, "
        f"out {comma(summary.get('latest_turn_output_tokens'))}, "
        f"reasoning {comma(summary.get('latest_turn_reasoning_tokens'))}) | "
        f"session: {comma(summary.get('session_total_tokens'))} tokens"
    )


def format_reply_chip(
    summary: dict[str, Any],
    round_index: int | None = None,
    total_rounds: int | None = None,
    user_turn_index: int | None = None,
    user_total_turns: int | None = None,
    assistant_turn_index: int | None = None,
    assistant_total_turns: int | None = None,
) -> str:
    percent = summary.get("latest_context_percent")
    percent_text = f" ({percent:.1f}%)" if isinstance(percent, float) else ""
    chip = (
        f"Token: Current {comma(summary.get('latest_context_tokens'))}/"
        f"{comma(summary.get('context_window'))}{percent_text} | "
        f"Total {comma(summary.get('latest_turn_total_tokens'))}/"
        f"{comma(summary.get('session_total_tokens'))}"
    )
    user_index = user_turn_index if user_turn_index is not None else round_index
    user_total = user_total_turns if user_total_turns is not None else total_rounds
    assistant_index = assistant_turn_index if assistant_turn_index is not None else round_index
    assistant_total = assistant_total_turns if assistant_total_turns is not None else total_rounds
    if user_index is not None and user_total is not None:
        chip += f"   Rounds：User {user_index}/{user_total}"
        if assistant_index is not None and assistant_total is not None:
            chip += f"  | Assistant {assistant_index}/{assistant_total}"
    elif assistant_index is not None and assistant_total is not None:
        chip += f"   Rounds：Assistant {assistant_index}/{assistant_total}"
    return chip


def format_hover(summary: dict[str, Any]) -> str:
    lines = [
        f"Session total  {comma(summary.get('session_total_tokens'))}",
        f"Input          {comma(summary.get('session_input_tokens'))}",
        f"Cached input   {comma(summary.get('session_cached_input_tokens'))}",
        f"Output         {comma(summary.get('session_output_tokens'))}",
        f"Reasoning      {comma(summary.get('session_reasoning_tokens'))}",
    ]
    return "\n".join(lines)


def format_percent(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, float) else "-"


def context_pressure(value: Any) -> str:
    if not isinstance(value, float):
        return "UNKNOWN"
    if value >= 85:
        return "HIGH"
    if value >= 70:
        return "WATCH"
    return "OK"


def print_table(summaries: list[dict[str, Any]]) -> None:
    headers = ["updated", "thread", "context", "turn", "session", "cwd"]
    rows = []
    for item in summaries:
        rows.append(
            [
                str(item.get("updated_at") or "-")[:19],
                str(item.get("thread_id") or "-")[:12],
                f"{comma(item.get('latest_context_tokens'))}/{comma(item.get('context_window'))}",
                comma(item.get("latest_turn_total_tokens")),
                comma(item.get("session_total_tokens")),
                str(item.get("cwd") or "-"),
            ]
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Session JSONL files or directories. Defaults to ~/.codex/sessions and archived_sessions.",
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum sessions to inspect.")
    parser.add_argument(
        "--format",
        choices=["table", "json", "hover", "footer"],
        default="table",
        help="Output format.",
    )
    parser.add_argument("--latest", action="store_true", help="Only show the newest matching session.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = args.paths or [str(path) for path in DEFAULT_ROOTS]
    limit = 1 if args.latest else args.limit
    summaries = [summarize_session(path) for path in session_files(paths, limit=limit)]
    summaries = [summary for summary in summaries if summary.get("token_events")]

    if args.format == "json":
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    elif args.format == "hover":
        for index, summary in enumerate(summaries):
            if index:
                print()
            print(format_hover(summary))
    elif args.format == "footer":
        for summary in summaries:
            print(format_reply_footer(summary))
    else:
        print_table(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
