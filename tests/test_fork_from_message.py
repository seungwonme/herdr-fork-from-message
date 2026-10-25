from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fork_from_message as module
from agent_contracts import ContractError, QUESTION_POLICY, fork_launch, safe_launch_flags
from runtime_registry import RegistryError, read_record, state_root, write_record

SOURCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
CHILD = "019fe92e-1111-7222-8333-444455556666"


class FakeClient:
    def __init__(self, cwd, agent="claude"):
        self.cwd, self.agent, self.calls = cwd, agent, []
        self.fail_start = False
        self.blocked = False
        self.same_id = False
        self.missing_session = False
        self.changed_source = False
        self.source_reads = 0
        self.argv = [agent, "--model", "chosen-model"]
        self.child_argv = []

    def pane(self, child=False):
        sid = SOURCE if not child or self.same_id else CHILD
        if not child:
            self.source_reads += 1
            if self.changed_source and self.source_reads > 1:
                sid = CHILD
        value = {"pane_id": "w:p2" if child else "w:p1", "workspace_id": "w", "tab_id": "w:t1",
                 "terminal_id": "child-terminal" if child else "source-terminal", "agent": self.agent,
                 "agent_status": "blocked" if self.blocked and child else "idle", "cwd": self.cwd,
                 "foreground_cwd": self.cwd}
        if not self.missing_session:
            value["agent_session"] = {"agent": self.agent, "kind": "id", "source": "herdr:" + self.agent, "value": sid}
        return value

    def json(self, args, timeout=15):
        self.calls.append(tuple(args))
        op = tuple(args[:2])
        if op == ("pane", "get"):
            return {"pane": self.pane(args[2] == "w:p2")}
        if op == ("pane", "process-info"):
            child = args[-1] == "w:p2"
            launched = bool(self.child_argv)
            proc = {"pid": 200 if child else 100, "name": self.agent, "argv": self.child_argv if child else self.argv}
            if child and not launched:
                proc = {"pid": 2, "name": "zsh", "argv": ["zsh"]}
            return {"process_info": {"shell_pid": 2, "foreground_processes": [proc]}}
        if op == ("pane", "split"):
            return {"pane": {"pane_id": "w:p2", "tab_id": "w:t1", "workspace_id": "w"}}
        if op in (("tab", "create"), ("workspace", "create")):
            return {"root_pane": {"pane_id": "w:p2"}, "tab": {"tab_id": "w:t2"}, "workspace": {"workspace_id": "new-w"}}
        if op == ("agent", "start"):
            if self.fail_start:
                raise module.ForkError("agent_not_ready")
            self.child_argv = [self.agent, *args[args.index("--") + 1:]]
            return {"type": "agent_started"}
        if op == ("pane", "run"):
            self.child_argv = [self.agent, "--tools", "read"]
            return {}
        if op in (("pane", "rename"), ("agent", "focus"), ("agent", "send-keys")):
            return {}
        raise AssertionError(args)


