from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_contracts import (
    AGENTS,
    ContractError,
    ENVIRONMENT_KEYS,
    fork_launch,
    safe_launch_flags,
)

REFERENCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
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

    def test_supported_agents_are_limited_to_claude_codex_and_omp(self):
        self.assertEqual(set(AGENTS), {"claude", "codex", "omp"})

    def test_normal_native_launches_do_not_add_read_only_restrictions(self):
        expected = {
            "claude": ("claude", "--resume", REFERENCE, "--fork-session"),
            "codex": ("codex", "fork", REFERENCE),
            "omp": ("omp", "--fork", REFERENCE),
        }
        for agent, argv in expected.items():
            with self.subTest(agent=agent):
                launch = self.launch(agent, environment={"HERDR_PANEL_FORK_READ_ONLY": "1"})
                self.assertEqual(launch.argv, argv)
                self.assertEqual(launch.environment["HERDR_PANEL_FORK_READ_ONLY"], "0")
                self.assertEqual(launch.environment["HERDR_PANEL_FORK_AGENT"], agent)
                self.assertEqual(launch.restrictions, "native permissions; shared checkout")

    def test_excluded_prompt_values_are_not_reparsed_as_model_flags(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, [agent, "--system-prompt", "--model", "secret-prompt"]), [])
                self.assertEqual(safe_launch_flags(agent, [agent, "--api-key", "--model", "secret", "--model", "real"]), ["--model", "real"])

    def test_existing_omp_read_only_controls_remain(self):
        launch = self.launch("omp", read_only=True)
        for argument in ("read,grep,glob", "--no-lsp", "--no-pty", "--no-extensions",
                         "/plugin/runtime_bridge.ts", "/plugin/read-only-omp.yml"):
            self.assertIn(argument, launch.argv)
        self.assertEqual(launch.environment["HERDR_PANEL_FORK_READ_ONLY"], "1")

    def test_only_documented_value_flags_are_relayed(self):
        flags = {
            "claude": ["--model", "chosen", "--effort", "high"],
            "codex": ["--model", "chosen", "--profile", "work"],
            "omp": ["--model", "chosen", "--thinking", "high", "--profile", "work", "--session-dir", "/sessions"],
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

    def test_flags_reject_missing_empty_control_and_flag_values(self):
        for agent in AGENTS:
            for args in (["--model"], ["--model="], ["--model", "--allow-all-tools"], ["--model", "bad\nmodel"]):
                with self.subTest(agent=agent, args=args), self.assertRaises(ContractError):
                    safe_launch_flags(agent, [agent, *args])
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, [agent, "--", "--model", "prompt"]), [])

    def test_source_configuration_roots_and_xdg_paths_are_preserved_without_credentials(self):
        roots = {
            "claude": ("CLAUDE_CONFIG_DIR",),
            "codex": ("CODEX_HOME",),
            "omp": ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"),
        }
        for agent, keys in roots.items():
            with self.subTest(agent=agent):
                environment = {key: f"/source/{key}" for key in (*keys, *XDG_KEYS)}
                environment.update({"API_KEY": "secret", "SERVICE_TOKEN": "secret"})
                environment["HERDR_PANEL_FORK_STATE_DIR"] = "/source/fork-state"
                environment["HERDR_PANEL_FORK_HELPER"] = "/plugin/fork_from_message.py"
                launch = self.launch(agent, environment=environment)
                for key in (*keys, *XDG_KEYS, "HERDR_PANEL_FORK_STATE_DIR", "HERDR_PANEL_FORK_HELPER"):
                    self.assertEqual(launch.environment[key], environment[key])
                self.assertNotIn("API_KEY", launch.environment)
                self.assertNotIn("SERVICE_TOKEN", launch.environment)
                self.assertEqual(set(ENVIRONMENT_KEYS[agent]) - set(XDG_KEYS), set(keys))

    def test_omp_live_model_thinking_and_session_directory_override_startup(self):
        launch = self.launch(
            "omp",
            argv=["omp", "--model", "old/model", "--thinking", "low", "--session-dir", "/stale"],
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

    def test_live_model_uses_provider_qualified_selector(self):
        launch = self.launch("omp", runtime={"model": "provider/model", "profile": "work"})
        self.assertIn("--model", launch.argv)
        self.assertNotIn("work", launch.argv)

    def test_inherit_thinking_does_not_add_a_native_flag(self):
        launch = self.launch("omp", runtime={"thinking": "inherit"})
        self.assertNotIn("--thinking", launch.argv)

    def test_unknown_agent_is_a_contract_error(self):
        for agent in ("unknown", "pi", "opencode"):
            with self.subTest(agent=agent):
                self.assertNotIn(agent, AGENTS)
                with self.assertRaisesRegex(ContractError, "Unsupported agent"):
                    self.launch(agent)
                with self.assertRaisesRegex(ContractError, "Unsupported agent"):
                    safe_launch_flags(agent, [agent])


if __name__ == "__main__":
    unittest.main()
