# SPDX-License-Identifier: Apache-2.0
"""python.yml's extras matrix runs every entry python_extras_tests.sh has."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ExtrasMatrixTests(unittest.TestCase):
    def test_the_workflow_matrix_is_the_scripts_entry_list(self):
        listed = subprocess.run(
            ["bash", str(ROOT / ".github/scripts/python_extras_tests.sh"), "--list"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        workflow = (ROOT / ".github/workflows/python.yml").read_text()
        job = workflow[workflow.index("\n  extras:\n"):]
        matrix = re.search(r"^\s+entry: \[([^\]]*)\]", job, re.M)
        self.assertIsNotNone(matrix, "python.yml's extras job has no entry matrix")
        entries = [e.strip() for e in matrix.group(1).split(",")]
        self.assertEqual(sorted(entries), sorted(listed))
        self.assertEqual(len(entries), len(set(entries)))


if __name__ == "__main__":
    unittest.main()
