# SPDX-License-Identifier: Apache-2.0
"""Offline tests for check_controls.get: what is retried and what is not."""

from __future__ import annotations

import io
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_controls  # noqa: E402


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://anchor.test/x", code, "", {}, io.BytesIO(b""))


class GetTest(unittest.TestCase):
    def run_get(self, *outcomes):
        """Runs get() against urlopen outcomes (a response or an exception)."""
        sleeps = []
        with mock.patch.object(
            check_controls.urllib.request, "urlopen", side_effect=list(outcomes)
        ) as urlopen:
            result = check_controls.get("https://anchor.test/x", sleep=sleeps.append)
        return result, urlopen.call_count, sleeps

    def test_answer_on_first_attempt(self):
        result, calls, sleeps = self.run_get(Response(b'{"leaf_index": 7}'))
        self.assertEqual(result, (200, {"leaf_index": 7}))
        self.assertEqual((calls, sleeps), (1, []))

    def test_http_status_is_never_retried(self):
        for code in (400, 404, 500, 503):
            with self.subTest(code=code):
                result, calls, sleeps = self.run_get(http_error(code))
                self.assertEqual(result, (code, {}))
                self.assertEqual((calls, sleeps), (1, []))

    def test_timeout_then_answer(self):
        result, calls, sleeps = self.run_get(TimeoutError("The read operation timed out"), Response(b"{}"))
        self.assertEqual(result, (200, {}))
        self.assertEqual((calls, sleeps), (2, [5]))

    def test_connection_failures_are_retried(self):
        outcomes = (
            urllib.error.URLError(socket.gaierror("no such host")),
            ConnectionResetError("reset by peer"),
            Response(b"{}"),
        )
        result, calls, sleeps = self.run_get(*outcomes)
        self.assertEqual(result, (200, {}))
        self.assertEqual((calls, sleeps), (3, [5, 15]))

    def test_unreachable_after_every_attempt(self):
        outcomes = [TimeoutError("timed out")] * check_controls.ATTEMPTS
        with self.assertRaises(check_controls.Unreachable):
            self.run_get(*outcomes)

    def test_unreachable_exits_2(self):
        def main():
            raise check_controls.Unreachable("https://anchor.test/x")

        with mock.patch.object(check_controls, "main", main), self.assertRaises(SystemExit) as exit_:
            check_controls.run()
        self.assertEqual(exit_.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
