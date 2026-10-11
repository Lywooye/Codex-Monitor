import importlib.util
import json
import os
import pathlib
import tempfile
import sys
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "context_token_inspector.py"
INJECTOR_PATH = PLUGIN_ROOT / "scripts" / "context_token_injector.py"


def load_module():
    spec = importlib.util.spec_from_file_location("context_token_inspector", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_injector():
    spec = importlib.util.spec_from_file_location("context_token_injector", INJECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContextTokenInspectorTests(unittest.TestCase):
    def write_session(self, rows):
        tmpdir = pathlib.Path(tempfile.mkdtemp())
        session = tmpdir / "rollout-2026-05-22T14-06-01-019e4e4a-demo.jsonl"
        with session.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return session

    def test_summarizes_latest_context_and_cumulative_usage(self):
        module = load_module()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "019e4e4a-demo",
                        "cwd": "/tmp/project",
                        "model_provider": "openai",
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:39.701Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 62670,
                                "cached_input_tokens": 27904,
                                "output_tokens": 1346,
                                "reasoning_output_tokens": 588,
                                "total_tokens": 64016,
                            },
                            "last_token_usage": {
                                "input_tokens": 39949,
                                "cached_input_tokens": 22400,
                                "output_tokens": 467,
                                "reasoning_output_tokens": 72,
                                "total_tokens": 40416,
                            },
                            "model_context_window": 258400,
                        },
                    },
                },
            ]
        )

        summary = module.summarize_session(session)

        self.assertEqual(summary["thread_id"], "019e4e4a-demo")
        self.assertEqual(summary["cwd"], "/tmp/project")
        self.assertEqual(summary["latest_context_tokens"], 39949)
        self.assertEqual(summary["latest_context_percent"], 15.5)
        self.assertEqual(summary["session_total_tokens"], 64016)
        self.assertEqual(summary["latest_turn_total_tokens"], 40416)

    def test_zero_context_is_reported_as_zero_percent(self):
        module = load_module()

        self.assertEqual(module.pct(0, 258400), 0.0)

    def test_thread_id_fallback_keeps_complete_uuid(self):
        module = load_module()
        path = pathlib.Path(
            "rollout-2026-07-15T13-00-25-019f6425-c770-7c13-951a-46e380cd5dfd.jsonl"
        )

        self.assertEqual(
            module.infer_thread_id(path),
            "019f6425-c770-7c13-951a-46e380cd5dfd",
        )

    def test_formats_reply_footer_line(self):
        module = load_module()
        summary = {
            "latest_context_tokens": 39949,
            "context_window": 258400,
            "latest_context_percent": 15.5,
            "latest_turn_total_tokens": 40416,
            "latest_turn_input_tokens": 39949,
            "latest_turn_output_tokens": 467,
            "latest_turn_reasoning_tokens": 72,
            "session_total_tokens": 64016,
        }

        footer = module.format_reply_footer(summary)

        self.assertEqual(
            footer,
            "context: 39,949 / 258,400 (15.5%) | turn: 40,416 tokens "
            "(in 39,949, out 467, reasoning 72) | session: 64,016 tokens",
        )

    def test_parses_messages_and_attaches_next_token_count_to_assistant_reply(self):
        module = load_module()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": "019e4e4a-demo", "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-22T06:06:07.320Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "build this"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:39.533Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:39.701Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 62670,
                                "cached_input_tokens": 27904,
                                "output_tokens": 1346,
                                "reasoning_output_tokens": 588,
                                "total_tokens": 64016,
                            },
                            "last_token_usage": {
                                "input_tokens": 39949,
                                "output_tokens": 467,
                                "reasoning_output_tokens": 72,
                                "total_tokens": 40416,
                            },
                            "model_context_window": 258400,
                        },
                    },
                },
            ]
        )

        detail = module.parse_session_detail(session)

        self.assertEqual(detail["summary"]["thread_id"], "019e4e4a-demo")
        self.assertEqual(len(detail["messages"]), 2)
        self.assertEqual(detail["messages"][1]["role"], "assistant")
        self.assertEqual(detail["messages"][1]["token_footer"], "context: 39,949 / 258,400 (15.5%) | turn: 40,416 tokens (in 39,949, out 467, reasoning 72) | session: 64,016 tokens")

    def test_injector_payload_keeps_sidebar_and_active_reply_data_compact(self):
        injector = load_injector()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": "019e4e4a-demo", "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-22T06:06:39.533Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:39.701Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 62670,
                                "cached_input_tokens": 27904,
                                "output_tokens": 1346,
                                "reasoning_output_tokens": 588,
                                "total_tokens": 64016,
                            },
                            "last_token_usage": {
                                "input_tokens": 39949,
                                "output_tokens": 467,
                                "reasoning_output_tokens": 72,
                                "total_tokens": 40416,
                            },
                            "model_context_window": 258400,
                        },
                    },
                },
            ]
        )

        payload = injector.build_payload([str(session.parent)], limit=10, selected_thread_id="local:019e4e4a-demo")

        self.assertEqual(payload["activeThreadId"], "local:019e4e4a-demo")
        self.assertEqual(payload["selectedThreadId"], "019e4e4a-demo")
        self.assertEqual(payload["summaries"][0]["badge"], "15.5% ctx")
        self.assertIn("local:019e4e4a-demo", payload["summaries"][0]["thread_keys"])
        self.assertIn("Session total  64,016", payload["summaries"][0]["hover"])
        self.assertIn("Cached input   27,904", payload["summaries"][0]["hover"])
        self.assertNotIn("Context  39,949 / 258,400", payload["summaries"][0]["hover"])
        self.assertNotIn("/tmp/project", payload["summaries"][0]["hover"])
        self.assertEqual(len(payload["detail"]["assistantFooters"]), 1)
        self.assertEqual(payload["detail"]["assistantChips"][0], "Token: Current 39,949/258,400 (15.5%) | Total 40,416/64,016   Rounds：User 1/1  | Assistant 1/1")

    def test_injector_payload_uses_session_rounds_for_historical_replies(self):
        injector = load_injector()
        rows = [
            {
                "timestamp": "2026-05-22T06:06:02.814Z",
                "type": "session_meta",
                "payload": {"id": "019e4e4a-demo", "cwd": "/tmp/project"},
            },
        ]
        for index in range(1, 4):
            rows.extend(
                [
                    {"type":"event_msg", "payload":{"type":"task_started", "turn_id":f"t{index}"}},
                    {
                        "timestamp": f"2026-05-22T06:0{index}:10.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": f"assistant reply {index}"}],
                        },
                    },
                    {
                        "timestamp": f"2026-05-22T06:0{index}:11.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 10000 * index,
                                    "cached_input_tokens": 5000 * index,
                                    "output_tokens": 100 * index,
                                    "reasoning_output_tokens": 20 * index,
                                    "total_tokens": 10120 * index,
                                },
                                "last_token_usage": {
                                    "input_tokens": 3000 * index,
                                    "cached_input_tokens": 1200 * index,
                                    "output_tokens": 100,
                                    "reasoning_output_tokens": 20,
                                    "total_tokens": 3120 * index,
                                },
                                "model_context_window": 258400,
                            },
                        },
                    },
                ]
            )
        session = self.write_session(rows)

        payload = injector.build_payload([str(session.parent)], limit=10, selected_thread_id="019e4e4a-demo")
        items = payload["detail"]["assistantItems"]

        self.assertEqual([item["roundIndex"] for item in items], [1, 2, 3])
        self.assertEqual([item["totalRounds"] for item in items], [3, 3, 3])
        self.assertTrue(payload["detail"]["assistantChips"][2].endswith("Rounds：User 3/3  | Assistant 3/3"))

    def test_rounds_count_user_turns_not_assistant_status_messages(self):
        injector = load_injector()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": "019e4e4a-demo", "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-22T06:06:03.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first request"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:04.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "first assistant status"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:05.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 1000},
                            "last_token_usage": {"input_tokens": 400, "total_tokens": 500},
                            "model_context_window": 1000,
                        },
                    },
                },
                {
                    "timestamp": "2026-05-22T06:07:03.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "second request"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:07:04.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "second assistant status"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:07:05.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 2000},
                            "last_token_usage": {"input_tokens": 500, "total_tokens": 600},
                            "model_context_window": 1000,
                        },
                    },
                },
                {
                    "timestamp": "2026-05-22T06:07:06.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "second assistant final"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:07:07.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 3000},
                            "last_token_usage": {"input_tokens": 600, "total_tokens": 700},
                            "model_context_window": 1000,
                        },
                    },
                },
            ]
        )

        payload = injector.build_payload([str(session.parent)], limit=10, selected_thread_id="019e4e4a-demo")
        items = payload["detail"]["assistantItems"]

        self.assertEqual([item["roundIndex"] for item in items], [1, 2])
        self.assertEqual([item["totalRounds"] for item in items], [2, 2])
        self.assertTrue(payload["detail"]["assistantChips"][0].endswith("Rounds：User 1/2  | Assistant 1/2"))
        self.assertTrue(payload["detail"]["assistantChips"][1].endswith("Rounds：User 2/2  | Assistant 2/2"))

    def test_injector_payload_prefers_latest_session_over_stale_active_thread(self):
        injector = load_injector()
        tmpdir = pathlib.Path(tempfile.mkdtemp())
        old_session = tmpdir / "rollout-2026-05-20T19-19-12-019e451c-old.jsonl"
        new_session = tmpdir / "rollout-2026-05-22T14-06-01-019e4e4a-new.jsonl"

        def write(path, session_id, replies):
            rows = [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp/project"},
                },
            ]
            for index in range(1, replies + 1):
                rows.extend(
                    [
                        {"type":"event_msg", "payload":{"type":"task_started", "turn_id":f"t{index}"}},
                        {
                            "timestamp": f"2026-05-22T06:{index:02d}:10.000Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": f"reply {index}"}],
                            },
                        },
                        {
                            "timestamp": f"2026-05-22T06:{index:02d}:11.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {"total_tokens": 1000 * index},
                                    "last_token_usage": {"input_tokens": 100 * index, "total_tokens": 200 * index},
                                    "model_context_window": 1000,
                                },
                            },
                        },
                    ]
                )
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

        write(old_session, "019e451c-old", 7)
        write(new_session, "019e4e4a-new", 2)
        os.utime(old_session, (1_700_000_000, 1_700_000_000))
        os.utime(new_session, (1_700_000_100, 1_700_000_100))

        payload = injector.build_payload([str(tmpdir)], limit=10, selected_thread_id="local:019e451c-old")

        self.assertEqual(payload["activeThreadId"], "local:019e451c-old")
        self.assertEqual(payload["selectedThreadId"], "019e4e4a-new")
        self.assertEqual(payload["detail"]["thread_id"], "019e4e4a-new")
        self.assertEqual(len(payload["detail"]["assistantItems"]), 2)
        self.assertTrue(payload["detail"]["assistantChips"][1].endswith("Rounds：User 2/2  | Assistant 2/2"))

    def test_injector_payload_includes_active_session_detail_outside_default_window(self):
        injector = load_injector()
        tmpdir = pathlib.Path(tempfile.mkdtemp())

        def write(path, session_id, total_tokens):
            rows = [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-22T06:06:04.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"reply {session_id}"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:05.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": total_tokens},
                            "last_token_usage": {"input_tokens": 100, "total_tokens": 200},
                            "model_context_window": 1000,
                        },
                    },
                },
            ]
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

        for index in range(5):
            path = tmpdir / f"rollout-2026-05-22T06-0{index}-00-new-{index}.jsonl"
            write(path, f"new-{index}", 1000 + index)
            os.utime(path, (1_700_000_100 + index, 1_700_000_100 + index))
        old_path = tmpdir / "rollout-2026-05-19T18-18-03-019e3fbe-old.jsonl"
        write(old_path, "019e3fbe-old", 9999)
        os.utime(old_path, (1_700_000_000, 1_700_000_000))

        payload = injector.build_payload(
            [str(tmpdir)],
            limit=10,
            selected_thread_id="local:019e3fbe-old",
            detail_limit=2,
        )

        details = {detail["thread_id"] for detail in payload["detailsByThread"].values()}
        self.assertIn("019e3fbe-old", details)

    def test_injector_payload_includes_active_session_outside_summary_limit(self):
        injector = load_injector()
        tmpdir = pathlib.Path(tempfile.mkdtemp())

        def write(path, session_id, total_tokens):
            rows = [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-22T06:06:04.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"reply {session_id}"}],
                    },
                },
                {
                    "timestamp": "2026-05-22T06:06:05.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": total_tokens},
                            "last_token_usage": {"input_tokens": 1000, "total_tokens": 2000},
                            "model_context_window": 258400,
                        },
                    },
                },
            ]
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

        active_path = tmpdir / "rollout-2026-05-20T01-00-00-019e4e4a-active.jsonl"
        write(active_path, "019e4e4a-active", 81000)
        os.utime(active_path, (1_700_000_000, 1_700_000_000))
        for index in range(3):
            path = tmpdir / f"rollout-2026-05-22T01-00-0{index}-019e4e4a-new-{index}.jsonl"
            write(path, f"019e4e4a-new-{index}", 500000 + index)
            os.utime(path, (1_700_000_100 + index, 1_700_000_100 + index))

        payload = injector.build_payload([str(tmpdir)], limit=2, selected_thread_id="local:019e4e4a-active")

        self.assertIn("019e4e4a-active", [item["thread_id"] for item in payload["summaries"]])
        self.assertEqual(payload["detailsByThread"]["019e4e4a-active"]["thread_id"], "019e4e4a-active")
        self.assertEqual(
            payload["detailsByThread"]["019e4e4a-active"]["assistantItems"][0]["tokenUsage"]["session_total_tokens"],
            81000,
        )

    def test_injection_bootstrap_preserves_existing_dom(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT
        bootstrap = script.split("installSidebarHoverDelegation();", 1)[1].split("installObserver(payload);", 1)[0]

        self.assertIn("const RUNTIME_VERSION = 7;", script)
        self.assertIn("if (!runtimeChanged) return;", script)
        self.assertIn("document.getElementById(ROOT_ID)?.remove();", script)
        self.assertIn("__codexContextTokenInspectorObserver?.disconnect", script)
        self.assertNotIn("querySelectorAll(`[${BADGE_ATTR}]`).forEach(node => node.remove())", bootstrap)
        self.assertNotIn("querySelectorAll(`[${FOOTER_ATTR}]`).forEach(node => node.remove())", bootstrap)
        self.assertNotIn("querySelectorAll(`[${CHIP_ATTR}]`).forEach(node => node.remove())", bootstrap)

    def test_apply_all_prefers_active_session_detail_for_hud(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT
        apply_all = script.split("function applyAll(payload)", 1)[1].split("function installObserver(payload)", 1)[0]

        self.assertIn("const currentDetail = detailForCurrentThread(payload);", apply_all)
        self.assertIn("applyHud(payload, currentDetail);", apply_all)
        self.assertNotIn("applyHud(payload, detailForCurrentThread(payload));", apply_all)

    def test_injection_supports_integrated_chatgpt_turns_without_rewriting_stable_dom(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT

        self.assertIn('[data-chatgpt-conversation-turn="true"]', script)
        self.assertIn('data-app-action-sidebar-thread-active="true"', script)
        self.assertIn('aria-current="page"', script)
        self.assertIn("activeChanged ? 80 : 300", script)
        self.assertIn("attributeFilter: ['data-app-action-sidebar-thread-active', 'aria-current']", script)
        self.assertNotIn("row.__ctiSidebarHoverInstalled", script)
        self.assertNotIn(
            "document.querySelector('[data-app-action-sidebar-thread-row][data-app-action-sidebar-thread-active]') ||\n"
            "      document.querySelector('[data-app-action-sidebar-thread-active]')",
            script,
        )
        self.assertIn("[data-assistant-message-sent-time]", script)
        self.assertIn("function actionRowForAssistant(node)", script)
        self.assertIn("function assistantChipTargets()", script)
        self.assertIn("targetsByHost.set(host, { node, actionRow, host });", script)
        self.assertIn("actionRow.insertAdjacentElement('afterend', chip);", script)
        self.assertIn("host.appendChild(chip);", script)
        self.assertNotIn("target.parentElement.appendChild(chip)", script)
        self.assertNotIn("node.insertAdjacentElement('afterbegin', chip)", script)
        self.assertIn("if (chip.textContent !== chipText)", script)
        self.assertIn("if (body.innerHTML !== bodyHtml)", script)

    def test_reply_chips_are_reused_by_action_row_host_and_deduplicated(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT
        apply_footers = script.split("function applyFooters(detail)", 1)[1].split("function applyHud", 1)[0]

        self.assertIn("const directChips = directReplyChips(host);", apply_footers)
        self.assertIn("actionRow?.nextElementSibling?.hasAttribute(CHIP_ATTR)", apply_footers)
        self.assertIn("const keptChips = new Set();", apply_footers)
        self.assertIn("keptChips.add(chip);", apply_footers)
        self.assertIn("if (!keptChips.has(chip)) chip.remove();", apply_footers)
        self.assertNotIn("let chip = node.querySelector(`[${CHIP_ATTR}]`);", apply_footers)

    def test_reply_chip_uses_a_full_width_wrapping_row(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT
        chip_css = script.split(".cti-reply-chip {", 1)[1].split("}", 1)[0]

        self.assertIn("display: block;", chip_css)
        self.assertIn("width: 100%;", chip_css)
        self.assertIn("max-width: 100%;", chip_css)
        self.assertIn("white-space: normal;", chip_css)
        self.assertIn("overflow-wrap: anywhere;", chip_css)
        self.assertNotIn("text-overflow: ellipsis;", chip_css)

    def test_injected_ui_localizes_chinese_and_falls_back_to_english(self):
        injector = load_injector()
        script = injector.INJECTION_SCRIPT

        self.assertIn("document.documentElement.lang || navigator.language || 'en'", script)
        self.assertIn("return language.startsWith('zh') ? 'zh' : 'en';", script)
        self.assertIn("monitor: 'Monitor'", script)
        self.assertIn("monitor: '监控'", script)
        self.assertIn("sessionTotal: '会话总计'", script)
        self.assertIn("madeBy: 'Kevin KE 制作'", script)
        self.assertIn("credit.textContent = tr('madeBy');", script)
        self.assertIn("const text = collapsed && total != null ? `${tr('monitor')}", script)

    def test_target_selection_prefers_codex_app_renderer(self):
        injector = load_injector()
        targets = [
            {
                "type": "page",
                "title": "Utility",
                "url": "https://example.test",
                "webSocketDebuggerUrl": "ws://127.0.0.1/utility",
            },
            {
                "type": "page",
                "title": "ChatGPT",
                "url": "app://codex/index.html",
                "webSocketDebuggerUrl": "ws://127.0.0.1/codex",
            },
        ]

        selected = injector.select_target(targets)

        self.assertEqual(selected["webSocketDebuggerUrl"], "ws://127.0.0.1/codex")

    def test_target_selection_avoids_integrated_avatar_overlay(self):
        injector = load_injector()
        targets = [
            {
                "type": "page",
                "title": "ChatGPT",
                "url": "app://-/index.html?initialRoute=%2Favatar-overlay",
                "webSocketDebuggerUrl": "ws://127.0.0.1/avatar",
            },
            {
                "type": "page",
                "title": "ChatGPT",
                "url": "app://-/index.html",
                "webSocketDebuggerUrl": "ws://127.0.0.1/main",
            },
        ]

        selected = injector.select_target(targets)

        self.assertEqual(selected["webSocketDebuggerUrl"], "ws://127.0.0.1/main")

    def test_target_selection_rejects_foreign_devtools_pages(self):
        injector = load_injector()
        targets = [
            {
                "type": "page",
                "title": "Example",
                "url": "https://example.test",
                "webSocketDebuggerUrl": "ws://127.0.0.1/example",
            }
        ]

        with self.assertRaises(injector.CDPError):
            injector.select_target(targets)

    def test_injector_accepts_quiet_mode(self):
        injector = load_injector()
        args = injector.build_arg_parser().parse_args(["--quiet"])

        self.assertTrue(args.quiet)

    def test_detail_cache_reuses_unchanged_session_and_extends_on_append(self):
        injector = load_injector()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:06:02.814Z",
                    "type": "session_meta",
                    "payload": {"id": "019e4e4a-demo"},
                }
            ]
        )
        summary = {"path": str(session), "thread_id": "019e4e4a-demo"}

        first = injector.cached_session_detail(str(session), summary)
        second = injector.cached_session_detail(str(session), summary)
        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "later",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "next turn"}],
                        },
                    }
                )
                + "\n"
            )
        third = injector.cached_session_detail(str(session), summary)

        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(third["_current_turn_index"], 1)

    def test_incremental_detail_matches_full_parse(self):
        injector = load_injector()
        session = self.write_session(
            [
                {
                    "timestamp": "2026-05-22T06:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "019e4e4a-demo"},
                },
                {
                    "timestamp": "2026-05-22T06:01:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first"}],
                    },
                },
            ]
        )
        summary = {"path": str(session), "thread_id": "019e4e4a-demo"}
        injector.cached_session_detail(str(session), summary)
        appended = [
            {
                "timestamp": "2026-05-22T06:01:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "reply"}],
                },
            },
            {
                "timestamp": "2026-05-22T06:01:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 200},
                        "last_token_usage": {"input_tokens": 100, "total_tokens": 120},
                        "model_context_window": 1000,
                    },
                },
            },
        ]
        with session.open("a", encoding="utf-8") as handle:
            for row in appended:
                handle.write(json.dumps(row) + "\n")

        incremental = injector.cached_session_detail(str(session), summary)
        full = injector.inspector.parse_session_detail(str(session), summary=summary)

        self.assertEqual(incremental["messages"], full["messages"])
        self.assertEqual(
            incremental["_current_turn_index"],
            full["_current_turn_index"],
        )


if __name__ == "__main__":
    unittest.main()
