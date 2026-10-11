#!/usr/bin/env python3
"""Inject Codex context/token stats into the existing Codex Desktop window.

This helper talks to a Codex Desktop renderer through Chrome DevTools Protocol.
It does not modify Codex.app, app.asar, app state, or local session JSONL files.
Codex must be launched with a local --remote-debugging-port first.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import context_token_inspector as inspector
from weekly_usage import estimate_weekly


DEFAULT_PORT = 9222
DETAIL_SESSION_LIMIT = 6
DETAIL_CACHE_LIMIT = 24
_DETAIL_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


class CDPError(RuntimeError):
    pass


class CDPClient:
    def __init__(self, websocket_url: str, timeout: float = 5.0) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self.sock = self._connect(websocket_url)
        self.next_id = 1

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        self._send_json({"id": message_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._recv_json()
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise CDPError(str(message["error"]))
            return message.get("result") or {}
        raise TimeoutError(f"Timed out waiting for CDP response to {method}")

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        remote = result.get("result") or {}
        if "exceptionDetails" in result:
            raise CDPError(str(result["exceptionDetails"]))
        return remote.get("value")

    def _connect(self, websocket_url: str) -> socket.socket:
        parsed = urllib.parse.urlparse(websocket_url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError(f"Unsupported websocket URL: {websocket_url}")
        port = parsed.port or 80
        sock = socket.create_connection((parsed.hostname, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise CDPError(f"WebSocket handshake failed: {response[:200]!r}")
        return sock

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.sock.sendall(masked_websocket_frame(data))

    def _recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = read_websocket_frame(self.sock)
            if opcode == 1:
                data = json.loads(payload.decode("utf-8"))
                if isinstance(data, dict):
                    return data
            if opcode == 9:
                # Chromium may ping long-lived CDP clients. A masked pong keeps
                # the connection alive across normal ten-second refresh gaps.
                self.sock.sendall(masked_websocket_frame(payload, opcode=10))
            if opcode == 8:
                raise CDPError("WebSocket closed by target")


def masked_websocket_frame(payload: bytes, opcode: int = 1) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def read_websocket_frame(sock: socket.socket) -> tuple[int, bytes]:
    first = read_exact(sock, 2)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]
    mask = read_exact(sock, 4) if masked else b""
    payload = read_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def read_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise CDPError("Unexpected WebSocket EOF")
        chunks.extend(chunk)
    return bytes(chunks)


def devtools_targets(port: int) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CDPError(
            f"Cannot connect to the Codex renderer on 127.0.0.1:{port}. "
            "Launch ChatGPT or Codex with --remote-debugging-port first."
        ) from exc
    return data if isinstance(data, list) else []


def select_target(targets: list[dict[str, Any]]) -> dict[str, Any]:
    pages = [
        target
        for target in targets
        if target.get("type") == "page"
        and is_codex_renderer_target(target)
        and target_score(target) > 0
    ]
    candidates = sorted(pages, key=target_score, reverse=True)
    for target in candidates:
        if target.get("webSocketDebuggerUrl"):
            return target
    raise CDPError("No debuggable Codex renderer target found")


def is_codex_renderer_target(target: dict[str, Any]) -> bool:
    title = str(target.get("title") or "").lower()
    url = str(target.get("url") or "").lower()
    return url.startswith("app://") and (
        "codex" in title
        or "chatgpt" in title
        or url.startswith("app://codex/")
        or url.startswith("app://-/index.html")
    )


def target_score(target: dict[str, Any]) -> int:
    title = str(target.get("title") or "").lower()
    url = str(target.get("url") or "").lower()
    decoded_url = urllib.parse.unquote(url)
    score = 0
    if url.startswith("app://"):
        score += 100
    # ChatGPT.app exposes utility pages (for example the avatar overlay) on the
    # same CDP endpoint as the main Codex window. Always prefer the un-routed
    # index page so the monitor is not injected into an invisible utility view.
    if url in {"app://-/index.html", "app://codex/index.html"}:
        score += 200
    if "initialroute=" in decoded_url:
        score -= 100
    if "avatar-overlay" in decoded_url:
        score -= 300
    if "codex" in title or "codex" in url:
        score += 40
    if "chatgpt" in title:
        score += 30
    return score


def runtime_state(client: CDPClient) -> dict[str, Any]:
    expression = r"""
