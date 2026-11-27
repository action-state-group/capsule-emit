# SPDX-License-Identifier: Apache-2.0
"""examples.yml's matrix runs every entry examples.sh has."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ExamplesMatrixTests(unittest.TestCase):
    def test_the_workflow_matrix_is_the_scripts_entry_list(self):
        listed = subprocess.run(
            ["bash", str(ROOT / ".github/scripts/examples.sh"), "--list"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        workflow = (ROOT / ".github/workflows/examples.yml").read_text()
        matrix = re.search(r"^\s+entry: \[([^\]]*)\]", workflow, re.M)
        self.assertIsNotNone(matrix, "examples.yml has no entry matrix")
        entries = [e.strip() for e in matrix.group(1).split(",")]
        self.assertEqual(sorted(entries), sorted(listed))
        self.assertEqual(len(entries), len(set(entries)))

    def test_an_unknown_entry_is_refused(self):
        run = subprocess.run(
            ["bash", str(ROOT / ".github/scripts/examples.sh"), "no-such-entry", "python3"],
            capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("no entry no-such-entry", run.stderr)


if __name__ == "__main__":
    unittest.main()
