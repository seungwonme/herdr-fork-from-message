from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from test_fork_from_message import CHILD, SOURCE, FakeClient
import fork_from_message as module
from runtime_registry import RegistryError


class HarnessWorkflowTests(unittest.TestCase):
    def workflow(self, client, **options):
        return module.ForkWorkflow(client, {"focused_pane_id": "w:p1", "workspace_id": "w"},
                                   {"HERDR_SOCKET_PATH": "/socket", "HERDR_PLUGIN_STATE_DIR": "/state"}, focus=False, **options)

    def test_omp_requires_authoritative_settings_before_destination(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", side_effect=RegistryError("missing owner")):
            client = FakeClient(cwd, "omp")
            with self.assertRaisesRegex(module.ForkError, "missing owner"):
                self.workflow(client).execute()
            self.assertFalse(any(call[:2] == ("pane", "split") for call in client.calls))

    def test_omp_keeps_launcher_roots_live_model_and_child_bridge(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "omp")
            records = [dict(session_id=SOURCE, environment={"XDG_STATE_HOME": "/owner/state"}, model="provider/live"), dict(session_id=SOURCE), dict(session_id=CHILD), dict(session_id=SOURCE)]
            launched = []
            def start(workflow, pane, argv):
                launched.extend(argv)
                client.child_argv = ["omp", "--fork", SOURCE]
            with patch.object(module, "read_record", side_effect=records), patch.object(module.ForkWorkflow, "_start_shell", autospec=True, side_effect=start):
                result = self.workflow(client).execute()
            self.assertEqual(result.fork_session_id, CHILD)
            self.assertIn("omp", launched)
            self.assertIn("provider/live", launched)
            self.assertIn("XDG_STATE_HOME=/owner/state", launched)
            self.assertIn("HERDR_PLUGIN_STATE_DIR", launched)
            self.assertNotIn("--extension", launched)
            self.assertFalse(any(call[:2] == ("agent", "start") for call in client.calls))

    def test_omp_read_only_requires_the_runtime_guard_bridge_before_destination(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", return_value=dict(session_id=SOURCE)):
            client = FakeClient(cwd, "omp")
            with self.assertRaisesRegex(module.ForkError, "runtime guard bridge"):
                self.workflow(client, mode="read-only").execute()
            self.assertFalse(any(call[:2] == ("pane", "split") for call in client.calls))

    def test_source_changed_during_startup_is_not_success(self):
        class SwitchingClient(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                if not child and self.source_reads >= 3:
                    pane["agent_session"]["value"] = CHILD
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaisesRegex(module.ForkError, "Source identity changed.*Destination preserved"):
                self.workflow(SwitchingClient(cwd, "codex")).execute()

    def test_working_source_is_not_forked(self):
        class WorkingClient(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                pane["agent_status"] = "working"
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            client = WorkingClient(cwd, "codex")
            with self.assertRaisesRegex(module.ForkError, "Source must be idle"):
                self.workflow(client).execute()
            self.assertEqual(client.child_argv, [])


if __name__ == "__main__":
    unittest.main()
