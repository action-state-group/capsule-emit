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

    def test_release_tags_match_metadata_and_reject_other_languages(self):
        guard = load("release", ".github/scripts/check_release.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {
                "python": root / "pyproject.toml",
                "ts": root / "package.json",
                "go": root / "go.mod",
            }
            metadata["python"].write_text('[project]\nversion = "0.9.0"\n')
            metadata["ts"].write_text('{"name": "@action-state-group/capsule-emit", "version": "0.4.0"}')
            metadata["go"].write_text(f"module {guard.GO_MODULE}\n\ngo 1.27.0\n")
            accepted = {
                "python": ("python/v0.9.0",),
                "ts": ("ts/v0.4.0",),
                "go": ("go/v0.3.0", "go/v1.0.0", "go/v0.3.0-rc.1"),
            }
            for language, tags in accepted.items():
                for tag in tags:
                    with self.subTest(language=language, tag=tag):
                        guard.check(language, tag, metadata[language])
            refused = {
                "python": ("v0.9.0", "go/v0.9.0", "ts/v0.9.0", "crates/capsule-emit-v0.9.0",
                           "python/v0.8.0", "python/v0.9.0;echo unsafe", "python/v0.9.0-rc1"),
                "ts": ("v0.4.0", "python/v0.4.0", "go/v0.4.0", "ts/v0.3.0", "ts/v0.4",
                       "ts/v0.4.0;echo unsafe"),
                "go": ("v0.3.0", "python/v0.3.0", "ts/v0.3.0", "crates/capsule-emit-v0.3.0",
                       "go/v2.0.0", "go/v0.3", "go/v01.0.0", "go/v0.3.0;echo unsafe"),
            }
            for language, tags in refused.items():
                for tag in tags:
                    with self.subTest(language=language, tag=tag), self.assertRaises(ValueError):
                        guard.check(language, tag, metadata[language])
            metadata["go"].write_text("module github.com/action-state-group/capsule-emit-go\n")
            with self.assertRaises(ValueError):
                guard.check("go", "go/v0.3.0", metadata["go"])
            with self.assertRaises(ValueError):
                guard.check("rust", "rust/v0.1.0", metadata["go"])

    def test_publishers_route_only_their_own_tag_namespace(self):
        workflows = ROOT / ".github/workflows"
        routes = {
            "publish-python.yml": "startsWith(github.event.release.tag_name, 'python/v')",
            "publish-ts.yml": "startsWith(github.event.release.tag_name, 'ts/v')",
            "publish-go.yml": 'tags: ["go/v*"]',
            "publish-rust.yml": 'tags: ["crates/*-v*"]',
        }
        for name, route in routes.items():
            with self.subTest(workflow=name):
                self.assertIn(route, (workflows / name).read_text())
        for name in ("publish-python.yml", "publish-ts.yml", "publish-go.yml"):
            language = name.removeprefix("publish-").removesuffix(".yml")
            with self.subTest(workflow=name):
                self.assertIn(f"check_release.py {language} ", (workflows / name).read_text())
