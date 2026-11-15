from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from anki_batch_auto_easy.legacy import LegacyController


class _Db:
    def __init__(self, recent=()):
        self.recent = recent

    def all(self, _query, *_args):
        return [(value,) for value in self.recent]


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config = {
            "enabled": True,
            "auto_easy_enabled": True,
            "auto_hard_enabled": True,
            "auto_easy_timer_start": "answer",
            "auto_hard_threshold_mode": "custom",
            "auto_hard_threshold_ms": 1000,
            "auto_grade_priority": "hard",
            "threshold_ms": 1000,
            "history_window": 2,
        }
        self.mw = SimpleNamespace(
            pm=SimpleNamespace(
                profileFolder=lambda: self.temporary_directory.name
            ),
            addonManager=SimpleNamespace(
                getConfig=lambda _package: self.config
            ),
            col=SimpleNamespace(db=_Db()),
        )
        self.controller = LegacyController(self.mw)
        self.controller.config = lambda: self.config
        self.card = SimpleNamespace(id=123, did=7)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_fast_good_becomes_easy_when_recent_history_allows(self):
        self.controller.space_answer = True
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
        )
        self.assertEqual(result, 4)

    def test_recent_again_or_hard_blocks_easy(self):
        self.mw.col.db.recent = (2,)
        self.controller.space_answer = True
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
        )
        self.assertEqual(result, 3)

    def test_classic_scheduler_does_not_apply_batch_or_quota_rules(self):
        self.controller.space_answer = True
        self.config["strategy"] = "smart"
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
        )
        self.assertEqual(result, 3)

        self.config["strategy"] = "fixed"
        self.config["quota_enabled"] = True
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
        )
        self.assertEqual(result, 3)

    def test_new_learning_card_stops_auto_easy_after_two_good_answers(self):
        self.card.type = 0
        self.card.lapses = 0
        self.mw.col.db.recent = (3, 3)
        self.controller.space_answer = True
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
        )
        self.assertEqual(result, 3)

    def test_long_answer_requests_same_day_re_review(self):
        self.controller.space_answer = True
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 1500, "question_start": 1.0, "answer_start": 2.5},
        )
        self.assertEqual(result, 1)

    def test_manual_rating_is_left_unchanged(self):
        self.controller.space_answer = False
        result = self.controller._effective_ease(
            self.card,
            3,
            {"elapsed_ms": 100, "question_start": 1.0, "answer_start": 1.1},
        )
        self.assertEqual(result, 3)

    def test_state_file_is_in_active_profile(self):
        self.assertEqual(
            Path(self.controller.state_path).parent,
            Path(self.temporary_directory.name),
        )


if __name__ == "__main__":
    unittest.main()
