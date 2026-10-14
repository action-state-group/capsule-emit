# SPDX-License-Identifier: Apache-2.0
"""Regression checks for incoming language coverage and release isolation."""
import importlib.util
import json
import os
import re
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
    def test_python_readme_matches_root_with_relative_links(self):
        root = (ROOT / "README.md").read_text()
        root = root[:root.index("Language projects:")] + root[root.index("[![CI]"):]
        root = re.sub(
            r"\]\((docs/|rust/|TRANSLATION.md|ADOPT.md|LICENSE|NOTICE|examples/|test-vectors/|witnesses.json)",
            r"](../\1", root,
        )
        root = root.replace("](python/capsule_emit/", "](capsule_emit/")
        self.assertEqual(root, (ROOT / "python/README.md").read_text())

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
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs"):
                (root / ("source" + suffix)).write_text(phrase)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            result = subprocess.run(
                ["python3", str(ROOT / ".github/hostname_lint.py"), str(root),
                 "--allowlist-root", str(ROOT)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs"):
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

    def test_publishers_route_only_their_own_tag_namespace(self):
        workflows = ROOT / ".github/workflows"
        routes = {
            "publish-python.yml": "startsWith(github.event.release.tag_name, 'python/v')",
            "publish-ts.yml": "startsWith(github.event.release.tag_name, 'ts/v')",
            "publish-rust.yml": 'tags: ["crates/*-v*"]',
        }
        for name, route in routes.items():
            with self.subTest(workflow=name):
                self.assertIn(route, (workflows / name).read_text())
        # The tag guard itself is tested in .github/test_release.py.
        for name in ("publish-python.yml", "publish-ts.yml"):
            language = name.removeprefix("publish-").removesuffix(".yml")
            with self.subTest(workflow=name):
                self.assertIn(f"check {language} ", (workflows / name).read_text())
                self.assertIn("release.py", (workflows / name).read_text())
        publish_ts = (workflows / "publish-ts.yml").read_text()
        self.assertIn("ARTIFACT_MYSQL_TEST: '1'", publish_ts)
        self.assertIn('npm publish --tag "$dist_tag"', publish_ts)
        self.assertNotIn("run: npm publish\n", publish_ts)
