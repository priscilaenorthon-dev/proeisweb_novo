import os
import threading
import time
import unittest
from unittest.mock import patch

from proeis_http import CaptchaSubmission, ProeisHTTP


class FakeParallelCaptchaClient(ProeisHTTP):
    def __init__(self, answers):
        self.answers = answers
        self.last_captcha_id = None
        self.captcha_elapsed_seconds = 0.0
        self.captcha_submissions = 0

    def _solve_single_captcha_submission_result(
        self,
        image: bytes,
        solver_index: int,
        stop_event: threading.Event | None = None,
    ) -> CaptchaSubmission:
        delay, answer, captcha_id = self.answers[solver_index - 1]
        time.sleep(delay)
        self.captcha_submissions += 1
        return CaptchaSubmission(text=answer, captcha_id=captcha_id, solver_index=solver_index)

    def _solve_single_captcha_submission(self, image: bytes, solver_index: int) -> str:
        result = self._solve_single_captcha_submission_result(image, solver_index)
        self.last_captcha_id = result.captcha_id
        return result.text


class ParallelCaptchaTests(unittest.TestCase):
    def test_parallel_captcha_uses_first_valid_answer(self):
        client = FakeParallelCaptchaClient([
            (0.20, "BAD", "slow-invalid"),
            (0.01, "ABC123", "fast-valid"),
        ])

        with patch.dict(os.environ, {"TWOCAPTCHA_PARALLEL_SOLVES": "2"}):
            answer = client.solve_captcha_once(b"image")

        self.assertEqual(answer, "ABC123")
        self.assertEqual(client.last_captcha_id, "fast-valid")

    def test_parallel_captcha_falls_back_after_invalid_answer(self):
        client = FakeParallelCaptchaClient([
            (0.01, "12", "invalid"),
            (0.02, "A1B2C3", "valid"),
        ])

        with patch.dict(os.environ, {"TWOCAPTCHA_PARALLEL_SOLVES": "2"}):
            answer = client.solve_captcha_once(b"image")

        self.assertEqual(answer, "A1B2C3")
        self.assertEqual(client.last_captcha_id, "valid")

    def test_parallel_captcha_preserves_single_submission_mode(self):
        client = FakeParallelCaptchaClient([
            (0.01, "ABC123", "single"),
            (0.01, "ZZZ999", "unused"),
        ])

        with patch.dict(os.environ, {"TWOCAPTCHA_PARALLEL_SOLVES": "1"}):
            answer = client.solve_captcha_once(b"image")

        self.assertEqual(answer, "ABC123")
        self.assertEqual(client.captcha_submissions, 1)

    def test_parallel_captcha_uses_winner_id_without_shared_state_race(self):
        class RaceClient(FakeParallelCaptchaClient):
            def _solve_single_captcha_submission_result(
                self,
                image: bytes,
                solver_index: int,
                stop_event: threading.Event | None = None,
            ) -> CaptchaSubmission:
                if solver_index == 1:
                    time.sleep(0.05)
                    self.last_captcha_id = "late-shared-id"
                    return CaptchaSubmission(text="ZZZ999", captcha_id="late-id", solver_index=solver_index)
                time.sleep(0.01)
                self.last_captcha_id = "wrong-shared-id"
                return CaptchaSubmission(text="ABC123", captcha_id="winner-id", solver_index=solver_index)

        client = RaceClient([
            (0.05, "ZZZ999", "late-id"),
            (0.01, "ABC123", "winner-id"),
        ])

        with patch.dict(os.environ, {"TWOCAPTCHA_PARALLEL_SOLVES": "2"}):
            answer = client.solve_captcha_once(b"image")

        self.assertEqual(answer, "ABC123")
        self.assertEqual(client.last_captcha_id, "winner-id")


if __name__ == "__main__":
    unittest.main()
