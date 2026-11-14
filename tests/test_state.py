from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from anki_batch_auto_easy.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_legacy_auto_hard_streak_state_is_removed_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "daily": {},
                        "batches": {},
                        "timing_history": [],
                        "auto_hard_streaks": {"42": 1},
                    }
                ),
                encoding="utf-8",
            )

            state = StateStore(path).load()

        self.assertNotIn("auto_hard_streaks", state)
        self.assertIn("daily", state)
        self.assertIn("batches", state)


if __name__ == "__main__":
    unittest.main()
