from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "fork_from_message.py"
SPEC = importlib.util.spec_from_file_location("fork_from_message", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SOURCE_ID = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
FORK_ID = "019fe92e-1111-7222-8333-444455556666"


class FakeClient:
    def __init__(
        self,
        cwd: str,
        *,
        agent: str = "codex",
        session=SOURCE_ID,
        session_source: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.agent = agent
        self.session = session
        self.session_source = session_source or f"herdr:{agent}"
        self.commands: list[tuple[str, ...]] = []
        self.fail_start = False
        self.fail_close = False
        self.start_busy_attempts = 0
        self.fork_session = FORK_ID
        self.show_picker = True
        self.show_backtrack_selection = True
        self.show_claude_picker = True
        self.show_hook_review = False
        self.missing_root_pane = False

    def json(self, args, timeout=15):
        command = tuple(args)
        self.commands.append(command)
        if command[:2] == ("pane", "get"):
            agent_session = None
            if self.session is not None:
                agent_session = {
                    "agent": self.agent,
                    "kind": "id",
                    "source": self.session_source,
                    "value": self.session,
                }
            return {
                "result": {
                    "pane": {
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                        "agent": self.agent,
                        "agent_session": agent_session,
                        "foreground_cwd": self.cwd,
                        "cwd": self.cwd,
                    }
                }
            }
        if command[:2] == ("tab", "create"):
            root_pane = {} if self.missing_root_pane else {"pane_id": "w1:p2"}
            return {"result": {"tab": {"tab_id": "w1:t2"}, "root_pane": root_pane}}
        if command[:2] == ("agent", "start"):
            if self.start_busy_attempts:
                self.start_busy_attempts -= 1
                raise MODULE.ForkError("agent_pane_busy")
            if self.fail_start:
                raise MODULE.ForkError("simulated launch failure")
            return {"result": {"type": "agent_started"}}
        if command[:2] == ("agent", "get"):
            agent_session = None
            if self.fork_session is not None:
                agent_session = {
                    "agent": self.agent,
                    "kind": "id",
                    "source": f"herdr:{self.agent}",
                    "value": self.fork_session,
                }
            return {
                "result": {
                    "agent": {
                        "agent_session": agent_session
                    }
                }
            }
        if command[:2] == ("tab", "close") and self.fail_close:
            raise MODULE.ForkError("simulated close failure")
        if command[:2] in (("agent", "send-keys"), ("tab", "focus"), ("tab", "close")):
            return {"result": {"type": "ok"}}
        raise AssertionError(f"unexpected command: {command}")

    def text(self, args, timeout=15):
        command = tuple(args)
        self.commands.append(command)
        if command[:2] != ("agent", "read"):
            raise AssertionError(f"unexpected text command: {command}")
        if self.show_hook_review:
            return "Hooks need review"
        if self.agent == "claude":
            picker_opened = ("agent", "send-keys", "w1:p2", "esc", "esc") in self.commands
            if picker_opened and self.show_claude_picker:
                return "Rewind\nRestore the code and/or conversation to the point before…"
            return "Claude ready"
        if not self.show_picker:
            return "Codex ready"
        if ("agent", "send-keys", "w1:p2", "esc") in self.commands:
            if self.show_backtrack_selection:
                return "Esc/Left to edit prev  Right to edit next  Enter to edit message"
            return "Esc to edit prev"
        return "Esc to edit prev"


def context(cwd: str):
    return {
        "focused_pane_id": "w1:p1",
        "focused_pane_cwd": cwd,
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "tab_label": "Original task",
    }


class ForkWorkflowTests(unittest.TestCase):
    def test_success_forks_exact_session_and_opens_picker_before_focus(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            result = MODULE.ForkWorkflow(client, context(cwd)).execute()

        self.assertEqual(result.source_session_id, SOURCE_ID)
        self.assertEqual(result.fork_session_id, FORK_ID)
        start = next(command for command in client.commands if command[:2] == ("agent", "start"))
        self.assertEqual(start[-3:], ("--", "fork", SOURCE_ID))
        send_index = client.commands.index(("agent", "send-keys", "w1:p2", "ctrl+t"))
        select_index = client.commands.index(("agent", "send-keys", "w1:p2", "esc"))
        focus_index = client.commands.index(("tab", "focus", "w1:t2"))
        self.assertLess(send_index, select_index)
        self.assertLess(select_index, focus_index)
        self.assertNotIn(("tab", "close", "w1:t1"), client.commands)
        self.assertNotIn(("agent", "send-keys", "w1:p1", "ctrl+t"), client.commands)
        self.assertNotIn(("agent", "send-keys", "w1:p1", "esc"), client.commands)

    def test_claude_success_forks_exact_session_and_opens_rewind_before_focus(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, agent="claude")
            result = MODULE.ForkWorkflow(client, context(cwd)).execute()

        self.assertEqual(result.source_session_id, SOURCE_ID)
        self.assertEqual(result.fork_session_id, FORK_ID)
        start = next(command for command in client.commands if command[:2] == ("agent", "start"))
        self.assertEqual(start[start.index("--kind") + 1], "claude")
        self.assertEqual(start[-4:], ("--", "--resume", SOURCE_ID, "--fork-session"))
        picker_index = client.commands.index(("agent", "send-keys", "w1:p2", "esc", "esc"))
        focus_index = client.commands.index(("tab", "focus", "w1:t2"))
        self.assertLess(picker_index, focus_index)
        self.assertNotIn(("agent", "send-keys", "w1:p2", "ctrl+t"), client.commands)
        self.assertNotIn(("tab", "close", "w1:t1"), client.commands)

    def test_explicit_codex_home_is_preserved_in_new_tab(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as codex_home:
            client = FakeClient(cwd)
            MODULE.ForkWorkflow(client, context(cwd), codex_home=codex_home).execute()
        create = next(command for command in client.commands if command[:2] == ("tab", "create"))
        env_index = create.index("--env")
        self.assertEqual(create[env_index + 1], f"CODEX_HOME={codex_home}")

    def test_claude_preserves_only_its_explicit_config_directory(self):
        with (
            tempfile.TemporaryDirectory() as cwd,
            tempfile.TemporaryDirectory() as codex_home,
            tempfile.TemporaryDirectory() as claude_config_dir,
        ):
            client = FakeClient(cwd, agent="claude")
            MODULE.ForkWorkflow(
                client,
                context(cwd),
                codex_home=codex_home,
                claude_config_dir=claude_config_dir,
            ).execute()
        create = next(command for command in client.commands if command[:2] == ("tab", "create"))
        env_index = create.index("--env")
        self.assertEqual(create[env_index + 1], f"CLAUDE_CONFIG_DIR={claude_config_dir}")
        self.assertNotIn(f"CODEX_HOME={codex_home}", create)

    def test_new_shell_busy_race_is_retried(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.start_busy_attempts = 1
            with patch.object(MODULE.time, "sleep"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        starts = [command for command in client.commands if command[:2] == ("agent", "start")]
        self.assertEqual(len(starts), 2)

    def test_unsupported_pane_fails_before_tab_creation(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, agent="gemini")
            with self.assertRaisesRegex(MODULE.ForkError, "only Codex and Claude are supported"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertFalse(any(command[:2] == ("tab", "create") for command in client.commands))

    def test_claude_requires_herdr_claude_session_source(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, agent="claude", session_source="untrusted:claude")
            with self.assertRaisesRegex(MODULE.ForkError, "trusted Claude session ID"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertFalse(any(command[:2] == ("tab", "create") for command in client.commands))

    def test_missing_session_id_never_falls_back_to_fresh_codex(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, session=None)
            with self.assertRaisesRegex(MODULE.ForkError, "no trusted Codex session ID"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertFalse(any(command[:2] == ("tab", "create") for command in client.commands))
        self.assertFalse(any(command[:2] == ("agent", "start") for command in client.commands))

    def test_invalid_session_id_is_rejected_as_data_not_shell_input(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, session="$(touch /tmp/not-allowed)")
            with self.assertRaisesRegex(MODULE.ForkError, "invalid session ID"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertFalse(any(command[:2] == ("tab", "create") for command in client.commands))

    def test_launch_failure_closes_only_the_new_tab(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fail_start = True
            with self.assertRaisesRegex(MODULE.ForkError, "launch failure"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)
        self.assertNotIn(("tab", "close", "w1:t1"), client.commands)

    def test_incomplete_tab_response_is_cleaned_up_by_returned_tab_id(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.missing_root_pane = True
            with self.assertRaisesRegex(MODULE.ForkError, "did not return"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_cleanup_failure_identifies_tab_for_manual_close(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fail_start = True
            client.fail_close = True
            with self.assertRaisesRegex(MODULE.ForkError, "w1:t2.*Close that tab manually"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()

    def test_deferred_fork_session_report_is_allowed(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fork_session = None
            result = MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIsNone(result.fork_session_id)
        self.assertNotIn(("tab", "close", "w1:t2"), client.commands)

    def test_source_session_reported_in_fork_pane_is_rejected(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fork_session = SOURCE_ID
            with self.assertRaisesRegex(MODULE.ForkError, "source session ID"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_hook_review_is_reported_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fork_session = None
            client.show_hook_review = True
            with self.assertRaisesRegex(MODULE.ForkError, "hook trust review"):
                MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_picker_timeout_cleans_up(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.show_backtrack_selection = False
            with patch.object(MODULE, "PICKER_TIMEOUT_SECONDS", -1):
                with self.assertRaisesRegex(MODULE.ForkError, "message selection did not appear"):
                    MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_claude_picker_timeout_cleans_up(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, agent="claude")
            client.show_claude_picker = False
            with patch.object(MODULE, "PICKER_TIMEOUT_SECONDS", -1):
                with self.assertRaisesRegex(MODULE.ForkError, "Claude Rewind picker did not appear"):
                    MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_transcript_timeout_does_not_send_selection_key_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.show_picker = False
            with patch.object(MODULE, "TRANSCRIPT_TIMEOUT_SECONDS", -1):
                with self.assertRaisesRegex(MODULE.ForkError, "transcript did not appear"):
                    MODULE.ForkWorkflow(client, context(cwd)).execute()
        self.assertNotIn(("agent", "send-keys", "w1:p2", "esc"), client.commands)
        self.assertIn(("tab", "close", "w1:t2"), client.commands)

    def test_context_parser_requires_plugin_context(self):
        with self.assertRaisesRegex(MODULE.ForkError, "Invoke this through"):
            MODULE.load_invocation_context({})
        parsed = MODULE.load_invocation_context({"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"x": 1})})
        self.assertEqual(parsed, {"x": 1})


if __name__ == "__main__":
    unittest.main()
