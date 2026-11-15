"""Small smoke check that runs with Anki's Python 3.6 runtime."""

import os
import sys
import tempfile
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from anki_batch_auto_easy.legacy import LegacyController


class FakeDatabase(object):
    def all(self, query, *args):
        return []


root = tempfile.mkdtemp(prefix="oneki-python36-")
mw = types.SimpleNamespace(
    pm=types.SimpleNamespace(profileFolder=lambda: root),
    col=types.SimpleNamespace(db=FakeDatabase()),
)
controller = LegacyController(mw)
controller.config = lambda: {
    "enabled": True,
    "auto_easy_enabled": True,
    "auto_hard_enabled": False,
    "strategy": "fixed",
    "quota_enabled": False,
    "threshold_ms": 1000,
    "history_window": 2,
}
controller.space_answer = True
card = types.SimpleNamespace(id=10, did=1, type=2, lapses=1)
rating = controller._effective_ease(
    card,
    3,
    {"elapsed_ms": 500, "question_start": 1.0, "answer_start": 1.5},
)
if rating != 4:
    raise SystemExit("expected a fast Good answer to become Easy")

print("Python 3.6 legacy runtime smoke check passed")