class ContractTests(unittest.TestCase):
    def launch(self, agent, **options):
        return fork_launch(agent, SOURCE, options.pop("argv", [agent]), options.pop("env", {}),
                           question=options.pop("question", True), bridge=Path("/plugin/runtime_bridge.ts"),
                           omp_overlay=Path("/plugin/question-omp.yml"), **options)

    def test_claude_tools_hooks_mcp_and_prompt_boundary(self):
        args = self.launch("claude").argv
        self.assertEqual(args[1:4], ("--resume", SOURCE, "--fork-session"))
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn('{"disableAllHooks":true}', args)
        self.assertIn(" ".join(QUESTION_POLICY.split()), args)
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_codex_read_only_never_approval_and_all_mcp_disabled(self):
        args = self.launch("codex", mcp_servers=[("normal", "stdio"), ("http-server", "streamable_http")]).argv
        self.assertEqual(args[1:3], ("fork", SOURCE))
        self.assertIn("read-only", args)
        self.assertIn("never", args)
        self.assertIn('mcp_servers.normal={command="false",enabled=false}', args)
        self.assertIn('mcp_servers.http-server={url="http://127.0.0.1:1/disabled",enabled=false}', args)

    def test_unsupported_mcp_name_or_transport_fails_closed(self):
        for servers in ([("odd.name", "stdio")], [("normal", "unknown")]):
            with self.subTest(servers=servers), self.assertRaises(ContractError):
                self.launch("codex", mcp_servers=servers)

    def test_gjc_preserves_live_profile_and_session_directory(self):
        launch = self.launch("gjc", argv=["gjc", "--model", "old", "--thinking", "low"],
                             runtime={"model": "new/model", "profile": "work", "thinking": "high", "session_file": "/scope/current.jsonl"})
        self.assertIn("work", launch.argv)
        self.assertNotIn("old", launch.argv)
        self.assertIn("high", launch.argv)
        self.assertIn("/scope", launch.argv)
        self.assertEqual(launch.argv[-1], "/goal pause")
        self.assertNotIn("--extension", launch.argv)

    def test_omp_child_disables_discovery_and_loads_only_guard(self):
        args = self.launch("omp").argv
        self.assertIn("--no-extensions", args)
        self.assertIn("/plugin/runtime_bridge.ts", args)
        self.assertIn("/plugin/question-omp.yml", args)
        self.assertIn("read,grep,glob", args)

    def test_secrets_and_parent_goal_and_prompts_never_copied(self):
        args = self.launch("omp", argv=["omp", "--api-key", "secret", "--goal", "old-task", "prompt", "--model=new"]).argv
        self.assertNotIn("secret", args)
        self.assertNotIn("old-task", args)
        self.assertNotIn("prompt", args)
        self.assertIn("new", args)

    def test_only_allowlisted_environment_is_relayed(self):
        launch = self.launch("gjc", env={"GJC_CODING_AGENT_DIR": "/agent", "GJC_MASTER_CAPABILITY": "secret", "TOKEN": "secret"})
        self.assertEqual(launch.environment["GJC_CODING_AGENT_DIR"], "/agent")
        self.assertNotIn("TOKEN", launch.environment)
        self.assertNotIn("GJC_MASTER_CAPABILITY", launch.environment)

    def test_fork_mode_has_no_question_restrictions(self):
        args = self.launch("codex", question=False).argv
        self.assertEqual(args, ("codex", "fork", SOURCE))

    def test_missing_flag_value_is_rejected(self):
        with self.assertRaises(ContractError):
            safe_launch_flags("gjc", ["gjc", "--model"])

    def test_positional_separator_stops_flag_relay(self):
        self.assertEqual(safe_launch_flags("omp", ["omp", "--", "--model", "fake"]), [])

    def test_inherit_thinking_is_not_passed_as_invalid_effort(self):
        args = self.launch("gjc", runtime={"thinking": "inherit"}).argv
        self.assertNotIn("--thinking", args)


