import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import aqt
    from anki.collection import Collection
except ImportError:
    Collection = None

if Collection is not None:
    addon_path = Path(__file__).resolve().parents[1] / "anki_batch_auto_easy"
    package = types.ModuleType("anki_batch_auto_easy")
    package.__path__ = [str(addon_path)]
    sys.modules.setdefault("anki_batch_auto_easy", package)

from anki_batch_auto_easy.adapter import AnkiReplacementAdapter, _scheduling_states


class SchedulerCompatibilityTests(unittest.TestCase):
    def test_newer_than_269_is_allowed_when_required_apis_exist(self):
        scheduler = SimpleNamespace(
            answer_card=lambda _answer: None,
            build_answer=lambda **_kwargs: None,
        )
        backend = SimpleNamespace(
            get_next_card_states=lambda card_id: ("states", card_id)
        )
        collection = SimpleNamespace(sched=scheduler, _backend=backend, db=None)
        aqt = types.ModuleType("aqt")
        aqt.appVersion = "2.1.50"

        with patch.dict(sys.modules, {"aqt": aqt}):
            AnkiReplacementAdapter(collection).ensure_supported()

    def test_uses_anki_2150_scheduling_state_api(self):
        expected = object()
        backend = SimpleNamespace(
            get_next_card_states=lambda card_id: expected
        )
        collection = SimpleNamespace(_backend=backend)
        self.assertIs(_scheduling_states(collection, 42), expected)


@unittest.skipUnless(
    Collection is not None and hasattr(Collection, "set_v3_scheduler"),
    "requires the Anki 2.1.50 scheduler runtime",
)
class Anki2150SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oneki-scheduler-compat-")
        self.col = Collection(str(Path(self.temp.name) / "collection.anki2"))
        self.col.upgrade_to_v2_scheduler()

    def tearDown(self):
        self.col.close()
        self.temp.cleanup()

    def _add_card(self):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note.fields[:] = ["Question", "Answer"]
        self.col.add_note(note, 1)
        card_id = int(self.col.card_ids_of_note(note.id)[0])
        card = self.col.get_card(card_id)
        card.start_timer()
        return card

    def test_v3_uses_2150_backend_states_api(self):
        from anki.scheduler.v3 import CardAnswer

        self.col.set_v3_scheduler(True)
        card = self._add_card()
        states = _scheduling_states(self.col, int(card.id))
        answer = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.GOOD
        )
        self.col.sched.answer_card(answer)
        row = self.col.db.first(
            "select ease from revlog where cid=? order by id desc limit 1", card.id
        )
        self.assertEqual(int(row[0]), 3)

    def test_v2_replays_original_grade_and_keeps_review_undo(self):
        scheduler_module = type(self.col.sched).__module__
        if not (
            scheduler_module.endswith(".sched")
            or scheduler_module.endswith(".scheduler.v2")
        ):
            self.skipTest("requires an Anki runtime with the classic V2 scheduler")

        from anki_batch_auto_easy.legacy import CARD_COLUMNS, LegacyController

        self.col.set_v3_scheduler(False)
        card = self._add_card()
        before = self.col.db.first(
            "select %s from cards where id=?" % ",".join(CARD_COLUMNS), card.id
        )
        original_card = dict(zip(CARD_COLUMNS, before))
        self.col.sched.answerCard(card, 4)

        mw = SimpleNamespace(
            pm=SimpleNamespace(profileFolder=lambda: self.temp.name),
            col=self.col,
        )
        controller = LegacyController(mw)
        controller._record_change(
            card,
            original_ease=3,
            effective_ease=4,
            timing={
                "elapsed_ms": 500,
                "question_start": 1.0,
                "answer_start": 1.5,
            },
            original_card=original_card,
        )
        review_id = controller.state["instant_undo"]["post_revlog"]["id"]
        controller.undo_rating_change()

        row = self.col.db.first(
            "select ease,id from revlog where cid=? order by id desc limit 1", card.id
        )
        self.assertEqual((int(row[0]), int(row[1])), (3, int(review_id)))
        self.assertIsNone(controller.state.get("instant_undo"))


if __name__ == "__main__":
    unittest.main()
