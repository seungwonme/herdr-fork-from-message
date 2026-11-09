from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_contracts import ContractError
from harness_identity import entrypoint_argv, process_product, select_process, session_id, session_reference

SID = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"


class IdentityTests(unittest.TestCase):
    def test_only_entrypoints_identify_product(self):
        cases = [
            (["omp"], "omp"),
            (["claude", "--model", "test"], "claude"),
            (["bun", "/opt/bin/codex"], "codex"),
            (["pi"], None),
            (["opencode", "mini"], None),
            (["zsh", "omp"], None),
            (["bun", "/tmp/unknown.js", "omp"], None),
            (["bun", "--eval", "omp"], None),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(process_product({"argv": argv}), expected)

    def test_interpreter_is_removed_before_flag_parsing(self):
        self.assertEqual(entrypoint_argv({"argv": ["bun", "/opt/cli.js", "--model", "chosen"]}), ["/opt/cli.js", "--model", "chosen"])
        self.assertEqual(entrypoint_argv({"argv": ["omp", "--model", "chosen"]}), ["omp", "--model", "chosen"])

    def test_reported_hint_must_match_the_only_harness(self):
        product, process = select_process([{"pid": 3, "argv": ["omp"]}, {"argv": ["python3", "/plugin/fork_from_message.py"]}], "omp")
        self.assertEqual(product, "omp")
        self.assertEqual(process["pid"], 3)
        with self.assertRaises(ContractError):
            select_process([process], "claude")
        with self.assertRaises(ContractError):
            select_process([process, process], "omp")

    def test_exact_native_process_title_survives_node_argv_truncation(self):
        for extra in ({}, {"argv": None}):
            product, process = select_process([{"pid": 12, "name": "node", "argv0": "omp", **extra}], "omp")
            self.assertEqual(product, "omp")
            self.assertEqual(process["argv"], ["omp"])
        for invalid in ([], "omp", [None]):
            with self.assertRaises(ContractError):
                select_process([{"pid": 12, "name": "node", "argv0": "omp", "argv": invalid}], "omp")
        with self.assertRaises(ContractError):
            select_process([{"pid": 12, "name": "node", "argv0": "arbitrary omp prompt"}], "omp")

    def test_reference_namespaces_and_sources(self):
        for product in ("claude", "codex", "omp"):
            pane = {"agent_session": {"agent": product, "source": f"herdr:{product}", "kind": "id", "value": SID}}
            self.assertEqual(session_reference(pane, product), SID)
            pane["agent_session"]["source"] = "untrusted"
            with self.assertRaises(ContractError):
                session_reference(pane, product)
        for value in ("not-uuid", "019fe92d23967f408dfebdb1ae3d4e27", "--resume", "ses_test-123"):
            with self.assertRaises(ContractError):
                session_reference({"agent_session": {"agent": "omp", "source": "herdr:omp", "kind": "id", "value": value}}, "omp")

    def test_only_omp_may_report_a_session_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("{}", encoding="utf-8")
            pane = {"agent_session": {"agent": "claude", "source": "herdr:claude", "kind": "path", "value": str(path)}}
            with self.assertRaises(ContractError):
                session_reference(pane, "claude")

    def test_only_bounded_header_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            pane = {"agent_session": {"agent": "omp", "source": "herdr:omp", "kind": "path", "value": str(path)}}
            with self.assertRaises(ContractError):
                session_reference(pane, "omp")
            path.write_text(json.dumps({"type": "session", "id": SID}) + "\nnot json conversation body", encoding="utf-8")
            self.assertEqual(session_id(session_reference(pane, "omp")), SID)
            for header in ("x" * 32769, "{}", json.dumps({"type": "session", "id": SID.replace("-", "")})):
                path.write_text(header, encoding="utf-8")
                with self.assertRaises(ContractError):
                    session_id(str(path))


if __name__ == "__main__":
    unittest.main()
