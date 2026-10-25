from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_contracts import (
    AGENTS,
    ContractError,
    ENVIRONMENT_KEYS,
    PI_FAMILY,
    UNAVAILABLE,
    fork_launch,
    safe_launch_flags,
)

REFERENCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
NEW_AGENTS = ("pi", "omo", "opencode", "copilot")
XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


class HarnessContractTests(unittest.TestCase):
    def launch(self, agent, *, argv=None, environment=None, read_only=False, runtime=None):
        return fork_launch(
            agent,
            REFERENCE,
            [agent] if argv is None else argv,
            {} if environment is None else environment,
            read_only=read_only,
            bridge=Path("/plugin/runtime_bridge.ts"),
            omp_overlay=Path("/plugin/read-only-omp.yml"),
            runtime=runtime,
        )

    def test_new_native_launches_do_not_add_read_only_restrictions(self):
        expected = {
            "pi": ("pi", "--fork", REFERENCE),
            "omo": ("omo", "--fork", REFERENCE),
            "opencode": ("opencode", "mini", "--session", REFERENCE, "--fork"),
            "copilot": ("copilot", "--resume", REFERENCE),
        }
        for agent, argv in expected.items():
            with self.subTest(agent=agent):
                launch = self.launch(agent, environment={"HERDR_PANEL_FORK_READ_ONLY": "1"})
                self.assertEqual(launch.argv, argv)
                self.assertEqual(launch.environment["HERDR_PANEL_FORK_READ_ONLY"], "0")
                self.assertEqual(launch.environment["HERDR_PANEL_FORK_AGENT"], agent)
                self.assertEqual(launch.restrictions, "native permissions; shared checkout")

    def test_new_read_only_launches_fail_closed_before_parsing_flags(self):
        for agent in NEW_AGENTS:
            with self.subTest(agent=agent), self.assertRaisesRegex(
                ContractError, "Read-only mode.*native guard support has not been verified"
            ):
                self.launch(agent, argv=[agent, "--model"], read_only=True)

    def test_unsafe_native_paths_fail_before_argument_construction(self):
        for agent in UNAVAILABLE:
            for read_only in (False, True):
                with self.subTest(agent=agent, read_only=read_only), self.assertRaisesRegex(ContractError, "Source was not resumed or changed"):
                    self.launch(agent, argv=[agent, "--model"], read_only=read_only)

    def test_excluded_prompt_values_are_not_reparsed_as_model_flags(self):
        for agent in NEW_AGENTS:
            self.assertEqual(safe_launch_flags(agent, [agent, "--system-prompt", "--model", "secret-prompt"]), [])
            self.assertEqual(safe_launch_flags(agent, [agent, "--api-key", "--model", "secret", "--model", "real"]), ["--model", "real"])

    def test_opencode_preserves_private_server_instead_of_using_default_service(self):
        launch = self.launch("opencode", argv=["opencode", "mini", "--standalone", "--session", "ses_source"])
        self.assertIn("--standalone", launch.argv)
        self.assertIn("--fork", launch.argv)

    def test_existing_gjc_and_omp_read_only_controls_remain(self):
        for agent, tools in (("gjc", "read,search,find,ast_grep"), ("omp", "read,grep,glob")):
            with self.subTest(agent=agent):
                launch = self.launch(agent, read_only=True)
                self.assertIn(tools, launch.argv)
                self.assertIn("--no-lsp", launch.argv)
                self.assertIn("--no-pty", launch.argv)
                self.assertEqual(launch.environment["HERDR_PANEL_FORK_READ_ONLY"], "1")
                if agent == "gjc":
                    self.assertIn("--no-mcp", launch.argv)
                    self.assertEqual(launch.argv[-1], "/goal pause")
                    self.assertNotIn("--extension", launch.argv)
                else:
                    self.assertIn("--no-extensions", launch.argv)
                    self.assertIn("/plugin/runtime_bridge.ts", launch.argv)
                    self.assertIn("/plugin/read-only-omp.yml", launch.argv)

    def test_only_documented_value_flags_are_relayed(self):
        flags = {
            "pi": ["--model", "chosen", "--thinking", "high", "--session-dir", "/sessions", "--provider", "custom"],
            "omo": ["--model", "chosen", "--thinking", "high", "--session-dir", "/sessions", "--provider", "custom"],
            "opencode": ["--server", "http://127.0.0.1:4096", "--model", "chosen", "--agent", "build"],
            "copilot": ["--model", "chosen"],
        }
        for agent, expected in flags.items():
            with self.subTest(agent=agent):
                source = [agent, "--api-key", "secret", "--allow-all-tools", "--extension", "/untrusted.ts", *expected, "old prompt"]
                self.assertEqual(safe_launch_flags(agent, source), expected)
                inline = [f"{expected[index]}={expected[index + 1]}" for index in range(0, len(expected), 2)]
                self.assertEqual(safe_launch_flags(agent, [agent, *inline]), expected)
                launch = self.launch(agent, argv=source)
                self.assertEqual(launch.argv[-len(expected):], tuple(expected))
                self.assertNotIn("--extension", launch.argv)
                self.assertNotIn("--allow-all-tools", launch.argv)
                self.assertNotIn("secret", launch.argv)

    def test_new_flags_reject_missing_empty_control_and_flag_values(self):
        for agent in NEW_AGENTS:
            for args in (["--model"], ["--model="], ["--model", "--allow-all-tools"], ["--model", "bad\nmodel"]):
                with self.subTest(agent=agent, args=args), self.assertRaises(ContractError):
                    safe_launch_flags(agent, [agent, *args])
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, [agent, "--", "--model", "prompt"]), [])

    def test_source_configuration_roots_and_xdg_paths_are_preserved_without_credentials(self):
        roots = {
            "pi": ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"),
            "omo": ("OMO_CODING_AGENT_DIR", "SENPI_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "OMO_CODING_AGENT_SESSION_DIR"),
            "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"),
            "copilot": ("COPILOT_HOME",),
        }
        for agent, keys in roots.items():
            with self.subTest(agent=agent):
                environment = {key: f"/source/{key}" for key in (*keys, *XDG_KEYS)}
                if agent == "opencode":
                    environment["OPENCODE_CONFIG_CONTENT"] = '{"model":"source/model"}'
                environment.update({"API_KEY": "secret", "GJC_MASTER_CAPABILITY": "secret", "CURSOR_TOKEN": "secret"})
                environment["HERDR_PANEL_FORK_STATE_DIR"] = "/source/fork-state"
                environment["HERDR_PANEL_FORK_HELPER"] = "/plugin/fork_from_message.py"
                launch = self.launch(agent, environment=environment)
                for key in (*keys, *XDG_KEYS, "HERDR_PANEL_FORK_STATE_DIR", "HERDR_PANEL_FORK_HELPER"):
                    self.assertEqual(launch.environment[key], environment[key])
                self.assertNotIn("API_KEY", launch.environment)
                self.assertNotIn("GJC_MASTER_CAPABILITY", launch.environment)
                self.assertNotIn("CURSOR_TOKEN", launch.environment)
                self.assertEqual(set(ENVIRONMENT_KEYS[agent]) - set(XDG_KEYS), set(keys))

    def test_pi_family_live_model_thinking_and_session_directory_override_startup(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(
                    agent,
                    argv=[agent, "--model", "old/model", "--thinking", "low", "--session-dir", "/stale"],
                    runtime={"model": "new/live-model", "thinking": "high", "session_file": "/active sessions/current.jsonl"},
                )
                self.assertEqual(launch.argv.count("--model"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "new/live-model")
                self.assertEqual(launch.argv.count("--thinking"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--thinking") + 1], "high")
                self.assertEqual(launch.argv.count("--session-dir"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--session-dir") + 1], "/active sessions")
                self.assertNotIn("old/model", launch.argv)
                self.assertNotIn("/stale", launch.argv)

    def test_live_provider_qualified_model_replaces_stale_provider_selector(self):
        for agent in ("pi", "omo"):
            with self.subTest(agent=agent):
                launch = self.launch(
                    agent,
                    argv=[agent, "--provider", "old-provider", "--model", "old-model"],
                    runtime={"model": "current-provider/live-model"},
                )
                self.assertNotIn("--provider", launch.argv)
                self.assertNotIn("old-provider", launch.argv)
                self.assertIn("current-provider/live-model", launch.argv)

    def test_only_gjc_uses_live_model_profile(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(agent, runtime={"model": "provider/model", "profile": "work"})
                self.assertIn("--mpreset" if agent == "gjc" else "--model", launch.argv)
                self.assertEqual("work" in launch.argv, agent == "gjc")

    def test_inherit_thinking_does_not_add_a_native_flag(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(agent, runtime={"thinking": "inherit"})
                self.assertNotIn("--thinking", launch.argv)

    def test_unknown_agent_is_a_contract_error(self):
        self.assertNotIn("unknown", AGENTS)
        with self.assertRaisesRegex(ContractError, "Unsupported agent"):
            self.launch("unknown")
        with self.assertRaisesRegex(ContractError, "Unsupported agent"):
            safe_launch_flags("unknown", ["unknown"])


if __name__ == "__main__":
    unittest.main()
