from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from test_fork_from_message import CHILD, SOURCE, FakeClient
import fork_from_message as module
from native_sessions import NativeSessionError
from runtime_registry import RegistryError


class HarnessWorkflowTests(unittest.TestCase):
    def workflow(self, client, **options):
        return module.ForkWorkflow(client, {"focused_pane_id": "w:p1", "workspace_id": "w"},
                                   {"HERDR_SOCKET_PATH": "/socket", "HERDR_PLUGIN_STATE_DIR": "/state"}, focus=False, **options)

    def test_unavailable_harnesses_never_resume_source_or_create_destination(self):
        for agent in ("cursor", "agy", "hermes"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd, agent)
                with self.assertRaisesRegex(module.ForkError, "Source was not resumed or changed"):
                    self.workflow(client).execute()
                self.assertFalse(any(call[:2] in (("pane", "split"), ("pane", "run"), ("agent", "start")) for call in client.calls))

    def test_pi_family_requires_authoritative_settings_before_destination(self):
        for agent in ("pi", "omo"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", side_effect=RegistryError("missing owner")):
                client = FakeClient(cwd, agent)
                with self.assertRaisesRegex(module.ForkError, "missing owner"):
                    self.workflow(client).execute()
                self.assertFalse(any(call[:2] == ("pane", "split") for call in client.calls))

    def test_pi_and_omo_keep_launcher_roots_and_child_bridge(self):
        for agent in ("pi", "omo"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd, agent)
                if agent == "omo":
                    client.argv = ["bun", "/opt/node_modules/@code-yeongyu/senpi/dist/cli.js", "--model", "old"]
                records = [dict(session_id=SOURCE, environment={"XDG_STATE_HOME": "/owner/state"}, model="provider/live"), dict(session_id=SOURCE), dict(session_id=CHILD), dict(session_id=SOURCE)]
                launched = []
                def start(workflow, pane, argv):
                    launched.extend(argv)
                    client.child_argv = [agent, "--fork", SOURCE]
                with patch.object(module, "read_record", side_effect=records), patch.object(module.ForkWorkflow, "_start_shell", autospec=True, side_effect=start):
                    result = self.workflow(client).execute()
                self.assertEqual(result.fork_session_id, CHILD)
                self.assertIn(agent, launched)
                self.assertIn("provider/live", launched)
                self.assertIn("XDG_STATE_HOME=/owner/state", launched)
                self.assertIn("HERDR_PLUGIN_STATE_DIR", launched)
                self.assertIn("--extension", launched)
                self.assertTrue(launched[-1].endswith("runtime_bridge.ts"))
                self.assertFalse(any(call[:2] == ("agent", "start") for call in client.calls))

    def test_new_read_only_modes_fail_before_destination(self):
        for agent in ("pi", "omo", "copilot", "opencode"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", return_value=dict(session_id=SOURCE, bridge_version=1)):
                client = FakeClient(cwd, agent)
                workflow = self.workflow(client, mode="read-only")
                if agent == "opencode":
                    workflow._native_reference = lambda pane, agent: "ses_source"
                with self.assertRaisesRegex(module.ForkError, "Read-only mode"):
                    workflow.execute()
                self.assertFalse(any(call[:2] == ("pane", "split") for call in client.calls))

    def test_copilot_resumes_only_native_new_id(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value=CHILD) as clone:
            client = FakeClient(cwd, "copilot")
            result = self.workflow(client).execute()
            clone.assert_called_once()
            self.assertEqual(clone.call_args.args[1], SOURCE)
            self.assertEqual(result.fork_session_id, CHILD)
            self.assertIn(CHILD, client.child_argv)
            self.assertNotIn(SOURCE, client.child_argv)
            self.assertFalse(any(call[:2] == ("pane", "run") for call in client.calls))

    def test_copilot_clone_failure_never_creates_or_resumes(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", side_effect=NativeSessionError("unsupported RPC")):
            client = FakeClient(cwd, "copilot")
            with self.assertRaisesRegex(module.ForkError, "unsupported RPC"):
                self.workflow(client).execute()
            self.assertEqual(client.child_argv, [])
            self.assertFalse(any(call[:2] == ("pane", "split") for call in client.calls))

    def test_copilot_rejects_incorrect_child_identity(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value="019fe92e-1111-7222-8333-444455556667"):
            client = FakeClient(cwd, "copilot")
            with self.assertRaisesRegex(module.ForkError, "different from the native fork result.*Destination preserved"):
                self.workflow(client).execute()

    def test_opencode_launches_mini_and_checks_non_uuid_ids(self):
        class OpenCodeClient(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                pane["agent_session"]["value"] = "ses_child" if child else "ses_source"
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            client = OpenCodeClient(cwd, "opencode")
            result = self.workflow(client).execute()
            self.assertEqual(result.fork_session_id, "ses_child")
            self.assertEqual(client.child_argv[:5], ["opencode", "mini", "--session", "ses_source", "--fork"])

    def test_source_changed_during_startup_is_not_success(self):
        class SwitchingClient(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                if not child and self.source_reads >= 3:
                    pane["agent_session"]["value"] = CHILD
                return pane
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value=CHILD):
            with self.assertRaisesRegex(module.ForkError, "Source identity changed.*Destination preserved"):
                self.workflow(SwitchingClient(cwd, "copilot")).execute()

    def test_working_source_is_not_forked(self):
        class WorkingClient(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                pane["agent_status"] = "working"
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            client = WorkingClient(cwd, "copilot")
            with self.assertRaisesRegex(module.ForkError, "Source must be idle"):
                self.workflow(client).execute()
            self.assertEqual(client.child_argv, [])


if __name__ == "__main__":
    unittest.main()