(() => {
  function attr(el, name) { return el && el.getAttribute ? el.getAttribute(name) : null; }
  function activeSidebarRow() {
    return document.querySelector('[data-app-action-sidebar-thread-row][data-app-action-sidebar-thread-active="true"]') ||
      document.querySelector('[data-app-action-sidebar-thread-row][aria-current="page"]') ||
      document.querySelector('[data-app-action-sidebar-thread-active="true"]') ||
      document.querySelector('[data-app-action-sidebar-thread-row][data-app-action-sidebar-thread-active]:not([data-app-action-sidebar-thread-active="false"])');
  }
  const activeRow =
    activeSidebarRow();
  const activeId =
    attr(activeRow, 'data-app-action-sidebar-thread-id') ||
    attr(activeRow && activeRow.querySelector('[data-app-action-sidebar-thread-id]'), 'data-app-action-sidebar-thread-id') ||
    attr(document.querySelector('[data-conversation-id]'), 'data-conversation-id') ||
    attr(document.querySelector('[data-above-composer-conversation-id]'), 'data-above-composer-conversation-id') ||
    null;
  return { href: location.href, title: document.title, activeThreadId: activeId };
})()
"""
    value = client.evaluate(expression)
    return value if isinstance(value, dict) else {}


def build_payload(
    paths: list[str],
    limit: int,
    selected_thread_id: str | None,
    detail_limit: int = DETAIL_SESSION_LIMIT,
) -> dict[str, Any]:
    all_files = inspector.session_files(paths)
    for cached_path in set(inspector._INDEX_CACHE) - {str(p) for p in all_files}:
        del inspector._INDEX_CACHE[cached_path]
    indexed = []
    unavailable = 0
    for path in all_files:
        try:
            indexed.append(inspector.session_index(path))
        except (OSError, ValueError):
            unavailable += 1
    # Quota denominators include all discovered local sessions, not the UI limit.
    unique = {}
    for entry in indexed:
        unique.setdefault(entry["thread_id"], dict(entry["summary"]))
    summaries = list(unique.values())[:limit]
    summaries = [summary for summary in summaries if summary.get("session_total_tokens")]
    by_thread: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        thread_id = summary.get("thread_id")
        if not thread_id:
            continue
        normalized = normalize_thread_id(str(thread_id))
        by_thread[normalized] = summary
        by_thread[f"local:{normalized}"] = summary
    selected = summaries[0] if summaries else None
    active_selected = by_thread.get(normalize_thread_id(selected_thread_id or "")) if selected_thread_id else None
    if selected_thread_id and active_selected is None:
        active_path = session_file_for_thread(paths, selected_thread_id)
        known_paths = {str(summary.get("path")) for summary in summaries}
        if active_path and str(active_path) not in known_paths:
            summary = dict(inspector.summarize_session_fast(active_path))
            normalized = normalize_thread_id(str(summary.get("thread_id") or ""))
            if normalized == normalize_thread_id(selected_thread_id) and summary.get("session_total_tokens"):
                summaries.append(summary)
                by_thread[normalized] = summary
                by_thread[f"local:{normalized}"] = summary
                active_selected = summary

    quota_thread = (active_selected or selected or {}).get("thread_id")
    quota = estimate_weekly(indexed, quota_thread)
    if unavailable:
        quota["warnings"].append("unreadable_session_files")
        quota["unreadable_files"] = unavailable
    for summary in summaries:
        if summary.get("thread_id") == quota_thread:
            summary["weekly"] = quota
    compact_summaries = []
    for summary in summaries:
        compact_summaries.append(
            {
                **summary,
                "thread_id": summary.get("thread_id"),
                "thread_keys": thread_keys(summary.get("thread_id")),
                "cwd": summary.get("cwd"),
                "updated_at": summary.get("updated_at"),
                "latest_context_tokens": summary.get("latest_context_tokens"),
                "context_window": summary.get("context_window"),
                "latest_context_percent": summary.get("latest_context_percent"),
                "latest_turn_total_tokens": summary.get("latest_turn_total_tokens"),
                "latest_turn_input_tokens": summary.get("latest_turn_input_tokens"),
                "latest_turn_cached_input_tokens": summary.get("latest_turn_cached_input_tokens"),
                "latest_turn_output_tokens": summary.get("latest_turn_output_tokens"),
                "latest_turn_reasoning_tokens": summary.get("latest_turn_reasoning_tokens"),
                "session_total_tokens": summary.get("session_total_tokens"),
                "session_input_tokens": summary.get("session_input_tokens"),
                "session_cached_input_tokens": summary.get("session_cached_input_tokens"),
                "session_output_tokens": summary.get("session_output_tokens"),
                "session_reasoning_tokens": summary.get("session_reasoning_tokens"),
                "hover": inspector.format_hover(summary),
                "footer": inspector.format_reply_footer(summary),
                "badge": compact_badge(summary),
            }
        )

    details_by_thread: dict[str, dict[str, Any]] = {}
    detail: dict[str, Any] | None = None
    detail_summaries = summaries[: max(0, detail_limit)]
    if active_selected and active_selected not in detail_summaries:
        detail_summaries.append(active_selected)
    for summary in detail_summaries:
        if not summary.get("path"):
            continue
        parsed = cached_session_detail(str(summary["path"]), summary)
        assistant_token_messages = [
            message
            for message in parsed.get("messages", [])
            if message.get("role") == "assistant" and message.get("token_usage")
        ]
        total_rounds = len(assistant_token_messages)
        assistant_item_messages = assistant_token_messages
        assistant_start_index = total_rounds - len(assistant_item_messages)
        assistant_items = [
            {
                "footer": message.get("token_footer"),
                "chip": inspector.format_reply_chip(
                    message["token_usage"],
                    user_turn_index=message.get("turn_index"),
                    user_total_turns=message.get("total_turns"),
                    assistant_turn_index=assistant_start_index + index,
                    assistant_total_turns=total_rounds,
                ),
                "tokenUsage": message["token_usage"],
                "turnId": message.get("turn_id"),
                "tools": message["token_usage"].get("latest_turn_tools", []),
                "toolCount": message["token_usage"].get("latest_turn_tool_count", 0),
                "compactionTokens": message["token_usage"].get("latest_turn_compaction_tokens", 0),
                "weeklyTotal": quota.get("turn_estimated_pp", {}).get(message.get("turn_id"))
                    if summary.get("thread_id") == quota_thread else None,
                "weeklyCurrent": quota.get("turn_current_pp", {}).get(message.get("turn_id"), 0)
                    if summary.get("thread_id") == quota_thread and quota.get("current_pp") is not None else None,
                "textPrefix": text_prefix(message.get("text")),
                "roundIndex": message.get("turn_index") or index,
                "totalRounds": message.get("total_turns") or total_rounds,
                "userTurnIndex": message.get("turn_index"),
                "userTotalTurns": message.get("total_turns"),
                "assistantTurnIndex": assistant_start_index + index,
                "assistantTotalTurns": total_rounds,
            }
            for index, message in enumerate(assistant_item_messages, start=1)
        ]
        assistant_chips = [
            item["chip"]
            for item in assistant_items
        ]
        assistant_footers = [
            item["footer"]
            for item in assistant_items
            if item.get("footer")
        ]
        item_detail = {
            "thread_id": summary.get("thread_id"),
            "updated_at": summary.get("updated_at"),
            "footer": inspector.format_reply_footer(summary),
            "assistantFooters": assistant_footers,
            "assistantChips": assistant_chips,
            "assistantItems": assistant_items,
        }
        for key in thread_keys(summary.get("thread_id")):
            details_by_thread[key] = item_detail
        if selected and selected.get("thread_id") == summary.get("thread_id"):
            detail = item_detail

    return {
        "activeThreadId": selected_thread_id,
        "selectedThreadId": (selected or {}).get("thread_id") or selected_thread_id,
        "summaries": compact_summaries,
        "detail": detail,
        "detailsByThread": details_by_thread,
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def cached_session_detail(path: str, summary: dict[str, Any]) -> dict[str, Any]:
    try:
        stat = Path(path).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return inspector.parse_session_detail(path, summary=summary)

    cached = _DETAIL_CACHE.get(path)
    if cached and cached[2].get("_file_identity") == (stat.st_dev, stat.st_ino):
        cached_mtime, cached_offset, parsed = cached
        if cached_mtime == signature[0] and cached_offset == signature[1]:
            return parsed
        if signature[1] > cached_offset:
            next_offset = inspector.append_complete_rows(parsed["_ledger"], Path(path), cached_offset)
            inspector.extend_session_detail(parsed, (), summary=summary)
            _DETAIL_CACHE[path] = (signature[0], next_offset, parsed)
            return parsed

    parsed = inspector.parse_session_detail(path, summary=summary)
    if path not in _DETAIL_CACHE and len(_DETAIL_CACHE) >= DETAIL_CACHE_LIMIT:
        _DETAIL_CACHE.pop(next(iter(_DETAIL_CACHE)))
    parsed["_file_identity"] = (stat.st_dev, stat.st_ino)
    _DETAIL_CACHE[path] = (signature[0], parsed["_read_offset"], parsed)
    return parsed


def compact_badge(summary: dict[str, Any]) -> str:
    percent = summary.get("latest_context_percent")
    if isinstance(percent, float):
        return f"{percent:.1f}% ctx"
    total = summary.get("session_total_tokens")
    if isinstance(total, int):
        return f"{total // 1000}k tok"
    return "tokens"


def text_prefix(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def normalize_thread_id(thread_id: str) -> str:
    if thread_id.startswith("local:"):
        return thread_id.removeprefix("local:")
    return thread_id


def thread_keys(thread_id: Any) -> list[str]:
    if not thread_id:
        return []
    normalized = normalize_thread_id(str(thread_id))
    return [normalized, f"local:{normalized}"]


def session_file_for_thread(paths: list[str], thread_id: str | None) -> Path | None:
    normalized = normalize_thread_id(thread_id or "")
    if not normalized:
        return None
    for path in inspector.session_files(paths, limit=None):
        if normalized in path.stem:
            return path
    return None


INJECTION_SCRIPT = r"""
(payload => {
  // Bump this only when closures or event handlers change. A long-lived
  // renderer may still contain an observer from an older plugin release.
  const RUNTIME_VERSION = 7;
  const ROOT_ID = 'codex-context-token-inspector-root';
  const STYLE_ID = 'codex-context-token-inspector-style';
  const FOOTER_ATTR = 'data-context-token-footer';
  const CHIP_ATTR = 'data-context-token-chip';
  const BADGE_ATTR = 'data-context-token-badge';
  const SIDEBAR_HOVER_ATTR = 'data-context-token-sidebar-hover';
  const COLLAPSE_KEY = 'codex-context-token-inspector-collapsed';
  const POSITION_KEY = 'codex-context-token-inspector-position';
  const UNIT_KEY = 'codex-context-token-inspector-unit';
  const UNIT_DEFAULTED_KEY = 'codex-context-token-inspector-unit-defaulted';
  const previousRuntimeVersion = window.__codexContextTokenInspectorRuntimeVersion;
  const runtimeChanged = previousRuntimeVersion !== RUNTIME_VERSION;
  window.__codexContextTokenInspectorRuntimeVersion = RUNTIME_VERSION;

  const I18N = {
    en: {
      monitor: 'Monitor', tokenUnit: 'Token unit', rawUnit: 'raw',
      expandMonitor: 'Expand Monitor', collapseMonitor: 'Collapse Monitor',
      status: 'status', left: 'left', context: 'context', turn: 'turn', session: 'session',
      inputShort: 'in', cachedShort: 'cached', outputShort: 'out', reasoningShort: 'reason',
      sessionTotal: 'Session total', input: 'Input', cachedInput: 'Cached input',
      output: 'Output', reasoning: 'Reasoning', token: 'Token', current: 'Current',
      total: 'Total', rounds: 'Rounds', user: 'User', assistant: 'Assistant',
      contextTitle: 'Context', turnTitle: 'Turn', sessionTitle: 'Session', tokens: 'tokens',
      userRounds: 'User rounds', assistantRounds: 'Assistant rounds',
      noRecords: 'No token records found.', madeBy: 'Made by Kevin KE',
      unknown: 'UNKNOWN', high: 'HIGH', watch: 'WATCH', ok: 'OK',
    },
    zh: {
      monitor: '监控', tokenUnit: 'Token 单位', rawUnit: '原值',
      expandMonitor: '展开监控', collapseMonitor: '收起监控',
      status: '状态', left: '剩余', context: '上下文', turn: '本轮', session: '会话',
      inputShort: '输入', cachedShort: '缓存', outputShort: '输出', reasoningShort: '推理',
      sessionTotal: '会话总计', input: '输入', cachedInput: '缓存输入',
      output: '输出', reasoning: '推理', token: 'Token', current: '当前',
      total: '总计', rounds: '轮次', user: '用户', assistant: '助手',
      contextTitle: '上下文', turnTitle: '本轮', sessionTitle: '会话', tokens: 'Token',
      userRounds: '用户轮次', assistantRounds: '助手轮次',
      noRecords: '暂无 Token 记录。', madeBy: 'Kevin KE 制作',
      unknown: '未知', high: '高', watch: '注意', ok: '正常',
    },
  };

  function uiLanguage() {
    const language = String(document.documentElement.lang || navigator.language || 'en').toLowerCase();
    return language.startsWith('zh') ? 'zh' : 'en';
  }
  function tr(key) {
    const language = uiLanguage();
    return I18N[language][key] || I18N.en[key] || key;
  }
  function labeled(key, value) {
    return uiLanguage() === 'zh' ? `${tr(key)}：${value}` : `${tr(key)}: ${value}`;
  }
  function parenthesized(value) {
    return uiLanguage() === 'zh' ? `（${value}）` : `(${value})`;
  }
  function joined(values) {
    return values.join(uiLanguage() === 'zh' ? '，' : ', ');
  }

  function n(value) {
    return value == null ? '-' : new Intl.NumberFormat().format(value);
  }
  function ensureDefaultUnit() {
    if (!localStorage.getItem(UNIT_DEFAULTED_KEY)) {
      localStorage.setItem(UNIT_KEY, 'k');
      localStorage.setItem(UNIT_DEFAULTED_KEY, 'true');
    }
  }
  function unitMode() {
    const value = localStorage.getItem(UNIT_KEY);
    return ['raw', 'k', 'm'].includes(value) ? value : 'k';
  }
  function token(value) {
    if (value == null || Number.isNaN(Number(value))) return '-';
    const number = Number(value);
    const mode = unitMode();
    if (mode === 'k') {
      const scaled = number / 1000;
      const digits = Math.abs(scaled) >= 1000 ? 0 : Math.abs(scaled) >= 100 ? 1 : 2;
      return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(scaled)}K`;
    }
    if (mode === 'm') return `${(number / 1000000).toFixed(Math.abs(number) >= 10000000 ? 1 : 2)}M`;
    return n(number);
  }
  function pct(value) {
    return typeof value === 'number' ? `${value.toFixed(1)}%` : '-';
  }
  function pressure(value) {
    if (typeof value !== 'number') return tr('unknown');
    if (value >= 85) return tr('high');
    if (value >= 70) return tr('watch');
    return tr('ok');
  }
  function remainingContext(item) {
    if (typeof item?.latest_context_tokens !== 'number' || typeof item?.context_window !== 'number') return null;
    return Math.max(item.context_window - item.latest_context_tokens, 0);
  }
  function summaryHover(item) {
    return [
      labeled('sessionTotal', token(item.session_total_tokens)),
      labeled('input', token(item.session_input_tokens)),
      labeled('cachedInput', token(item.session_cached_input_tokens)),
      labeled('output', token(item.session_output_tokens)),
      labeled('reasoning', token(item.session_reasoning_tokens)),
    ].join('\n');
  }
  function itemChip(item, roundIndex, totalRounds) {
    const usage = item?.tokenUsage || {};
    const userIndex = item.userTurnIndex;
    const userTotal = item.userTotalTurns;
    const assistantIndex = item.assistantTurnIndex || item.roundIndex || roundIndex;
    const assistantTotal = item.assistantTotalTurns || item.totalRounds || totalRounds;
    const roundValues = userIndex && userTotal
      ? `${tr('user')} ${userIndex}/${userTotal}  | ${tr('assistant')} ${assistantIndex}/${assistantTotal}`
      : `${tr('assistant')} ${assistantIndex}/${assistantTotal}`;
    const current = `${tr('current')} ${token(usage.latest_context_tokens)}/${token(usage.context_window)} ${parenthesized(pct(usage.latest_context_percent))}`;
    return `${labeled('token', current)} | ${tr('total')} ${token(usage.latest_turn_total_tokens)}/${token(usage.session_total_tokens)}   ` +
      labeled('rounds', roundValues);
  }
  function itemTitle(item, roundIndex, totalRounds) {
    const usage = item?.tokenUsage || {};
    const userIndex = item.userTurnIndex;
    const userTotal = item.userTotalTurns;
    const assistantIndex = item.assistantTurnIndex || item.roundIndex || roundIndex;
    const assistantTotal = item.assistantTotalTurns || item.totalRounds || totalRounds;
    const turnBreakdown = joined([
      `${tr('inputShort')} ${token(usage.latest_turn_input_tokens)}`,
      `${tr('outputShort')} ${token(usage.latest_turn_output_tokens)}`,
      `${tr('reasoningShort')} ${token(usage.latest_turn_reasoning_tokens)}`,
    ]);
    const lines = [
      labeled('contextTitle', `${token(usage.latest_context_tokens)} / ${token(usage.context_window)} ${parenthesized(pct(usage.latest_context_percent))}`),
      labeled('turnTitle', `${token(usage.latest_turn_total_tokens)} ${tr('tokens')} ${parenthesized(turnBreakdown)}`),
      labeled('sessionTitle', `${token(usage.session_total_tokens)} ${tr('tokens')}`),
    ];
    if (userIndex && userTotal) lines.push(labeled('userRounds', `${userIndex}/${userTotal}`));
    lines.push(labeled('assistantRounds', `${assistantIndex}/${assistantTotal}`));
    return lines.join('\n');
  }
  function rowThreadId(row) {
    return row.getAttribute('data-app-action-sidebar-thread-id') ||
      row.querySelector('[data-app-action-sidebar-thread-id]')?.getAttribute('data-app-action-sidebar-thread-id') ||
      null;
  }
  function normalizeThreadId(threadId) {
    return String(threadId || '').replace(/^local:/, '');
  }
  function threadKeys(threadId) {
    const normalized = normalizeThreadId(threadId);
    return [String(threadId || ''), normalized, `local:${normalized}`].filter(Boolean);
  }
  function activeSidebarRow() {
    return document.querySelector('[data-app-action-sidebar-thread-row][data-app-action-sidebar-thread-active="true"]') ||
      document.querySelector('[data-app-action-sidebar-thread-row][aria-current="page"]') ||
      document.querySelector('[data-app-action-sidebar-thread-active="true"]') ||
      document.querySelector('[data-app-action-sidebar-thread-row][data-app-action-sidebar-thread-active]:not([data-app-action-sidebar-thread-active="false"])');
  }
  function activeThreadId() {
    const row = activeSidebarRow();
    return (row && rowThreadId(row)) ||
      document.querySelector('[data-conversation-id]')?.getAttribute('data-conversation-id') ||
      document.querySelector('[data-above-composer-conversation-id]')?.getAttribute('data-above-composer-conversation-id') || null;
  }
  function ensureStyle() {
    let style = document.getElementById(STYLE_ID);
    if (!style) {
      style = document.createElement('style');
      style.id = STYLE_ID;
      document.head.appendChild(style);
    }
    const css = `
      [${BADGE_ATTR}] {
        display: inline-flex;
        align-items: center;
        width: fit-content;
        max-width: 100%;
        margin-top: 4px;
        padding: 2px 6px;
        border-radius: 6px;
        background: color-mix(in srgb, CanvasText 9%, transparent);
        color: color-mix(in srgb, CanvasText 72%, transparent);
        font: 11px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        pointer-events: none;
      }
      .cti-hud {
        position: fixed;
        right: 14px;
        bottom: 16px;
        z-index: 2147483647;
        width: max-content;
        min-width: 180px;
        max-width: min(760px, calc(100vw - 28px));
        border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
        border-radius: 8px;
        background: color-mix(in srgb, Canvas 94%, transparent);
        color: CanvasText;
        box-shadow: 0 12px 36px color-mix(in srgb, CanvasText 18%, transparent);
        backdrop-filter: blur(16px);
        font: 12px/1.35 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
        overflow: hidden;
        user-select: none;
      }
      .cti-hud button {
        display: inline-grid;
        place-items: center;
        width: 28px;
        height: 24px;
        border: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
        border-radius: 6px;
        background: color-mix(in srgb, CanvasText 5%, transparent);
        color: inherit;
        font: 15px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        cursor: pointer;
        position: relative;
        z-index: 1;
      }
      .cti-hud-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 7px 9px;
        border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
        font-weight: 650;
        cursor: move;
        touch-action: none;
      }
      [data-cti-title] {
        cursor: pointer;
      }
      .cti-hud-tools {
        display: inline-flex;
        align-items: center;
        gap: 5px;
      }
      .cti-unit-group {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        border: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
        border-radius: 6px;
        padding: 2px;
        background: color-mix(in srgb, CanvasText 4%, transparent);
      }
      .cti-hud .cti-unit-button {
        width: auto;
        min-width: 30px;
        height: 20px;
        padding: 0 6px;
        border: 0;
        border-radius: 4px;
        background: transparent;
        font: 11px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }
      .cti-hud .cti-unit-button[data-active="true"] {
        background: color-mix(in srgb, CanvasText 12%, transparent);
      }
      .cti-hud[data-dragging="true"] {
        transition: none;
        opacity: 0.92;
      }
      .cti-hud-body {
        display: grid;
        gap: 4px;
        padding: 8px 9px;
        color: color-mix(in srgb, CanvasText 78%, transparent);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        white-space: normal;
        overflow-wrap: anywhere;
        max-height: 60vh;
        overflow-y: auto;
      }
      .cti-credit {
        margin-top: 3px;
        justify-self: end;
        text-align: right;
        color: color-mix(in srgb, CanvasText 44%, transparent);
        font: 10px/1.2 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      }
      .cti-hud[data-collapsed="true"] .cti-hud-body { display: none; }
      .cti-hud[data-collapsed="true"] .cti-unit-group { display: none; }
      .cti-reply-footer {
        margin-top: 8px;
        padding: 6px 8px;
        border: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
        border-radius: 6px;
        background: color-mix(in srgb, CanvasText 5%, transparent);
        color: color-mix(in srgb, CanvasText 68%, transparent);
        font: 11px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        overflow-wrap: anywhere;
      }
      .cti-reply-chip {
        display: block;
        box-sizing: border-box;
        width: 100%;
        max-width: 100%;
        margin-top: 6px;
        padding: 4px 7px;
        border: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
        border-radius: 6px;
        background: color-mix(in srgb, CanvasText 5%, transparent);
        color: color-mix(in srgb, CanvasText 62%, transparent);
        font: 11px/1.25 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        white-space: normal;
        overflow-wrap: anywhere;
      }
      .cti-sidebar-tooltip {
        position: fixed;
        z-index: 2147483647;
        max-width: min(420px, calc(100vw - 24px));
        padding: 12px 14px;
        border: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
        border-radius: 8px;
        background: color-mix(in srgb, Canvas 96%, transparent);
        color: CanvasText;
        box-shadow: 0 12px 36px color-mix(in srgb, CanvasText 18%, transparent);
        backdrop-filter: blur(16px);
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: normal;
        font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        pointer-events: none;
      }
      .cti-sidebar-credit {
        display: block;
        margin-top: 8px;
        text-align: right;
        color: color-mix(in srgb, CanvasText 44%, transparent);
        font: 11px/1.2 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      }
    `;
    if (style.textContent !== css) style.textContent = css;
  }
  function cleanOriginalTitle(value) {
    return String(value || '')
      .split(/\n{2,}(?=(?:Context|上下文)\s)/)[0]
      .replace(/\n?(?:Context|上下文)\s+[\s\S]*$/m, '')
      .trim();
  }
  function hideSidebarTooltip() {
    document.querySelector('.cti-sidebar-tooltip')?.remove();
  }
  function showSidebarTooltip(row, text) {
    hideSidebarTooltip();
    const tooltip = document.createElement('div');
    tooltip.className = 'cti-sidebar-tooltip';
    tooltip.lang = uiLanguage() === 'zh' ? 'zh-CN' : 'en';
    const content = document.createElement('div');
    content.textContent = text;
    const credit = document.createElement('span');
    credit.className = 'cti-sidebar-credit';
    credit.textContent = tr('madeBy');
    tooltip.append(content, credit);
    document.body.appendChild(tooltip);
    const rect = row.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - tooltipRect.width - 12, Math.max(12, rect.right + 8));
    const top = Math.min(window.innerHeight - tooltipRect.height - 12, Math.max(12, rect.top + 30));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }
  function installSidebarHoverDelegation() {
    const previous = window.__codexContextTokenInspectorSidebarDelegation;
    if (previous?.version === RUNTIME_VERSION) return;
    if (previous?.mouseover) document.removeEventListener('mouseover', previous.mouseover);
    if (previous?.mouseout) document.removeEventListener('mouseout', previous.mouseout);
    const mouseover = event => {
      const row = event.target?.closest?.(`[${SIDEBAR_HOVER_ATTR}]`);
      if (!row) return;
      setTimeout(() => showSidebarTooltip(row, row.getAttribute(SIDEBAR_HOVER_ATTR) || ''), 0);
    };
    const mouseout = event => {
      const row = event.target?.closest?.(`[${SIDEBAR_HOVER_ATTR}]`);
      if (!row) return;
      if (event.relatedTarget && row.contains(event.relatedTarget)) return;
      setTimeout(hideSidebarTooltip, 0);
    };
    document.addEventListener('mouseover', mouseover);
    document.addEventListener('mouseout', mouseout);
    window.__codexContextTokenInspectorSidebarDelegation = {
      version: RUNTIME_VERSION,
      mouseover,
      mouseout,
    };
  }
  function applyStoredHudPosition(root) {
    try {
      const position = JSON.parse(localStorage.getItem(POSITION_KEY) || 'null');
      if (!position || typeof position.left !== 'number' || typeof position.top !== 'number') return;
      root.style.left = `${Math.max(8, Math.min(window.innerWidth - 48, position.left))}px`;
      root.style.top = `${Math.max(8, Math.min(window.innerHeight - 32, position.top))}px`;
      root.style.right = 'auto';
      root.style.bottom = 'auto';
    } catch {}
  }
  function installHudDrag(root) {
    if (root.__ctiDragInstalled) return;
    root.__ctiDragInstalled = true;
    const head = root.querySelector('.cti-hud-head');
    let drag = null;
    const start = event => {
      if (event.target?.closest?.('[data-cti-toggle], [data-cti-unit], [data-cti-title]')) return;
      if (event.button !== undefined && event.button !== 0) return;
      const rect = root.getBoundingClientRect();
      drag = { dx: event.clientX - rect.left, dy: event.clientY - rect.top, x: event.clientX, y: event.clientY, moved: false };
      root.setAttribute('data-dragging', 'true');
      root.style.left = `${rect.left}px`;
      root.style.top = `${rect.top}px`;
      root.style.right = 'auto';
      root.style.bottom = 'auto';
      head.setPointerCapture?.(event.pointerId);
    };
    const move = event => {
      if (!drag) return;
      const width = root.offsetWidth || 48;
      const height = root.offsetHeight || 32;
      const left = Math.max(8, Math.min(window.innerWidth - width - 8, event.clientX - drag.dx));
      const top = Math.max(8, Math.min(window.innerHeight - height - 8, event.clientY - drag.dy));
      if (Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 3) {
        drag.moved = true;
      }
      root.style.left = `${left}px`;
      root.style.top = `${top}px`;
    };
    const end = () => {
      if (!drag) return;
      root.__ctiSuppressToggle = drag.moved;
      drag = null;
      root.removeAttribute('data-dragging');
      const rect = root.getBoundingClientRect();
      localStorage.setItem(POSITION_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
    };
    head.addEventListener('pointerdown', start);
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);
  }
  function keepTogglePosition(root, update) {
    const toggle = root.querySelector('[data-cti-toggle]');
    const before = toggle.getBoundingClientRect();
    update();
    const after = toggle.getBoundingClientRect();
    if (root.style.left && root.style.left !== 'auto') {
      const currentLeft = Number.parseFloat(root.style.left) || root.getBoundingClientRect().left;
      root.style.left = `${currentLeft + before.left - after.left}px`;
      const rect = root.getBoundingClientRect();
      localStorage.setItem(POSITION_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
    }
  }
  function updateUnitButtons(root) {
    root.querySelectorAll('[data-cti-unit]').forEach(button => {
      button.setAttribute('data-active', String(button.getAttribute('data-cti-unit') === unitMode()));
    });
  }
  function updateHudLanguage(root) {
    const language = uiLanguage();
    const htmlLanguage = language === 'zh' ? 'zh-CN' : 'en';
    if (root.lang !== htmlLanguage) root.lang = htmlLanguage;
    const unitGroup = root.querySelector('.cti-unit-group');
    if (unitGroup?.getAttribute('aria-label') !== tr('tokenUnit')) {
      unitGroup?.setAttribute('aria-label', tr('tokenUnit'));
    }
    const rawButton = root.querySelector('[data-cti-unit="raw"]');
    if (rawButton && rawButton.textContent !== tr('rawUnit')) rawButton.textContent = tr('rawUnit');
    const toggle = root.querySelector('[data-cti-toggle]');
    const toggleLabel = root.getAttribute('data-collapsed') === 'true' ? tr('expandMonitor') : tr('collapseMonitor');
    if (toggle?.getAttribute('aria-label') !== toggleLabel) toggle?.setAttribute('aria-label', toggleLabel);
  }
  function ensureHud() {
    let root = document.getElementById(ROOT_ID);
    if (root) {
      applyStoredHudPosition(root);
      installHudDrag(root);
      updateUnitButtons(root);
      updateHudLanguage(root);
      return root;
    }
    root = document.createElement('section');
    root.id = ROOT_ID;
    root.className = 'cti-hud';
    root.setAttribute('data-collapsed', localStorage.getItem(COLLAPSE_KEY) === 'true' ? 'true' : 'false');
    root.innerHTML = `
      <div class="cti-hud-head">
        <span data-cti-title>Monitor</span>
        <div class="cti-hud-tools">
          <div class="cti-unit-group" aria-label="Token unit">
            <button class="cti-unit-button" type="button" data-cti-unit="raw">raw</button>
            <button class="cti-unit-button" type="button" data-cti-unit="k">K</button>
            <button class="cti-unit-button" type="button" data-cti-unit="m">M</button>
          </div>
          <button type="button" data-cti-toggle>−</button>
        </div>
      </div>
      <div class="cti-hud-body" data-cti-body></div>
    `;
    root.querySelectorAll('[data-cti-unit]').forEach(button => {
      button.addEventListener('pointerdown', event => event.stopPropagation());
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        localStorage.setItem(UNIT_KEY, button.getAttribute('data-cti-unit'));
        applyAll(window.__codexContextTokenInspectorPayload);
      });
    });
    const titleButton = root.querySelector('[data-cti-title]');
    titleButton.addEventListener('pointerdown', event => {
      event.stopPropagation();
    });
    titleButton.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      if (root.getAttribute('data-collapsed') === 'true') {
        toggleHud(root);
      }
    });
    const toggleButton = root.querySelector('[data-cti-toggle]');
    toggleButton.addEventListener('pointerdown', event => {
      event.stopPropagation();
    });
    toggleButton.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      if (root.__ctiSuppressToggle) {
        root.__ctiSuppressToggle = false;
        return;
      }
      toggleHud(root);
    });
    toggleButton.addEventListener('pointerup', event => {
      if (!root.__ctiSuppressToggle) return;
      event.preventDefault();
      root.__ctiSuppressToggle = false;
    });
    function toggleHud(root) {
      keepTogglePosition(root, () => {
        const next = root.getAttribute('data-collapsed') !== 'true';
        root.setAttribute('data-collapsed', String(next));
        localStorage.setItem(COLLAPSE_KEY, String(next));
        root.querySelector('[data-cti-toggle]').textContent = next ? '+' : '−';
        updateHudTitle(root);
      });
    }
    document.body.appendChild(root);
    applyStoredHudPosition(root);
    installHudDrag(root);
    updateUnitButtons(root);
    updateHudLanguage(root);
    return root;
  }
  function applySidebar(summaries) {
    const byThread = new Map();
    summaries.forEach(item => {
      byThread.set(String(item.thread_id), item);
      (item.thread_keys || []).forEach(key => byThread.set(String(key), item));
    });
    document.querySelectorAll('[data-app-action-sidebar-thread-row]').forEach(row => {
      const id = rowThreadId(row);
      const item = byThread.get(String(id));
      if (!item) return;
      const existing = row.getAttribute('data-cti-original-title') || cleanOriginalTitle(row.getAttribute('title') || '');
      if (!row.hasAttribute('data-cti-original-title')) row.setAttribute('data-cti-original-title', existing);
      row.removeAttribute('title');
      row.setAttribute(SIDEBAR_HOVER_ATTR, summaryHover(item));
    });
  }
  function assistantNodes() {
    // Codex tasks and ChatGPT conversations currently use different turn
    // wrappers. Keep both paths so an app update can move a task between them.
    const selectors = [
      '[data-content-search-assistant-turn-key]',
      '[data-local-conversation-final-assistant]',
      '[data-chatgpt-conversation-turn="true"]',
    ];
    const seen = new Set();
    const nodes = [];
    for (const selector of selectors) {
      document.querySelectorAll(selector).forEach(node => {
        const element = node.closest('[data-content-search-assistant-turn-key]') ||
          node.closest('[data-chatgpt-conversation-turn="true"]') ||
          node;
        if (!seen.has(element)) {
          seen.add(element);
          nodes.push(element);
        }
      });
      if (nodes.length) break;
    }
    return nodes.filter(node => !node.closest(`#${ROOT_ID}`));
  }
  function actionRowForAssistant(node) {
    const turn = node.closest('[data-turn-key], [data-chatgpt-conversation-turn="true"]') || node;
    const sentTime = turn.querySelector('[data-assistant-message-sent-time]');
    if (sentTime?.parentElement) return sentTime.parentElement;
    const candidates = Array.from(turn.querySelectorAll('span, div')).filter(el => {
      if (el.closest(`#${ROOT_ID}`) || el.hasAttribute(CHIP_ATTR)) return false;
      const text = (el.textContent || '').trim();
      return /^Work(?:ing|ed) for /.test(text) || /\b\d{1,2}:\d{2}\s?(?:AM|PM)\b/.test(text);
    });
    const candidate = candidates.find(el => /^Work(?:ing|ed) for /.test((el.textContent || '').trim())) ||
      candidates.find(el => /\b\d{1,2}:\d{2}\s?(?:AM|PM)\b/.test((el.textContent || '').trim())) ||
      null;
    return candidate?.parentElement || null;
  }
  function assistantChipTargets() {
    const targetsByHost = new Map();
    assistantNodes().forEach(node => {
      const actionRow = actionRowForAssistant(node);
      const host = actionRow?.parentElement || node;
      if (!host) return;
      // Multi-step turns can expose several assistant wrappers for one native
      // action row. The action-row host is the visible reply boundary, so the
      // last wrapper for that host owns its single token chip.
      targetsByHost.set(host, { node, actionRow, host });
    });
    return Array.from(targetsByHost.values());
  }
  function directReplyChips(host) {
    return Array.from(host?.children || []).filter(child => child.hasAttribute(CHIP_ATTR));
  }
  function normalizedText(value) {
    return String(value || '')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/[`*~]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }
  function visibleItemForNode(node, index, items, used, visibleCount) {
    const turn = node.closest('[data-turn-key], [data-chatgpt-conversation-turn-id]');
    const keys = [turn?.getAttribute('data-turn-key'), turn?.getAttribute('data-chatgpt-conversation-turn-id')].filter(Boolean);
    let matches = items.map((item, i) => ({item, i})).filter(({item, i}) =>
      !used.has(i) && item.turnId && keys.includes(item.turnId));
    if (!matches.length) {
      const nodeText = normalizedText(node.textContent);
      matches = items.map((item, i) => ({item, i})).filter(({item, i}) => {
        const prefix = normalizedText(item.textPrefix);
        return !used.has(i) && prefix.length >= 12 && nodeText.includes(prefix);
      });
    }
    if (matches.length !== 1) return null;
    used.add(matches[0].i);
    return matches[0].item;
  }
  function applyFooters(detail) {
    if (!detail) return;
    const targets = assistantChipTargets();
    const items = detail.assistantItems || [];
    const used = new Set();
    const keptChips = new Set();
    targets.forEach(({ node, actionRow, host }, index) => {
      const item = visibleItemForNode(node, index, items, used, targets.length);
      const text = item?.footer;
      if (!text) return;
      node.querySelector(`[${FOOTER_ATTR}]`)?.remove();
      const sessionRound = item.roundIndex || index + 1;
      const sessionTotalRounds = item.totalRounds || items.length || targets.length;
      const extra = uiLanguage() === 'zh'
        ? ` | 工具 ${item.toolCount || 0} 次 | 压缩 ${token(item.compactionTokens || 0)}`
        : ` | Tools ${item.toolCount || 0} | Compaction ${token(item.compactionTokens || 0)}`;
      const quotaText = item.weeklyTotal == null ? '' : (uiLanguage() === 'zh'
        ? ` | 额度估算：累计 ${item.weeklyTotal.toFixed(2)} 个百分点 / 本周期 ${(item.weeklyCurrent == null ? '—' : item.weeklyCurrent.toFixed(2))}%`
        : ` | Quota estimate: lifetime ${item.weeklyTotal.toFixed(2)} pp / current ${(item.weeklyCurrent == null ? '—' : item.weeklyCurrent.toFixed(2))}%`);
      const chipText = itemChip(item, sessionRound, sessionTotalRounds) + extra + quotaText;
      const directChips = directReplyChips(host);
      // A v5 chip can be outside `node` after it is moved below the native
      // buttons. Look it up from the stable host first so refreshes reuse it.
      let chip = actionRow?.nextElementSibling?.hasAttribute(CHIP_ATTR)
        ? actionRow.nextElementSibling
        : directChips[0] || node.querySelector(`[${CHIP_ATTR}]`);
      if (!chip) {
        chip = document.createElement('div');
        chip.className = 'cti-reply-chip';
        chip.setAttribute(CHIP_ATTR, 'true');
      }
      keptChips.add(chip);
      chip.lang = uiLanguage() === 'zh' ? 'zh-CN' : 'en';
      if (actionRow?.parentElement === host) {
        // Keep Codex's fixed-height action row untouched. The chip is a sibling
        // immediately below it, so buttons and timestamps retain their layout.
        if (chip.parentElement !== host || chip.previousElementSibling !== actionRow) {
          actionRow.insertAdjacentElement('afterend', chip);
        }
      } else if (chip.parentElement !== host) {
        host.appendChild(chip);
      }
      if (chip.textContent !== chipText) chip.textContent = chipText;
      const toolText = (item.tools || []).map(t => `${t.name} × ${t.count} (${t.returned || 0} ${uiLanguage() === 'zh' ? '已返回' : 'returned'})`).join('\n');
      const title = itemTitle(item, sessionRound, sessionTotalRounds) + (toolText ? '\n' + toolText : '');
      if (chip.getAttribute('title') !== title) chip.setAttribute('title', title);
    });
    // Remove duplicates created by older runtimes and chips whose virtualized
    // reply host is no longer present. This also makes repeated refreshes
    // idempotent, preventing token rows from growing the scrollable content.
    document.querySelectorAll(`[${CHIP_ATTR}]`).forEach(chip => {
      if (!keptChips.has(chip)) chip.remove();
    });
  }
  function applyHud(payload, currentDetail = null) {
    const root = ensureHud();
    const body = root.querySelector('[data-cti-body]');
    updateHudTitle(root);
    const currentThreadId = currentDetail?.thread_id || activeThreadId() || null;
    const summaryForThread = threadId => {
      if (!threadId) return null;
      const keys = threadKeys(threadId);
      return payload.summaries.find(item =>
        keys.some(key => String(item.thread_id) === key || (item.thread_keys || []).includes(key))
      ) || null;
    };
    const selected =
      payload.summaries.find(item => String(item.thread_id) === String(currentDetail?.thread_id)) ||
      summaryForThread(currentThreadId);
    if (!selected) {
      root.__ctiSessionTotalTokens = null;
      updateHudTitle(root);
      if (body.textContent !== tr('noRecords')) body.textContent = tr('noRecords');
      return;
    }
    root.__ctiSessionTotalTokens = selected.session_total_tokens;
    updateHudTitle(root);
    const turnBreakdown = joined([
      `${tr('inputShort')} ${token(selected.latest_turn_input_tokens)}`,
      `${tr('cachedShort')} ${token(selected.latest_turn_cached_input_tokens)}`,
      `${tr('outputShort')} ${token(selected.latest_turn_output_tokens)}`,
      `${tr('reasoningShort')} ${token(selected.latest_turn_reasoning_tokens)}`,
    ]);
    const sessionBreakdown = joined([
      `${tr('inputShort')} ${token(selected.session_input_tokens)}`,
      `${tr('cachedShort')} ${token(selected.session_cached_input_tokens)}`,
      `${tr('outputShort')} ${token(selected.session_output_tokens)}`,
      `${tr('reasoningShort')} ${token(selected.session_reasoning_tokens)}`,
    ]);
    const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const zh = uiLanguage() === 'zh';
    const q = selected.weekly;
    const amount = value => value == null ? (zh ? '未知' : 'unknown') : `≈${value.toFixed(2)}`;
    const tools = (selected.latest_turn_tools || []).map(t => `${t.name} × ${t.count} (${t.returned || 0} ${uiLanguage() === 'zh' ? '已返回' : 'returned'})`).join(' · ');
    const warningText = {
      unobserved_legacy_prefix: ['早期累计用量已计入，但无法归属轮次与周期', 'Earlier cumulative usage is included; turn and time are unknown'],
      legacy_counter_decreased: ['旧格式计数回退，可能缺失用量', 'Legacy counter decreased; usage may be missing'],
      compaction_usage_unavailable: ['存在缺少用量的压缩记录；总数可能偏低', 'Compaction usage is missing; total may be low'],
      native_record_without_response_id: ['用量记录缺少响应标识', 'Usage record has no response ID'],
      account_identity_unavailable: ['账号标识缺失，可能混入其他账号', 'Account identity unavailable; accounts may be mixed'],
      conflicting_snapshots: ['快照冲突区间未分摊', 'Conflicting snapshot intervals excluded'],
      reset_boundary_unattributed: ['重置边界区间无法分摊', 'Reset boundary intervals cannot be attributed'],
      quota_delta_without_local_tokens: ['部分额度增长没有对应的本机 tokens', 'Some quota growth has no matching local tokens'],
      stale_quota_snapshot: ['已过期快照不参与分摊', 'Expired snapshots excluded'],
      current_cycle_snapshot_unavailable: ['缺少本周期快照', 'Current cycle snapshot unavailable'],
      unreadable_session_files: ['部分会话文件无法读取，分母不完整', 'Some session files are unreadable; denominator incomplete'],
      uncovered_tokens: ['部分 tokens 缺少前后额度快照', 'Some tokens lack surrounding quota snapshots'],
      session_not_indexed: ['当前会话未索引', 'Current session is not indexed'],
    };
    const warnings = [...new Set([...(selected.warnings || []), ...(q?.warnings || [])])]
      .filter(code => warningText[code]).map(code => `<div>${esc(warningText[code][zh ? 0 : 1])}</div>`).join('');
    const quotaHtml = q ? `<div>${zh ? '周额度分摊估算' : 'Weekly quota estimate'}: ${zh ? '本周期' : 'current'} ${amount(q.current_pp)}% / ${zh ? '跨周期累计' : 'lifetime'} ${amount(q.total_pp)} ${zh ? '个百分点' : 'pp'}</div>
      <div>${zh ? '本机 token 观测覆盖' : 'Local token coverage'}: ${q.coverage_percent == null ? '—' : q.coverage_percent + '%'} · ${zh ? '受其他设备、模型权重、快照精度及延迟影响；误差无法量化' : 'Other devices, model weighting, rounding and delay affect estimates; error is not quantifiable'}</div>
      <details><summary>${zh ? '各周期与数据缺口' : 'Cycles and gaps'}</summary>${(q.periods || []).map(p => `<div>${esc(new Date(p.resets_at*1000).toLocaleString())} ${zh ? '重置' : 'reset'} #${p.segment}: ≈${p.estimated_pp.toFixed(2)}%${p.current ? (zh ? '（本周期）' : ' (current)') : ''}</div>`).join('')}
      ${warnings}<div>${zh ? '未覆盖 tokens' : 'Uncovered tokens'}: ${token(q.uncovered_tokens || 0)}</div></details>` : '';
    const bodyHtml = `
      <div>${labeled('status', `${pressure(selected.latest_context_percent)} | ${tr('left')} ${token(remainingContext(selected))}`)}</div>
      <div>${labeled('context', `${token(selected.latest_context_tokens)} / ${token(selected.context_window)} ${parenthesized(pct(selected.latest_context_percent))}`)}</div>
      <div>${labeled('turn', `${token(selected.latest_turn_total_tokens)} ${parenthesized(turnBreakdown)}`)}</div>
      <div>${labeled('session', `${token(selected.session_total_tokens)} ${parenthesized(sessionBreakdown)}`)}</div>
      <div>${zh ? '压缩（已计入）' : 'Compaction (included)'}: ${token(selected.session_compaction_tokens || 0)}</div>
      <div>${zh ? '工具调用：本轮 / 会话' : 'Tools: turn / session'}: ${selected.latest_turn_tool_count || 0} / ${selected.session_tool_count || 0}</div>
      ${tools ? `<div>${esc(tools)}</div>` : ''}
      ${quotaHtml || warnings}
      <div class="cti-credit">${tr('madeBy')}</div>
    `;
    const cyclesExpanded = body.querySelector('details')?.open;
    if (body.innerHTML !== bodyHtml) body.innerHTML = bodyHtml;
    if (cyclesExpanded && body.querySelector('details')) body.querySelector('details').open = true;
    const toggle = root.querySelector('[data-cti-toggle]');
    toggle.textContent = root.getAttribute('data-collapsed') === 'true' ? '+' : '−';
    updateUnitButtons(root);
  }
  function updateHudTitle(root) {
    const title = root.querySelector('[data-cti-title]');
    if (!title) return;
    const collapsed = root.getAttribute('data-collapsed') === 'true';
    const total = root.__ctiSessionTotalTokens;
    const text = collapsed && total != null ? `${tr('monitor')} (ttk:${token(total)})` : tr('monitor');
    if (title.textContent !== text) title.textContent = text;
    updateHudLanguage(root);
  }
  function clearFooters() {
    document.querySelectorAll(`[${FOOTER_ATTR}]`).forEach(node => node.remove());
    document.querySelectorAll(`[${CHIP_ATTR}]`).forEach(node => node.remove());
  }
  function detailForCurrentThread(payload) {
    const details = payload.detailsByThread || {};
    for (const key of threadKeys(activeThreadId())) {
      if (details[key]) return details[key];
    }
    return null;
  }
  function scheduleDetailApply(payload) {
    if (window.__codexContextTokenInspectorDetailTimer) {
      clearTimeout(window.__codexContextTokenInspectorDetailTimer);
    }
    if (window.__codexContextTokenInspectorIdleCallback && window.cancelIdleCallback) {
      window.cancelIdleCallback(window.__codexContextTokenInspectorIdleCallback);
      window.__codexContextTokenInspectorIdleCallback = null;
    }
    const run = () => {
    window.__codexContextTokenInspectorDetailTimer = null;
    const work = () => {
      if (window.__codexContextTokenInspectorApplying) return;
      window.__codexContextTokenInspectorApplying = true;
      try {
        const currentDetail = detailForCurrentThread(payload);
        payload.currentDetailThreadId = currentDetail?.thread_id || null;
        applyHud(payload, currentDetail);
        if (currentDetail) {
          applyFooters(currentDetail);
        } else {
          clearFooters();
        }
      } finally {
        setTimeout(() => { window.__codexContextTokenInspectorApplying = false; }, 0);
      }
    };
      if (window.requestIdleCallback) {
        window.__codexContextTokenInspectorIdleCallback = window.requestIdleCallback(work, { timeout: 900 });
      } else {
        setTimeout(work, 0);
      }
    };
    window.__codexContextTokenInspectorDetailTimer = setTimeout(run, 220);
  }
  function applyAll(payload) {
    window.__codexContextTokenInspectorApplying = true;
    try {
      payload.activeThreadId = activeThreadId();
      applySidebar(payload.summaries || []);
      // Never transfer usage labels between conversations based on similar text.
      const currentDetail = detailForCurrentThread(payload);
      payload.currentDetailThreadId = currentDetail?.thread_id || null;
      applyHud(payload, currentDetail);
    } finally {
      setTimeout(() => { window.__codexContextTokenInspectorApplying = false; }, 0);
    }
    scheduleDetailApply(payload);
  }
  function installObserver(payload) {
    window.__codexContextTokenInspectorPayload = payload;
    if (window.__codexContextTokenInspectorObserver) return;
    let timer = null;
    const observer = new MutationObserver(records => {
      if (window.__codexContextTokenInspectorApplying) return;
      const activeChanged = records.some(record =>
        record.type === 'attributes' &&
        (record.attributeName === 'data-app-action-sidebar-thread-active' || record.attributeName === 'aria-current')
      );
      // Session switches deserve a fast path. Ordinary render churn is batched
      // to avoid repeatedly walking the message tree while a response streams.
      if (timer && !activeChanged) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        applyAll(window.__codexContextTokenInspectorPayload);
      }, activeChanged ? 80 : 300);
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-app-action-sidebar-thread-active', 'aria-current'],
    });
    window.__codexContextTokenInspectorObserver = observer;
  }

  function resetStaleRuntime() {
    if (!runtimeChanged) return;
    window.__codexContextTokenInspectorObserver?.disconnect();
    window.__codexContextTokenInspectorObserver = null;
    hideSidebarTooltip();
    if (window.__codexContextTokenInspectorDetailTimer) {
      clearTimeout(window.__codexContextTokenInspectorDetailTimer);
      window.__codexContextTokenInspectorDetailTimer = null;
    }
    if (window.__codexContextTokenInspectorIdleCallback && window.cancelIdleCallback) {
      window.cancelIdleCallback(window.__codexContextTokenInspectorIdleCallback);
      window.__codexContextTokenInspectorIdleCallback = null;
    }
    // Position, collapse state, and unit remain in localStorage and are restored
    // when the versioned HUD is recreated.
    document.getElementById(ROOT_ID)?.remove();
  }

  resetStaleRuntime();
  ensureDefaultUnit();
  ensureStyle();
  installSidebarHoverDelegation();
  installObserver(payload);
  applyAll(payload);
  return {
    ok: true,
    summaries: (payload.summaries || []).length,
    activeThreadId: activeThreadId(),
    selectedThreadId: payload.selectedThreadId,
    currentDetailThreadId: payload.currentDetailThreadId || null,
    assistantNodes: assistantNodes().length,
    replyChips: document.querySelectorAll(`[${CHIP_ATTR}]`).length,
  };
})
"""


def inject_once(client: CDPClient, roots: list[str], limit: int, detail_limit: int) -> Any:
    state = runtime_state(client)
    payload = build_payload(roots, limit, state.get("activeThreadId"), detail_limit=detail_limit)
    expression = f"({INJECTION_SCRIPT})({json.dumps(payload, ensure_ascii=False)})"
    return client.evaluate(expression)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Codex DevTools port.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum recent sessions to inspect.")
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=DETAIL_SESSION_LIMIT,
        help="Maximum recent sessions to parse for per-message chips.",
    )
    parser.add_argument("--interval", type=float, default=10.0, help="Refresh interval in seconds.")
    parser.add_argument("--once", action="store_true", help="Inject once and exit.")
    parser.add_argument("--quiet", action="store_true", help="Suppress successful refresh output.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Session JSONL files or directories. Defaults to ~/.codex/sessions and archived_sessions.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    roots = args.paths or [str(path) for path in inspector.DEFAULT_ROOTS]
    client: CDPClient | None = None
    last_error: str | None = None
    try:
        while True:
            try:
                if client is None:
                    target = select_target(devtools_targets(args.port))
                    client = CDPClient(str(target["webSocketDebuggerUrl"]))
                result = inject_once(client, roots, args.limit, args.detail_limit)
                if not args.quiet:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                last_error = None
            except (CDPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                if client is not None:
                    client.close()
                    client = None
                if args.once:
                    raise

                # A renderer can be replaced while the app remains open. Reconnect
                # in-process in that case; return to the launcher if CDP disappeared.
                try:
                    devtools_targets(args.port)
                except CDPError:
                    return 1
                message = f"Codex Monitor reconnecting after CDP error: {exc}"
                if message != last_error:
                    print(message, file=sys.stderr, flush=True)
                    last_error = message
                time.sleep(min(max(args.interval, 0.2), 2.0))
                continue
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