class WorkflowTests(unittest.TestCase):
    def workflow(self, client, **options):
        return module.ForkWorkflow(client, {"focused_pane_id": "w:p1", "workspace_id": "w"},
                                   {"HERDR_SOCKET_PATH": "/socket", "HERDR_PLUGIN_STATE_DIR": "/state"}, **options)

    def test_question_split_is_right_and_never_inputs_parent(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            result = self.workflow(client).execute()
        self.assertEqual(result.fork_session_id, CHILD)
        split = next(call for call in client.calls if call[:2] == ("pane", "split"))
        self.assertIn("right", split)
        self.assertIn("--no-focus", split)
        self.assertFalse(any(call[:2] == ("agent", "send-keys") for call in client.calls))
        self.assertFalse(any(call[1:2] == ("close",) for call in client.calls))

    def test_each_destination_and_no_focus(self):
        for destination in ("tab", "workspace", "pane"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd)
                result = self.workflow(client, destination=destination, focus=False).execute()
                self.assertEqual(result.destination, destination)
                self.assertFalse(any(c[:2] == ("agent", "focus") for c in client.calls))

    def test_unsupported_agent_fails_before_split(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "unknown")
            with self.assertRaisesRegex(module.ForkError, "Supported agents"):
                self.workflow(client).execute()
        self.assertFalse(any(c[:2] == ("pane", "split") for c in client.calls))

    def test_missing_session_never_falls_back_to_new_conversation(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.missing_session = True
            with self.assertRaisesRegex(module.ForkError, "No trusted"):
                self.workflow(client).execute()
        self.assertFalse(any(c[:2] == ("agent", "start") for c in client.calls))

    def test_in_process_source_switch_is_rejected(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.changed_source = True
            with self.assertRaisesRegex(module.ForkError, "switched"):
                self.workflow(client).execute()
        self.assertFalse(any(c[:2] == ("pane", "split") for c in client.calls))

    def test_start_not_ready_is_failure_and_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.fail_start = True
            with self.assertRaisesRegex(module.ForkError, "Destination preserved: w:p2"):
                self.workflow(client).execute()
        self.assertFalse(any(c[1:2] == ("close",) for c in client.calls))

    def test_same_parent_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.same_id = True
            with self.assertRaisesRegex(module.ForkError, "parent session ID"):
                self.workflow(client).execute()

    def test_blocked_child_preserves_approval_and_never_answers_it(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.blocked = True
            with self.assertRaisesRegex(module.ForkError, "requires login"):
                self.workflow(client).execute()
        self.assertFalse(any(c[:2] == ("agent", "send-keys") for c in client.calls))

    def test_message_picker_runs_only_in_child(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module.time, "sleep"):
            client = FakeClient(cwd)
            self.workflow(client, mode="message").execute()
        calls = [c for c in client.calls if c[:2] == ("agent", "send-keys")]
        self.assertEqual(calls, [("agent", "send-keys", "w:p2", "esc"), ("agent", "send-keys", "w:p2", "esc")])

    def test_gjc_requires_fresh_owner_registration_before_creating_pane(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", side_effect=RegistryError("expired")):
            client = FakeClient(cwd, "gjc")
            with self.assertRaisesRegex(module.ForkError, "expired"):
                self.workflow(client).execute()
        self.assertFalse(any(c[:2] == ("pane", "split") for c in client.calls))

    def test_gjc_uses_shell_launch_not_unsupported_agent_start(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "gjc")
            records = [dict(session_id=SOURCE, bridge_version=1, environment={"XDG_CACHE_HOME": "/owner-cache"}), dict(session_id=SOURCE), dict(session_id=CHILD, question_ready=True)]
            with patch.object(module, "read_record", side_effect=records), patch.object(module.ForkWorkflow, "_start_shell", autospec=True) as start:
                def launch(workflow, pane, argv):
                    client.child_argv = list(argv)
                start.side_effect = launch
                workflow = self.workflow(client)
                workflow.env = {**workflow.env, "GJC_CODING_AGENT_DIR": "/wrong-caller-root", "XDG_CACHE_HOME": "/wrong-cache"}
                result = workflow.execute()
            self.assertEqual(result.fork_session_id, CHILD)
            self.assertIn("-u", client.child_argv)
            self.assertIn("GJC_CODING_AGENT_DIR", client.child_argv)
            self.assertIn("XDG_CACHE_HOME=/owner-cache", client.child_argv)
            self.assertFalse(any("wrong" in argument for argument in client.child_argv))
            self.assertFalse(any(c[:2] == ("agent", "start") for c in client.calls))


class RegistryTests(unittest.TestCase):
    def test_runtime_and_plugin_state_use_herdr_state_root_not_config_root(self):
        with patch("runtime_registry.Path.home", return_value=Path("/home/test")):
            self.assertEqual(state_root({}), Path("/home/test/.local/state/herdr/plugins/seungwonme.question-fork"))
            expected = Path("/state/herdr/plugins/seungwonme.question-fork")
            self.assertEqual(state_root({"XDG_STATE_HOME": "/state", "XDG_CONFIG_HOME": "/wrong"}), expected)
            self.assertEqual(state_root({"HERDR_PLUGIN_STATE_DIR": str(expected)}), expected)

    def test_exact_binding_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as cwd:
            root = Path(cwd)
            pane = {"pane_id": "w:p1", "terminal_id": "t", "agent": "gjc", "cwd": cwd}
            data = {**pane, "pid": os.getpid(), "process_start": "start", "session_id": SOURCE}
            path = write_record(root, "/socket", data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("runtime_registry.process_start", return_value="start"):
                result = read_record(root, "/socket", pane, os.getpid())
                self.assertEqual(result["session_id"], SOURCE)
                with self.assertRaises(RegistryError):
                    read_record(root, "/socket", {**pane, "terminal_id": "other"}, os.getpid())
                with patch("runtime_registry.time.time", return_value=result["updated_at"] + 100):
                    with self.assertRaisesRegex(RegistryError, "expired"):
                        read_record(root, "/socket", pane, os.getpid())

    def test_symlink_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as cwd:
            root = Path(cwd)
            pane = {"pane_id": "w:p1", "terminal_id": "t", "agent": "gjc", "cwd": cwd}
            data = {**pane, "pid": os.getpid(), "process_start": "start", "session_id": SOURCE}
            path = write_record(root, "/socket", data)
            target = root / "foreign"
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaises(RegistryError):
                read_record(root, "/socket", pane, os.getpid())

    def test_canonical_uuid_rejects_shell_input(self):
        with self.assertRaises(module.ForkError):
            module.native_id("$(touch /tmp/not-allowed)")


if __name__ == "__main__":
    unittest.main()
