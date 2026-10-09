# SPDX-License-Identifier: Apache-2.0
"""Regression checks for incoming language coverage and release isolation."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConsolidationTests(unittest.TestCase):
    def test_all_language_pitch_roots_and_source_exclusion(self):
        guard = load("messaging", ".github/messaging_guard.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for language in ("python", "rust", "go", "ts"):
                (root / language / "docs").mkdir(parents=True)
                (root / language / "README.md").write_text("Just logging\n")
                (root / language / "docs" / "guide.md").write_text("Your log, now provable\n")
                (root / language / "source.py").write_text('"Just logging"\n')
                (root / language / "CHANGELOG.md").write_text("Just logging\n")
            self.assertEqual(len(guard.scan(root)), 8)

    def test_typescript_source_scanned_with_destination_exemptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".github").mkdir()
            phrase = "https://verify." + "actionstate.example"
            # The incoming tree cannot grant itself an exemption.
            (root / ".github" / "hostname_lint_allowlist.txt").write_text(phrase)
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".rs"):
                (root / ("source" + suffix)).write_text(phrase)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            result = subprocess.run(
                ["python3", str(ROOT / ".github/hostname_lint.py"), str(root),
                 "--allowlist-root", str(ROOT)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".rs"):
                self.assertIn("source" + suffix, result.stdout)
            env = {**os.environ, "NEUTRALITY_TERMS": json.dumps({"word": ["syntheticmarker"]}),
                   "NEUTRALITY_REVEAL": "false"}
            for path in root.glob("source.*"):
                path.write_text("syntheticmarker\n")
            result = subprocess.run(
                ["python3", str(ROOT / ".github/neutrality_scan.py"), str(root)],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("syntheticmarker", result.stdout + result.stderr)
            env.pop("NEUTRALITY_TERMS")
            result = subprocess.run(
                ["python3", str(ROOT / ".github/neutrality_scan.py"), str(root)],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)

    def test_python_tag_matches_metadata_and_rejects_other_languages(self):
        guard = load("release", ".github/scripts/check_python_release.py")
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "pyproject.toml"
            metadata.write_text('[project]\nversion = "0.9.0"\n')
            guard.check("python/v0.9.0", metadata)
            for tag in ("v0.9.0", "go/v0.9.0", "ts/v0.9.0", "crates/capsule-emit-v0.9.0",
                        "python/v0.8.0", "python/v0.9.0;echo unsafe", "python/v0.9.0-rc1"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    guard.check(tag, metadata)
