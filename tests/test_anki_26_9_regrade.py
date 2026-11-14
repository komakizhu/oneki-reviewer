"""Integration checks for the Anki 26.9.3 scheduler, history, and Qt UI.

Run with the Anki 26.9.3 runtime (the PyPI aqt package or bundled Anki resources):
ANKI_RESOURCES="/Applications/Anki.app/Contents/Resources" \
  /path/to/python3.13 -m unittest discover -s tests -p 'test_anki_26_9_regrade.py' -v
"""

from __future__ import annotations

import base64
import copy
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


_resources = os.environ.get("ANKI_RESOURCES")
if _resources:
    _resources_path = Path(_resources)
    sys.path.insert(0, str(_resources_path / "app"))
    sys.path.insert(1, str(_resources_path / "app_packages"))

try:
    import anki

    if _resources:
        anki.__path__.append(str(Path(_resources) / "app_packages" / "anki"))
    from anki.collection import Collection
    from anki.scheduler.v3 import CardAnswer
except ImportError:
    Collection = None
    CardAnswer = None

BatchController = None
_review_shortcut_script = None
if Collection is not None:
    _addon_path = Path(__file__).resolve().parents[1] / "anki_batch_auto_easy"
    _package = types.ModuleType("anki_batch_auto_easy")
    _package.__path__ = [str(_addon_path)]
    sys.modules.setdefault("anki_batch_auto_easy", _package)
    from anki_batch_auto_easy import i18n as addon_i18n
    _original_anki_language_code = addon_i18n._anki_language_code

    # Existing UI assertions use Chinese copy. Keep the Anki runtime itself in
    # its configured language so collection fixtures retain native model names.
    from anki_batch_auto_easy.integration import (
        BatchController,
        DEFAULT_CONFIG,
        RuntimeAttempt,
        _review_shortcut_script,
        _row_by_id_from_collection,
        _timeout_grade,
    )
    from anki_batch_auto_easy.models import Attempt
    from anki_batch_auto_easy.adapter import (
        AnkiReplacementAdapter,
    )


def setUpModule() -> None:
    if Collection is not None:
        addon_i18n._anki_language_code = lambda: "zh_CN"


def tearDownModule() -> None:
    if Collection is not None:
        addon_i18n._anki_language_code = _original_anki_language_code


@unittest.skipIf(Collection is None, "set ANKI_RESOURCES to an Anki desktop runtime")
class AnkiSchedulerRegradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="anki-auto-easy-")
        self.col = Collection(str(Path(self.temp.name) / "collection.anki2"))

    def tearDown(self) -> None:
        self.col.close()
        self.temp.cleanup()

    def _add_card(self, *, review_card: bool = False) -> tuple[Any, Any]:
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note.fields[:] = ["Temporary question", "Temporary answer"]
        self.col.add_note(note, 1)
        card_id = int(self.col.card_ids_of_note(note.id)[0])
        if review_card:
            # Use Anki's collection API to prepare a due review-card fixture.
            card = self.col.get_card(card_id)
            card.type = 2
            card.queue = 2
            card.due = int(self.col.sched.today)
            card.ivl = 10
            card.reps = 7
            self.col.update_card(card)
            self.older_undo = "Update Card"
        else:
            self.older_undo = "Add Note"
        card = self.col.get_card(card_id)
        card.start_timer()
        queued = self.col.sched.get_queued_cards(fetch_limit=100).cards
        scheduling = next(value.states for value in queued if int(value.card.id) == card_id)
        return card, scheduling

    def _revlog(self, card_id: int) -> list[dict[str, int]]:
        rows = self.col.db.all(
            "select id, cid, ease, time from revlog where cid=? order by id",
            card_id,
        )
        return [
            {
                "id": int(row[0]),
                "cid": int(row[1]),
                "ease": int(row[2]),
                "time": int(row[3]),
            }
            for row in rows
        ]

    def _full_row(self, table: str, row_id: int) -> dict[str, Any] | None:
        return _row_by_id_from_collection(self.col, table, row_id)

    def _batch_controller(self, config: dict[str, Any] | None = None) -> Any:
        settings = {
            **DEFAULT_CONFIG,
            "strategy": "smart",
            "smart_minimum_mode": "count",
            "minimum_batch_size": 1,
            **(config or {}),
        }
        controller = BatchController(
            types.SimpleNamespace(
                col=self.col,
                pm=types.SimpleNamespace(profileFolder=lambda: self.temp.name),
                addonManager=types.SimpleNamespace(
                    getConfig=lambda _package: dict(settings)
                ),
            )
        )
        controller.notifications = []
        controller.diagnostics = []
        controller._notify = lambda message, **_kwargs: (
            controller.notifications.append(message)
        )
        controller._log_background = lambda message, _exc=None: (
            controller.diagnostics.append(message)
        )
        controller._show_instant_undo_notice = lambda *_args: None
        return controller

    def _realtime_answer(
        self,
        controller: Any,
        card_id: int,
        *,
        elapsed_ms: int = 500,
        input_source: str = "space",
    ) -> Any:
        card = self.col.get_card(card_id)
        self.col.update_card(card)
        card.start_timer()
        states = self.col._backend.get_scheduling_states(card_id)
        reviewer = types.SimpleNamespace(
            card=card, _v3=types.SimpleNamespace(states=states)
        )
        controller.did_show_question(card)
        controller.did_show_answer(card)
        controller.answer_timer_started[card_id] = time.monotonic() - elapsed_ms / 1000
        if input_source == "space":
            controller.space_answer = (reviewer, card, object())
        proceed, ease = controller.will_answer((True, 3), reviewer, card)
        self.assertTrue(proceed)
        answer = self.col.sched.build_answer(
            card=card,
            states=states,
            rating={
                1: CardAnswer.AGAIN,
                2: CardAnswer.HARD,
                3: CardAnswer.GOOD,
                4: CardAnswer.EASY,
            }[ease],
        )
        self.col.sched.answer_card(answer)
        controller.did_answer(reviewer, card, ease)
        batch = controller.state["batches"][controller.current_batch_key]
        return RuntimeAttempt.from_dict(batch["attempts"][-1])

    def _batch_answer(
        self,
        controller: Any,
        card_id: int,
        *,
        input_source: str = "space",
        from_queue: bool = False,
        update_card: bool = True,
        rating: int = 3,
    ) -> Any:
        card = self.col.get_card(card_id)
        if from_queue:
            queued = self.col.sched.get_queued_cards(fetch_limit=100).cards
            self.assertTrue(queued)
            self.assertEqual(int(queued[0].card.id), card_id)
            states = queued[0].states
        else:
            if update_card:
                self.col.update_card(card)
            states = self.col._backend.get_scheduling_states(card_id)
        card.start_timer()
        original_card = self._full_row("cards", card_id)
        answer_by_rating = {
            1: CardAnswer.AGAIN,
            2: CardAnswer.HARD,
            3: CardAnswer.GOOD,
            4: CardAnswer.EASY,
        }
        rated = self.col.sched.build_answer(
            card=card, states=states, rating=answer_by_rating[rating]
        )
        easy = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.EASY
        )
        previous_ids = [
            int(value["original_revlog"]["id"])
            for batch in controller.state["batches"].values()
            for value in batch["attempts"]
        ]
        answered_at = max(
            int(rated.answered_at_millis),
            max(previous_ids, default=0) + 1,
        )
        rated.answered_at_millis = answered_at
        easy.answered_at_millis = answered_at
        rated.milliseconds_taken = 1250
        easy.milliseconds_taken = 1250
        self.col.sched.answer_card(rated)
        day = time.strftime("%Y-%m-%d")
        if controller.review_session_id is None:
            controller.review_session_id = "test-review-session"
        controller._ensure_batch(f"{day}:{card.did}", day, int(card.did))
        batch = controller.state["batches"][controller.current_batch_key]
        batch["session_id"] = controller.review_session_id
        sequence = len(batch["attempts"])
        attempt_id = f"batch:{sequence}:{card_id}:{answered_at}"
        runtime = RuntimeAttempt(
            attempt=Attempt(
                attempt_id=attempt_id,
                card_id=card_id,
                deck_id=int(card.did),
                sequence=sequence,
                original_ease=rating,
                elapsed_ms=500,
                prior_ratings=(),
                leaves_today_learning=True,
                answered_at_ms=answered_at,
                batch_auto_easy_eligible=(input_source == "space" and rating == 3),
            ),
            original_card=original_card,
            post_card=self._full_row("cards", card_id),
            original_revlog=self._full_row("revlog", answered_at),
            easy_answer_b64=base64.b64encode(easy.SerializeToString()).decode("ascii"),
            input_source=input_source,
            user_ease=rating,
            original_answer_b64=base64.b64encode(rated.SerializeToString()).decode(
                "ascii"
            ),
            reviewer_undo_step=int(self.col.undo_status().last_step),
        )
        controller._store_attempt(runtime)
        controller.state["timing_history"].append(
            {
                "attempt_id": attempt_id,
                "user_ease": rating,
                "effective_ease": rating,
                "question_to_grade_ms": 1250,
                "answer_to_grade_ms": 500,
            }
        )
        return runtime

    def _deck_counts(self, deck_ids: list[int]) -> dict[int, dict[str, Any]]:
        return {
            deck_id: {
                key: self.col.decks.get(deck_id)[key]
                for key in ("newToday", "revToday", "lrnToday", "timeToday")
            }
            for deck_id in deck_ids
        }

    def test_batch_replacement_preserves_answer_counts_and_time(self) -> None:
        for card_kind in ("new", "learning", "review"):
            if card_kind != "new":
                self.tearDown()
                self.setUp()
            with self.subTest(card_kind=card_kind):
                card, _states = self._add_card(review_card=card_kind == "review")
                parent_id = self.col.decks.id("Batch")
                child_id = self.col.decks.id("Batch::Child")
                card.did = child_id
                self.col.update_card(card)
                controller = self._batch_controller()
                if card_kind == "learning":
                    self._batch_answer(controller, int(card.id))
                    self.assertEqual(self.col.get_card(card.id).type, 1)
                runtime = self._batch_answer(controller, int(card.id))
                counts = self._deck_counts([parent_id, child_id])
                scheduler_reps = self.col.sched.reps
                card_reps = self.col.get_card(card.id).reps
                adapter = AnkiReplacementAdapter(self.col)

                replaced = adapter.replace_many([runtime.replacement_record()])

                self.assertEqual(
                    replaced.replaced_attempt_ids,
                    [runtime.attempt.attempt_id],
                )
                self.assertEqual(self._deck_counts([parent_id, child_id]), counts)
                self.assertEqual(self.col.sched.reps, scheduler_reps)
                self.assertEqual(self.col.get_card(card.id).reps, card_reps)
                self.assertEqual(
                    len(self._revlog(int(card.id))),
                    2 if card_kind == "learning" else 1,
                )
                self.assertEqual(self._revlog(int(card.id))[-1]["ease"], 4)
                self.assertEqual(self._revlog(int(card.id))[-1]["time"], 1250)
                self.assertEqual(
                    adapter.replace_many([runtime.replacement_record()]).replaced_attempt_ids,
                    [],
                )
                self.assertEqual(self._deck_counts([parent_id, child_id]), counts)
                self.assertEqual(self.col.sched.reps, scheduler_reps)

    def test_last_card_batch_finalization_preserves_native_undo(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        runtime = self._batch_answer(controller, int(card.id))
        batch = controller.state["batches"][controller.current_batch_key]
        batch["session_id"] = "test-review-session"
        controller.review_session_id = batch["session_id"]
        controller._reviewer_queue_empty = lambda: True

        controller.will_end()

        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 4)
        self.assertEqual(self.col.undo_status().undo, "Answer Card")
        self.col.undo()
        self.assertEqual(self._revlog(int(card.id)), [])
        self.col.redo()
        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 4)
        controller.will_end()
        self.assertEqual(len(self._revlog(int(card.id))), 1)
        self.assertEqual(len(controller.notifications), 1)
        self.assertEqual(
            controller.notifications,
            ["复习结束：已将 1 张卡的评分由良好调整为简单"],
        )

    def test_multi_card_finalization_replays_answers_in_native_undo_order(self) -> None:
        cards = [self._add_card(review_card=True)[0] for _index in range(3)]
        controller = self._batch_controller()
        queued_ids = [
            int(value.card.id)
            for value in self.col.sched.get_queued_cards(fetch_limit=100).cards
        ]
        runtimes = [
            self._batch_answer(controller, card_id, from_queue=True)
            for card_id in queued_ids
        ]
        before_counts = self._deck_counts([1])
        before_reps = self.col.sched.reps
        before_card_reps = {
            int(card.id): self.col.get_card(card.id).reps for card in cards
        }
        batch_key = controller.current_batch_key

        controller.finalize(batch_key)

        batch = controller.state["batches"][batch_key]
        upgraded_ids = set(batch["replaced_attempt_ids"])
        for runtime in runtimes:
            rows = self._revlog(runtime.attempt.card_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["ease"],
                4 if runtime.attempt.attempt_id in upgraded_ids else 3,
            )
            self.assertEqual(rows[0]["id"], runtime.original_revlog["id"])
            self.assertEqual(rows[0]["time"], runtime.original_revlog["time"])
            self.assertEqual(
                self.col.get_card(runtime.attempt.card_id).reps,
                before_card_reps[runtime.attempt.card_id],
            )
        self.assertEqual(self._deck_counts([1]), before_counts)
        self.assertEqual(self.col.sched.reps, before_reps)
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

        last = runtimes[-1]
        expected_last_ease = (
            4 if last.attempt.attempt_id in upgraded_ids else 3
        )
        for runtime in reversed(runtimes):
            self.assertEqual(self.col.undo_status().undo, "Answer Card")
            self.col.undo()
            self.assertEqual(self._revlog(runtime.attempt.card_id), [])
        for runtime in runtimes:
            self.assertEqual(self.col.undo_status().redo, "Answer Card")
            self.col.redo()
            expected_ease = 4 if runtime.attempt.attempt_id in upgraded_ids else 3
            self.assertEqual(
                self._revlog(runtime.attempt.card_id)[0]["ease"], expected_ease
            )
        self.assertEqual(
            self._revlog(last.attempt.card_id)[0]["ease"], expected_last_ease
        )

    def test_repeated_learning_answer_replays_and_remains_undoable(self) -> None:
        card, _states = self._add_card(review_card=True)
        deck_config = self.col.decks.config_dict_for_deck_id(int(card.did))
        deck_config["lapse"]["delays"] = [0.0001]
        self.col.decks.save(deck_config)
        controller = self._batch_controller()
        first = self._batch_answer(
            controller,
            int(card.id),
            update_card=False,
            from_queue=True,
            rating=1,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            queued_ids = {
                int(value.card.id)
                for value in self.col.sched.get_queued_cards(fetch_limit=100).cards
            }
            if int(card.id) in queued_ids:
                break
            time.sleep(0.01)
        self.assertIn(int(card.id), queued_ids)
        second = self._batch_answer(
            controller,
            int(card.id),
            update_card=False,
            from_queue=True,
        )

        controller.finalize(controller.current_batch_key)

        rows = self._revlog(int(card.id))
        self.assertEqual(
            [row["id"] for row in rows],
            [first.original_revlog["id"], second.original_revlog["id"]],
        )
        self.assertEqual([row["ease"] for row in rows], [1, 4])
        self.assertEqual([row["time"] for row in rows], [1250, 1250])
        batch = controller.state["batches"][controller.current_batch_key]
        self.assertIn(second.attempt.attempt_id, batch["replaced_attempt_ids"])
        self.assertNotIn("regrade_skip_reason", batch)
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

        self.col.undo()
        remaining = self._revlog(int(card.id))
        self.assertEqual(
            [row["id"] for row in remaining], [first.original_revlog["id"]]
        )
        self.assertEqual(remaining[0]["ease"], 1)
        self.col.undo()
        self.assertEqual(self._revlog(int(card.id)), [])
        self.col.redo()
        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 1)
        self.col.redo()
        self.assertEqual([row["ease"] for row in self._revlog(int(card.id))], [1, 4])

    def test_failed_multi_card_regrade_restores_answers_and_undo(self) -> None:
        self._add_card(review_card=True)
        self._add_card(review_card=True)
        controller = self._batch_controller()
        queued_ids = [
            int(value.card.id)
            for value in self.col.sched.get_queued_cards(fetch_limit=100).cards
        ]
        runtimes = [
            self._batch_answer(controller, card_id, from_queue=True)
            for card_id in queued_ids
        ]
        before_counts = self._deck_counts([1])
        before_reps = self.col.sched.reps
        answer_card = self.col.sched.answer_card
        calls = 0

        def fail_second_replay(answer: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("test regrade failure")
            return answer_card(answer)

        with patch.object(self.col.sched, "answer_card", fail_second_replay):
            controller.finalize(controller.current_batch_key)

        for runtime in runtimes:
            rows = self._revlog(runtime.attempt.card_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ease"], 3)
            self.assertEqual(rows[0]["id"], runtime.original_revlog["id"])
            self.assertEqual(rows[0]["time"], runtime.original_revlog["time"])
        self.assertEqual(self._deck_counts([1]), before_counts)
        self.assertEqual(self.col.sched.reps, before_reps)
        self.assertEqual(self.col.undo_status().undo, "Answer Card")
        self.assertIn("原评分和 Anki 撤销记录已保留", controller.diagnostics[-1])

        self.col.undo()
        self.assertEqual(self._revlog(runtimes[-1].attempt.card_id), [])
        self.col.redo()
        self.assertEqual(self._revlog(runtimes[-1].attempt.card_id)[0]["ease"], 3)

    def test_repeated_card_keeps_scores_when_history_is_not_replayable(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        self._batch_answer(controller, int(card.id))
        self._batch_answer(controller, int(card.id))
        batch_key = controller.current_batch_key

        controller.finalize(batch_key)

        rows = self._revlog(int(card.id))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["ease"] for row in rows], [3, 3])
        batch = controller.state["batches"][batch_key]
        self.assertEqual(batch["replaced_attempt_ids"], [])
        self.assertTrue(batch.get("regrade_skip_reason"))
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

    def test_fixed_review_is_upgraded_before_session_end(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller({"strategy": "fixed"})

        runtime = self._realtime_answer(controller, int(card.id))

        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 4)
        self.assertTrue(runtime.instant_upgraded)
        self.assertFalse(runtime.attempt.batch_auto_easy_eligible)
        self.assertEqual(controller.state["timing_history"][0]["effective_ease"], 4)
        self.assertNotIn("预计", controller.batch_preview())
        before_card = self._full_row("cards", int(card.id))
        before_logs = self._revlog(int(card.id))
        before_counts = self._deck_counts([1])
        controller._reviewer_queue_empty = lambda: True
        controller.notifications.clear()

        with patch("anki_batch_auto_easy.integration.AnkiReplacementAdapter") as adapter:
            controller.will_end()

        adapter.assert_not_called()
        self.assertEqual(self._full_row("cards", int(card.id)), before_card)
        self.assertEqual(self._revlog(int(card.id)), before_logs)
        self.assertEqual(self._deck_counts([1]), before_counts)
        self.assertEqual(
            controller.notifications,
            ["本轮复习汇总：答题时已自动按「简单」评分 1 张。"],
        )

    def test_fixed_quota_does_not_backfill_earlier_answers(self) -> None:
        for earlier_elapsed_ms in (500, 1500):
            if earlier_elapsed_ms == 1500:
                self.tearDown()
                self.setUp()
            with self.subTest(earlier_elapsed_ms=earlier_elapsed_ms):
                cards = [self._add_card(review_card=True)[0] for _index in range(5)]
                controller = self._batch_controller(
                    {"strategy": "fixed", "quota_enabled": True, "quota_percent": 0.2}
                )
                for index, card in enumerate(cards):
                    self._realtime_answer(
                        controller,
                        int(card.id),
                        elapsed_ms=earlier_elapsed_ms if index < 4 else 500,
                    )
                grades = [self._revlog(int(card.id))[0]["ease"] for card in cards]
                self.assertEqual(grades, [3, 3, 3, 3, 4])

                with patch(
                    "anki_batch_auto_easy.integration.AnkiReplacementAdapter"
                ) as adapter:
                    controller.finalize(controller.current_batch_key)

                adapter.assert_not_called()
                self.assertEqual(
                    [self._revlog(int(card.id))[0]["ease"] for card in cards], grades
                )
                daily = controller._daily_stats(time.strftime("%Y-%m-%d"))
                self.assertEqual(daily.auto_upgraded, 1)
                self.assertEqual(daily.eligible_total, 5)

    def test_fixed_finalization_does_not_upgrade_old_batch_candidates(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller({"strategy": "fixed"})
        self._batch_answer(controller, int(card.id))

        with patch("anki_batch_auto_easy.integration.AnkiReplacementAdapter") as adapter:
            controller.finalize(controller.current_batch_key)

        adapter.assert_not_called()
        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 3)
        self.assertEqual(controller.notifications, ["本轮复习汇总：没有自动调整评分。"])

    def test_fixed_session_summary_combines_decks(self) -> None:
        first, _states = self._add_card(review_card=True)
        second, _states = self._add_card(review_card=True)
        second.did = self.col.decks.id("Other")
        self.col.update_card(second)
        controller = self._batch_controller({"strategy": "fixed"})
        self._realtime_answer(controller, int(first.id))
        self._realtime_answer(controller, int(second.id))
        controller._reviewer_queue_empty = lambda: True
        controller.notifications.clear()

        with patch("anki_batch_auto_easy.integration.AnkiReplacementAdapter") as adapter:
            controller.will_end()

        adapter.assert_not_called()
        self.assertEqual(
            controller.notifications,
            ["本轮复习汇总：答题时已自动按「简单」评分 2 张。"],
        )
        self.assertEqual(self._revlog(int(first.id))[0]["ease"], 4)
        self.assertEqual(self._revlog(int(second.id))[0]["ease"], 4)

    def test_fixed_undo_and_redo_update_completed_summary(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller({"strategy": "fixed"})
        self._realtime_answer(controller, int(card.id))
        self.col.undo()
        controller.native_undo_occurred(None)
        self.assertIn("没有自动调整评分", controller.batch_preview())
        self.col.redo()
        controller.native_undo_occurred(None)
        controller.notifications.clear()

        controller.finalize(controller.current_batch_key)

        self.assertEqual(
            controller.notifications[-1],
            "本轮复习汇总：答题时已自动按「简单」评分 1 张。",
        )
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(controller._daily_stats(day).auto_upgraded, 1)
        self.col.undo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._daily_stats(day).auto_upgraded, 0)
        self.col.redo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._daily_stats(day).auto_upgraded, 1)

    def test_distribution_modes_still_apply_grades_after_answers(self) -> None:
        for strategy in ("percent", "smart"):
            if strategy == "smart":
                self.tearDown()
                self.setUp()
            with self.subTest(strategy=strategy):
                card, _states = self._add_card(review_card=True)
                controller = self._batch_controller(
                    {"strategy": strategy, "distribution_percent": 1.0}
                )
                runtime = self._realtime_answer(controller, int(card.id))
                self.assertFalse(runtime.instant_upgraded)
                self.assertEqual(self._revlog(int(card.id))[0]["ease"], 3)

                controller.finalize(controller.current_batch_key)

                self.assertEqual(self._revlog(int(card.id))[0]["ease"], 4)

    def test_stale_session_history_preserves_original_scores(self) -> None:
        for change in ("undo", "flags"):
            if change != "undo":
                self.tearDown()
                self.setUp()
            with self.subTest(change=change):
                first, _states = self._add_card(review_card=True)
                second, _states = self._add_card(review_card=True)
                controller = self._batch_controller()
                valid = self._batch_answer(controller, int(first.id))
                stale = self._batch_answer(controller, int(second.id))
                if change == "undo":
                    self.col.undo()
                else:
                    changed_card = self.col.get_card(second.id)
                    changed_card.flags = 1
                    self.col.update_card(changed_card)

                replaced = AnkiReplacementAdapter(self.col).replace_many(
                    [valid.replacement_record(), stale.replacement_record()]
                )

                self.assertEqual(
                    replaced.replaced_attempt_ids,
                    [],
                )
                self.assertTrue(replaced.skipped_reason)
                self.assertEqual(self._revlog(int(first.id))[0]["ease"], 3)
                if change == "undo":
                    self.assertEqual(self._revlog(int(second.id)), [])
                else:
                    self.assertEqual(self._revlog(int(second.id))[0]["ease"], 3)
                    self.assertEqual(self.col.get_card(second.id).flags, 1)

    def test_batch_finalization_skips_when_history_changes_mid_review(self) -> None:
        first, _states = self._add_card(review_card=True)
        second, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        self._batch_answer(controller, int(first.id))
        self._batch_answer(controller, int(second.id))
        changed_card = self.col.get_card(second.id)
        changed_card.flags = 1
        self.col.update_card(changed_card)
        samples = copy.deepcopy(controller.state["timing_history"])

        controller.finalize(controller.current_batch_key)

        self.assertEqual(self._revlog(int(first.id))[0]["ease"], 3)
        self.assertEqual(self._revlog(int(second.id))[0]["ease"], 3)
        self.assertEqual(controller.state["timing_history"][0], samples[0])
        self.assertEqual(controller.state["timing_history"][1], samples[1])
        batch = controller.state["batches"][controller.current_batch_key]
        self.assertEqual(batch["replaced_attempt_ids"], [])
        self.assertIn("撤销历史", batch["regrade_skip_reason"])

    def test_batch_ignores_native_undo_before_reanswer(self) -> None:
        for emit_hook in (True, False):
            if not emit_hook:
                self.tearDown()
                self.setUp()
            with self.subTest(emit_hook=emit_hook):
                first, _states = self._add_card(review_card=True)
                second, _states = self._add_card(review_card=True)
                controller = self._batch_controller()
                self._batch_answer(controller, int(first.id))
                undone = self._batch_answer(controller, int(second.id))
                self.col.undo()
                if emit_hook:
                    controller.native_undo_occurred(None)
                    pending = controller._pending_daily_stats(
                        time.strftime("%Y-%m-%d"), 2
                    )
                    self.assertEqual(pending.reviewed_total, 1)
                self._batch_answer(controller, int(second.id), input_source="other")

                controller.finalize(controller.current_batch_key)

                batch = controller.state["batches"][controller.current_batch_key]
                self.assertEqual(batch["status"], "finalized")
                self.assertNotIn("last_error", batch)
                self.assertEqual(self._revlog(int(first.id))[0]["ease"], 3)
                self.assertEqual(self._revlog(int(second.id))[0]["ease"], 3)
                daily = controller._daily_stats(batch["day"])
                self.assertEqual(daily.reviewed_total, 2)
                self.assertEqual(daily.eligible_total, 1)
                self.assertEqual(daily.auto_upgraded, 0)
                self.assertTrue(batch.get("regrade_skip_reason"))
                undone_sample = next(
                    sample
                    for sample in controller.state["timing_history"]
                    if sample["attempt_id"] == undone.attempt.attempt_id
                )
                self.assertTrue(undone_sample["review_undone"])

    def test_native_redo_restores_batch_candidate(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        self._batch_answer(controller, int(card.id))
        day = time.strftime("%Y-%m-%d")

        self.col.undo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._pending_daily_stats(day, 2).reviewed_total, 0)
        self.col.redo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._pending_daily_stats(day, 2).reviewed_total, 1)
        self.assertEqual(controller._pending_daily_stats(day, 2).eligible_total, 1)

        controller.finalize(controller.current_batch_key)

        self.assertEqual(self._revlog(int(card.id))[0]["ease"], 3)
        batch = controller.state["batches"][controller.current_batch_key]
        self.assertTrue(batch.get("regrade_skip_reason"))
        self.assertEqual(controller._daily_stats(day).reviewed_total, 1)
        self.assertFalse(controller.state["timing_history"][0].get("review_undone", False))

    def test_native_undo_after_finalization_adjusts_daily_counts_once(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        runtime = self._batch_answer(controller, int(card.id), input_source="other")
        controller.finalize(controller.current_batch_key)
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(controller._daily_stats(day).reviewed_total, 1)

        self.col.undo()
        controller.native_undo_occurred(None)
        controller.native_undo_occurred(None)
        self.assertEqual(controller._daily_stats(day).reviewed_total, 0)
        self.col.redo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._daily_stats(day).reviewed_total, 1)
        self.assertEqual(controller._daily_stats(day).auto_upgraded, 0)
        self.assertEqual(self._revlog(runtime.attempt.card_id)[0]["ease"], 3)

    def test_native_undo_keeps_finalized_auto_upgrades_counted(self) -> None:
        card, _states = self._add_card(review_card=True)
        controller = self._batch_controller()
        runtime = self._batch_answer(controller, int(card.id))
        original_revlog = dict(runtime.original_revlog)
        controller.finalize(controller.current_batch_key)
        day = time.strftime("%Y-%m-%d")
        batch = controller.state["batches"][controller.current_batch_key]
        batch["attempts"][0]["original_revlog"] = original_revlog
        counts = controller._daily_stats(day)

        controller.native_undo_occurred(None)

        self.assertEqual(controller._daily_stats(day), counts)
        self.assertEqual(counts.auto_upgraded, 1)
        self.assertFalse(batch["attempts"][0].get("review_undone", False))

    def _answer_with_plugin_regrade(self, *, review_card: bool, auto_rating: int) -> None:
        card, states = self._add_card(review_card=review_card)
        before_reps = int(card.reps)
        card_id = int(card.id)
        before_card = self._full_row("cards", card_id)
        original = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.GOOD
        )
        automatic = self.col.sched.build_answer(
            card=card, states=states, rating=auto_rating
        )

        self.col.sched.answer_card(automatic)
        auto_row = self._revlog(int(card.id))[-1]
        self.assertEqual(auto_row["ease"], {0: 1, 1: 2, 3: 4}[auto_rating])
        post_card = self._full_row("cards", card_id)
        post_revlog = self._full_row("revlog", int(automatic.answered_at_millis))
        ease = {
            CardAnswer.AGAIN: 1,
            CardAnswer.HARD: 2,
            CardAnswer.GOOD: 3,
            CardAnswer.EASY: 4,
        }[auto_rating]
        attempt_id = f"test:{card_id}"
        batch_key = "2026-11-13:1"
        day = "2026-11-13"
        attempt = Attempt(
            attempt_id=attempt_id,
            card_id=card_id,
            deck_id=int(card.did),
            sequence=0,
            original_ease=ease,
            elapsed_ms=int(original.milliseconds_taken),
            prior_ratings=(),
            leaves_today_learning=True,
            answered_at_ms=int(original.answered_at_millis),
        )
        runtime = RuntimeAttempt(
            attempt=attempt,
            original_card=before_card or {},
            post_card=post_card,
            original_revlog=post_revlog,
            easy_answer_b64="",
            initial_new_learning=not review_card,
            instant_candidate=auto_rating == CardAnswer.EASY,
            instant_upgraded=auto_rating == CardAnswer.EASY,
            user_ease=3,
            original_answer_b64=base64.b64encode(
                original.SerializeToString()
            ).decode("ascii"),
        )
        sample = {
            "attempt_id": attempt_id,
            "card_id": card_id,
            "user_ease": 3,
            "effective_ease": ease,
            "question_to_grade_ms": 1000,
            "answer_to_grade_ms": int(original.milliseconds_taken),
        }
        entry = {
            "attempt_id": attempt_id,
            "card_id": card_id,
            "batch_key": batch_key,
            "day": day,
            "reviewer_undo_step": int(self.col.undo_status().last_step),
            "original_ease": 3,
            "effective_ease": ease,
            "original_answer_b64": runtime.original_answer_b64,
            "original_card": before_card,
            "post_card": post_card,
            "post_revlog": post_revlog,
            "timing_sample": sample,
            "status": "ready",
        }
        state = {
            "batches": {
                batch_key: {
                    "day": day,
                    "status": "open",
                    "daily_counted": False,
                    "attempts": [runtime.to_dict()],
                }
            },
            "timing_history": [dict(sample)],
            "initial_new_learning": (
                {str(card_id): True}
                if not review_card and int((post_card or {}).get("type", -1)) == 1
                else {}
            ),
            "instant_undo": entry,
        }
        controller = BatchController.__new__(BatchController)
        controller.state = state
        controller.mw = types.SimpleNamespace(
            col=self.col,
            reviewer=types.SimpleNamespace(refresh_if_needed=lambda: None),
            update_undo_actions=lambda: None,
        )
        controller.undo_in_progress = False
        controller.recovery_blocked = False
        controller.undo_notice = None
        controller.undo_button = None
        controller.shortcut_conflict = False
        controller.recovery_sync_retry_count = 0
        controller.current_batch_key = batch_key
        controller._save = lambda: None
        controller._set_review_shortcut_armed = lambda _armed: None
        notifications: list[str] = []
        diagnostics: list[str] = []
        controller._notify = lambda message, **_kwargs: notifications.append(message)
        controller._log_background = lambda message, _exc=None: diagnostics.append(message)

        class _ImmediateCollectionOp:
            def __init__(self, mw: Any, operation: Any):
                self.mw = mw
                self.operation = operation

            def success(self, callback: Any) -> _ImmediateCollectionOp:
                self.success_callback = callback
                return self

            def failure(self, callback: Any) -> _ImmediateCollectionOp:
                self.failure_callback = callback
                return self

            def run_in_background(self, *, initiator: Any) -> None:
                del initiator
                try:
                    result = self.operation(self.mw.col)
                except Exception as exc:
                    self.failure_callback(exc)
                else:
                    self.success_callback(result)

        from aqt import operations

        with patch.object(operations, "CollectionOp", _ImmediateCollectionOp):
            controller.undo_instant_regrade()

        rows = self._revlog(int(card.id))
        final_card = self.col.get_card(card.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ease"], 3)
        self.assertEqual(final_card.reps, before_reps + 1)
        self.assertEqual(rows[0]["id"], int(original.answered_at_millis))
        self.assertEqual(rows[0]["time"], int(original.milliseconds_taken))
        self.assertNotIn("instant_undo", controller.state)
        restored = RuntimeAttempt.from_dict(
            controller.state["batches"][batch_key]["attempts"][0]
        )
        self.assertEqual(restored.attempt.original_ease, 3)
        self.assertFalse(restored.attempt.batch_auto_easy_eligible)
        self.assertTrue(restored.grade_reverted)
        daily = controller._pending_daily_stats(day, 2)
        self.assertEqual(daily.reviewed_total, 1)
        self.assertEqual(daily.auto_upgraded, 0)
        self.assertEqual(daily.eligible_total, 0)
        self.assertEqual(controller.state["timing_history"][0]["effective_ease"], 3)
        self.assertIn("保留本次答题", notifications[-1])
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

        controller.undo_instant_regrade()
        self.assertEqual(len(self._revlog(int(card.id))), 1)
        self.assertEqual(self.col.get_card(card.id).reps, before_reps + 1)

        # Undoing the corrected answer still exposes the older operation.
        self.col.undo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._pending_daily_stats(day, 2).reviewed_total, 0)
        self.assertEqual(int(self.col.get_card(card.id).reps), before_reps)
        self.assertEqual(self._revlog(int(card.id)), [])
        self.assertEqual(self.col.undo_status().undo, self.older_undo)
        self.col.redo()
        controller.native_undo_occurred(None)
        self.assertEqual(controller._pending_daily_stats(day, 2).reviewed_total, 1)
        self.assertEqual(self._revlog(int(card.id))[-1]["ease"], 3)
        self.assertEqual(self.col.get_card(card.id).reps, before_reps + 1)

    def test_good_to_easy_hard_and_again_for_new_and_review_cards(self) -> None:
        scenario = 0
        for review_card in (False, True):
            for auto_rating in (CardAnswer.EASY, CardAnswer.HARD, CardAnswer.AGAIN):
                if scenario:
                    self.tearDown()
                    self.setUp()
                scenario += 1
                with self.subTest(review_card=review_card, auto_rating=auto_rating):
                    self._answer_with_plugin_regrade(
                        review_card=review_card,
                        auto_rating=auto_rating,
                    )

    def test_failed_original_rating_is_compensated_with_redo(self) -> None:
        card, states = self._add_card()
        automatic = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.HARD
        )
        self.col.sched.answer_card(automatic)
        before = self._revlog(int(card.id))
        saved_reps = int(self.col.sched.reps)

        self.col.undo()
        self.col.sched.reps = max(0, saved_reps - 1)
        with self.assertRaises(Exception):
            self.col.sched.answer_card(None)
        self.col.redo()
        self.col.sched.reps = saved_reps

        self.assertEqual(self._revlog(int(card.id)), before)
        self.assertEqual(self.col.get_card(card.id).reps, 1)
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

    def test_interrupted_after_undo_can_replay_the_saved_answer_once(self) -> None:
        card, states = self._add_card()
        original = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.GOOD
        )
        automatic = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.AGAIN
        )
        self.col.sched.answer_card(automatic)
        self.col.undo()
        self.col.close()

        self.col = Collection(str(Path(self.temp.name) / "collection.anki2"))
        self.col.sched.answer_card(original)

        rows = self._revlog(int(card.id))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ease"], 3)
        self.assertEqual(self.col.get_card(card.id).reps, 1)
        self.assertEqual(rows[0]["id"], int(original.answered_at_millis))
        self.assertEqual(rows[0]["time"], int(original.milliseconds_taken))

    def test_compensation_failure_is_journaled_and_recovered_after_restart(self) -> None:
        card, states = self._add_card()
        card_id = int(card.id)
        original_card = self._full_row("cards", card_id)
        original = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.GOOD
        )
        automatic = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.AGAIN
        )
        self.col.sched.answer_card(automatic)
        post_card = self._full_row("cards", card_id)
        post_revlog = self._full_row("revlog", int(automatic.answered_at_millis))
        self.assertEqual(self.col.undo_status().undo, "Answer Card")
        self.assertIsNotNone(post_card)
        self.assertIsNotNone(post_revlog)
        attempt_id = f"isolated:{card_id}"
        batch_key = "2026-11-13:1"
        attempt = Attempt(
            attempt_id=attempt_id,
            card_id=card_id,
            deck_id=int(card.did),
            sequence=0,
            original_ease=1,
            elapsed_ms=int(original.milliseconds_taken),
            prior_ratings=(),
            leaves_today_learning=True,
            answered_at_ms=int(original.answered_at_millis),
        )
        runtime = RuntimeAttempt(
            attempt=attempt,
            original_card=original_card or {},
            post_card=post_card,
            original_revlog=post_revlog,
            easy_answer_b64="",
            initial_new_learning=True,
            user_ease=3,
            original_answer_b64=base64.b64encode(
                original.SerializeToString()
            ).decode("ascii"),
        )
        batch_state = {
            "batches": {
                batch_key: {
                    "day": "2026-11-13",
                    "status": "open",
                    "daily_counted": False,
                    "attempts": [runtime.to_dict()],
                }
            },
            "timing_history": [
                {
                    "attempt_id": attempt_id,
                    "card_id": card_id,
                    "user_ease": 3,
                    "effective_ease": 1,
                    "question_to_grade_ms": 1200,
                }
            ],
            "initial_new_learning": {},
        }
        journal = {
            "attempt_id": attempt_id,
            "card_id": card_id,
            "batch_key": batch_key,
            "day": "2026-11-13",
            "reviewer_undo_step": int(self.col.undo_status().last_step),
            "original_ease": 3,
            "effective_ease": 1,
            "original_answer_b64": runtime.original_answer_b64,
            "original_card": original_card,
            "post_card": post_card,
            "post_revlog": post_revlog,
            "timing_sample": dict(batch_state["timing_history"][0]),
            "status": "ready",
        }
        persisted: dict[str, Any] = {}

        class _FailingScheduler:
            def __init__(self, scheduler: Any):
                self._scheduler = scheduler

            @property
            def reps(self) -> int:
                return int(self._scheduler.reps)

            @reps.setter
            def reps(self, value: int) -> None:
                self._scheduler.reps = value

            def answer_card(self, _answer: Any) -> None:
                raise RuntimeError("injected replay failure")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._scheduler, name)

        class _FailingCollection:
            def __init__(self, collection: Any):
                self._collection = collection
                self.sched = _FailingScheduler(collection.sched)

            def undo(self) -> Any:
                return self._collection.undo()

            def redo(self) -> None:
                raise RuntimeError("injected compensation redo failure")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._collection, name)

        class _ImmediateCollectionOp:
            def __init__(self, mw: Any, operation: Any):
                self.mw = mw
                self.operation = operation

            def success(self, callback: Any) -> _ImmediateCollectionOp:
                self.success_callback = callback
                return self

            def failure(self, callback: Any) -> _ImmediateCollectionOp:
                self.failure_callback = callback
                return self

            def run_in_background(self, *, initiator: Any) -> None:
                del initiator
                try:
                    result = self.operation(self.mw.col)
                except Exception as exc:
                    self.failure_callback(exc)
                else:
                    self.success_callback(result)

        controller = BatchController.__new__(BatchController)
        controller.state = {
            **batch_state,
            "instant_undo": journal,
        }
        controller.mw = types.SimpleNamespace(
            col=_FailingCollection(self.col),
            update_undo_actions=lambda: None,
            reviewer=None,
        )
        controller.undo_in_progress = False
        controller.recovery_blocked = False
        controller.undo_notice = None
        controller.undo_button = None
        controller.shortcut_conflict = False
        controller.recovery_sync_retry_count = 0
        controller.config = lambda: {"instant_undo_shortcut": "Z"}
        controller._set_review_shortcut_armed = lambda _armed: None
        controller._save = lambda: persisted.update(state=copy.deepcopy(controller.state))
        notifications: list[str] = []
        diagnostics: list[str] = []
        controller._notify = lambda message, **_kwargs: notifications.append(message)
        controller._log_background = lambda message, _exc=None: diagnostics.append(message)
        retry_requests: list[bool] = []
        controller._schedule_recovery_sync_retry = lambda: retry_requests.append(True)

        from aqt import operations

        with patch.object(operations, "CollectionOp", _ImmediateCollectionOp):
            controller.undo_instant_regrade()

        self.assertTrue(
            controller.recovery_blocked,
            f"diagnostics={diagnostics!r}; state={controller.state!r}",
        )
        self.assertEqual(
            controller.state["regrade_recovery"]["status"], "compensation_failed"
        )
        self.assertEqual(retry_requests, [True])
        self.assertTrue(any("补偿失败" in message for message in diagnostics))
        self.assertEqual(self._revlog(card_id), [])

        self.col.close()
        self.col = Collection(str(Path(self.temp.name) / "collection.anki2"))
        restarted = BatchController.__new__(BatchController)
        restarted.state = copy.deepcopy(persisted["state"])
        restarted.mw = types.SimpleNamespace(col=self.col, reviewer=None)
        restarted.undo_in_progress = False
        restarted.recovery_blocked = True
        restarted.undo_notice = None
        restarted.undo_button = None
        restarted.shortcut_conflict = False
        restarted.recovery_sync_retry_count = 0
        restarted.config = lambda: {"instant_undo_shortcut": "Z"}
        restarted._set_review_shortcut_armed = lambda _armed: None
        restarted._save = lambda: persisted.update(state=copy.deepcopy(restarted.state))
        restarted._notify = lambda message, **_kwargs: notifications.append(message)
        restarted._schedule_recovery_sync_retry = lambda: retry_requests.append(True)
        restarted._row_by_id = lambda table, row_id: self._full_row(table, row_id)
        restarted._latest_revlog = lambda _card_id: (
            self._full_row("revlog", int(self.col.db.scalar("select id from revlog order by id desc limit 1")))
            if self.col.db.scalar("select id from revlog order by id desc limit 1")
            else None
        )

        with patch.object(operations, "CollectionOp", _ImmediateCollectionOp):
            restarted._recover_interrupted_regrade()

        rows = self._revlog(card_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ease"], 3)
        self.assertEqual(self.col.get_card(card_id).reps, 1)
        self.assertEqual(rows[0]["id"], int(original.answered_at_millis))
        self.assertEqual(rows[0]["time"], int(original.milliseconds_taken))
        self.assertNotIn("regrade_recovery", restarted.state)
        restored = RuntimeAttempt.from_dict(
            restarted.state["batches"][batch_key]["attempts"][0]
        )
        self.assertEqual(restored.attempt.original_ease, 3)
        self.assertFalse(restored.attempt.batch_auto_easy_eligible)
        self.assertTrue(restored.grade_reverted)
        self.assertEqual(restarted.state["timing_history"][0]["effective_ease"], 3)
        self.assertTrue(restarted.state["initial_new_learning"][str(card_id)])
        self.assertEqual(self.col.undo_status().undo, "Answer Card")
        self.col.undo()
        self.assertEqual(self.col.undo_status().undo, "")
        self.col.redo()
        self.assertEqual(self._revlog(card_id)[0]["ease"], 3)

    def test_card_and_review_log_snapshots_keep_native_undo_available(self) -> None:
        card, states = self._add_card()
        card_id = int(card.id)
        answer = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.HARD
        )
        self.col.sched.answer_card(answer)
        step = int(self.col.undo_status().last_step)

        self.assertIsNotNone(self._full_row("cards", card_id))
        self.assertIsNotNone(self._full_row("revlog", int(answer.answered_at_millis)))

        self.assertEqual(self.col.undo_status().undo, "Answer Card")
        self.assertEqual(int(self.col.undo_status().last_step), step)

    def test_later_card_change_makes_regrade_undo_stale(self) -> None:
        card, states = self._add_card()
        card_id = int(card.id)
        original_card = self._full_row("cards", card_id)
        answer = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.HARD
        )
        self.col.sched.answer_card(answer)
        post_card = self._full_row("cards", card_id)
        post_revlog = self._full_row("revlog", int(answer.answered_at_millis))
        entry = {
            "card_id": card_id,
            "effective_ease": 2,
            "reviewer_undo_step": int(self.col.undo_status().last_step),
            "post_card": post_card,
            "post_revlog": post_revlog,
        }
        controller = BatchController.__new__(BatchController)
        controller.mw = types.SimpleNamespace(col=self.col)

        self.assertTrue(controller._instant_undo_is_current(self.col, entry))
        changed = self.col.get_card(card_id)
        changed.flags = 1
        self.col.update_card(changed)
        self.assertFalse(controller._instant_undo_is_current(self.col, entry))

    def test_review_reads_preserve_the_older_native_undo_entry(self) -> None:
        card, states = self._add_card()
        card_id = int(card.id)
        controller = BatchController.__new__(BatchController)
        controller.mw = types.SimpleNamespace(col=self.col)

        self.assertIsNotNone(controller._row_by_id("cards", card_id))
        controller._pass_history(
            card_id, 2, current_card_type=int(card.type)
        )
        self.assertEqual(self.col.undo_status().undo, "Add Note")

        answer = self.col.sched.build_answer(
            card=card, states=states, rating=CardAnswer.HARD
        )
        self.col.sched.answer_card(answer)
        self.assertIsNotNone(controller._row_by_id("cards", card_id))
        self.assertIsNotNone(controller._latest_revlog(card_id))
        self.assertEqual(self.col.undo_status().undo, "Answer Card")

        self.col.undo()
        self.assertEqual(self.col.undo_status().undo, "Add Note")

    def test_timeout_decision_uses_real_new_and_review_scheduler_states(self) -> None:
        for review_card, expected in ((False, 2), (True, 1)):
            with self.subTest(review_card=review_card):
                card, states = self._add_card(review_card=review_card)
                actual = _timeout_grade(
                    states,
                    time.time(),
                    int(self.col.sched.day_cutoff),
                )
                self.assertEqual(actual, expected)

    def _answer_after_reveal_delay(
        self,
        *,
        question_ms: int,
        answer_ms: int,
        review_card: bool = False,
        timer_start: str = "answer",
        threshold_mode: str = "anki_max_taken",
        show_question: bool = True,
        show_answer: bool = True,
    ) -> dict[str, Any]:
        card, states = self._add_card(review_card=review_card)
        controller = self._batch_controller({
            "strategy": "fixed",
            "auto_hard_enabled": True,
            "auto_easy_timer_start": timer_start,
            "auto_hard_threshold_mode": threshold_mode,
            "auto_hard_threshold_ms": 60_000,
        })
        reviewer = types.SimpleNamespace(
            card=card, _v3=types.SimpleNamespace(states=states)
        )
        with patch(
            "anki_batch_auto_easy.integration.time.monotonic", return_value=1000.0
        ) as clock:
            if show_question:
                controller.did_show_question(card)
            clock.return_value = 1000.0 + question_ms / 1000
            if show_answer:
                controller.did_show_answer(card)
            clock.return_value += answer_ms / 1000
            card.timer_started = time.time() - (question_ms + answer_ms) / 1000
            controller.space_answer = (reviewer, card, object())
            proceed, ease = controller.will_answer((True, 3), reviewer, card)
        self.assertTrue(proceed)
        answer = self.col.sched.build_answer(
            card=card,
            states=states,
            rating={1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD}[ease],
        )
        self.col.sched.answer_card(answer)
        controller.did_answer(reviewer, card, ease)
        self.assertEqual(self._revlog(int(card.id))[-1]["ease"], ease)
        return controller.state["timing_history"][-1]

    def test_answer_screen_delay_does_not_trigger_slow_answer_protection(self) -> None:
        for review_card in (False, True):
            for timer_start in ("answer", "question"):
                for threshold_mode in ("anki_max_taken", "custom"):
                    for answer_ms in (110_664, 3_067_512):
                        with self.subTest(
                            review_card=review_card,
                            timer_start=timer_start,
                            threshold_mode=threshold_mode,
                            answer_ms=answer_ms,
                        ):
                            sample = self._answer_after_reveal_delay(
                                question_ms=30_000,
                                answer_ms=answer_ms,
                                review_card=review_card,
                                timer_start=timer_start,
                                threshold_mode=threshold_mode,
                            )
                            self.assertEqual(sample["effective_ease"], 3)
                            self.assertEqual(sample["question_to_answer_ms"], 30_000)
                            self.assertGreater(sample["question_to_grade_ms"], 60_000)
                            self.assertAlmostEqual(
                                sample["answer_to_grade_ms"], answer_ms, delta=1
                            )

    def test_slow_answer_threshold_uses_only_time_before_reveal(self) -> None:
        for review_card, timeout_ease in ((False, 2), (True, 1)):
            for question_ms in (59_999, 60_000, 61_000):
                with self.subTest(review_card=review_card, question_ms=question_ms):
                    sample = self._answer_after_reveal_delay(
                        question_ms=question_ms,
                        answer_ms=120_000,
                        review_card=review_card,
                    )
                    self.assertEqual(
                        sample["effective_ease"],
                        timeout_ease if question_ms >= 60_000 else 3,
                    )

    def test_missing_reveal_timing_keeps_the_original_grade(self) -> None:
        for show_question, show_answer in ((False, True), (True, False)):
            with self.subTest(show_question=show_question, show_answer=show_answer):
                sample = self._answer_after_reveal_delay(
                    question_ms=120_000,
                    answer_ms=120_000,
                    show_question=show_question,
                    show_answer=show_answer,
                )
                self.assertEqual(sample["effective_ease"], 3)
                self.assertIsNone(sample["question_to_answer_ms"])

    def test_space_and_manual_answers_use_real_scheduler_states(self) -> None:
        card, states = self._add_card()
        reviewer = types.SimpleNamespace(_v3=types.SimpleNamespace(states=states))
        for source, expected in (("space", 4), ("other", 3)):
            with self.subTest(source=source):
                controller = BatchController.__new__(BatchController)
                controller.mw = types.SimpleNamespace(col=self.col)
                controller.state = {"batches": {}, "daily": {}}
                controller.current_batch_key = None
                controller.review_session_id = None
                controller.config = lambda: dict(DEFAULT_CONFIG)
                runtime, effective = controller._capture_attempt(
                    reviewer,
                    card,
                    3,
                    100,
                    question_to_answer_ms=100,
                    max_answer_ms=60_000,
                    input_source=source,
                )
                self.assertEqual(effective, expected)
                self.assertEqual(runtime.input_source, source)
                self.assertEqual(runtime.instant_upgraded, source == "space")
                self.assertEqual(
                    runtime.attempt.batch_auto_easy_eligible, False
                )

    def test_three_rating_switches_are_independent(self) -> None:
        for plugin_enabled in (False, True):
            for auto_easy_enabled in (False, True):
                for auto_hard_enabled in (False, True):
                    for priority, preferred in (("hard", 2), ("easy", 4)):
                        with self.subTest(
                            plugin_enabled=plugin_enabled,
                            auto_easy_enabled=auto_easy_enabled,
                            auto_hard_enabled=auto_hard_enabled,
                            priority=priority,
                        ):
                            card, states = self._add_card()
                            controller = BatchController.__new__(BatchController)
                            controller.mw = types.SimpleNamespace(col=self.col)
                            controller.state = {"batches": {}, "daily": {}}
                            controller.current_batch_key = None
                            controller.review_session_id = None
                            controller.config = lambda: {
                                **DEFAULT_CONFIG,
                                "enabled": plugin_enabled,
                                "auto_easy_enabled": auto_easy_enabled,
                                "auto_hard_enabled": auto_hard_enabled,
                                "auto_hard_threshold_mode": "custom",
                                "auto_hard_threshold_ms": 1_000,
                                "auto_grade_priority": priority,
                            }

                            runtime, effective = controller._capture_attempt(
                                types.SimpleNamespace(
                                    _v3=types.SimpleNamespace(states=states)
                                ),
                                card,
                                3,
                                100,
                                question_to_answer_ms=1_000,
                                max_answer_ms=60_000,
                                input_source="space",
                            )

                            if not plugin_enabled or not (
                                auto_easy_enabled or auto_hard_enabled
                            ):
                                expected = 3
                            elif auto_easy_enabled and auto_hard_enabled:
                                expected = preferred
                            elif auto_easy_enabled:
                                expected = 4
                            else:
                                expected = 2

                            self.assertEqual(effective, expected)
                            expected_easy_candidate = (
                                plugin_enabled
                                and auto_easy_enabled
                                and (not auto_hard_enabled or priority == "easy")
                            )
                            self.assertEqual(
                                runtime.instant_candidate,
                                expected_easy_candidate,
                            )

    def test_space_selected_non_good_ratings_are_never_auto_changed(self) -> None:
        for ease in (1, 2, 4):
            with self.subTest(ease=ease):
                card, states = self._add_card()
                controller = BatchController.__new__(BatchController)
                controller.mw = types.SimpleNamespace(col=self.col)
                controller.state = {"batches": {}, "daily": {}}
                controller.current_batch_key = None
                controller.review_session_id = None
                controller.config = lambda: dict(DEFAULT_CONFIG)
                runtime, effective = controller._capture_attempt(
                    types.SimpleNamespace(_v3=types.SimpleNamespace(states=states)),
                    card,
                    ease,
                    100,
                    question_to_answer_ms=100,
                    max_answer_ms=60_000,
                    input_source="space",
                )

                self.assertEqual(effective, ease)
                self.assertFalse(runtime.instant_candidate)
                self.assertFalse(runtime.attempt.is_valid_candidate)

    def test_timeout_with_both_options_after_cutoff_uses_again(self) -> None:
        for review_card in (False, True):
            with self.subTest(review_card=review_card):
                card, states = self._add_card(review_card=review_card)
                now = time.time()
                cutoff = int(self.col.sched.day_cutoff)
                if review_card:
                    states.again.normal.relearning.learning.scheduled_secs = int(
                        cutoff - now + 60
                    )
                else:
                    states.hard.normal.learning.scheduled_secs = int(cutoff - now + 60)
                    states.again.normal.learning.scheduled_secs = int(cutoff - now + 60)
                self.assertEqual(_timeout_grade(states, now, cutoff), 1)

    def test_unknown_timeout_schedule_keeps_good_and_skips_batch(self) -> None:
        card, states = self._add_card()
        states.hard.Clear()
        controller = BatchController.__new__(BatchController)
        controller.mw = types.SimpleNamespace(col=self.col)
        controller.state = {"batches": {}, "daily": {}}
        controller.current_batch_key = None
        controller.review_session_id = None
        controller.config = lambda: {
            **DEFAULT_CONFIG,
            "auto_hard_enabled": True,
            "auto_hard_threshold_mode": "custom",
            "auto_hard_threshold_ms": 1_000,
        }
        runtime, effective = controller._capture_attempt(
            types.SimpleNamespace(_v3=types.SimpleNamespace(states=states)),
            card,
            3,
            100,
            question_to_answer_ms=1_000,
            max_answer_ms=60_000,
            input_source="space",
        )
        self.assertEqual(effective, 3)
        self.assertFalse(runtime.instant_candidate)
        self.assertFalse(runtime.attempt.batch_auto_easy_eligible)


@unittest.skipIf(BatchController is None, "set ANKI_RESOURCES to an Anki desktop runtime")
class AnkiQtUndoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from aqt.qt import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_shortcut_can_be_changed_or_cleared_and_protects_input_fields(self) -> None:
        from aqt.qt import QKeySequence, QKeySequenceEdit

        edit = QKeySequenceEdit(QKeySequence("Z"))
        edit.setMaximumSequenceLength(1)
        edit.setKeySequence(QKeySequence("Ctrl+Alt+U"))
        self.assertEqual(
            edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
            "Ctrl+Alt+U",
        )
        edit.clear()
        self.assertTrue(edit.keySequence().isEmpty())

        default_script = _review_shortcut_script("Z")
        changed_script = _review_shortcut_script("Ctrl+Alt+U")
        cleared_script = _review_shortcut_script("")
        self.assertIn('"key": "z"', default_script)
        self.assertIn('"modifiers": ["ctrl", "alt"]', changed_script)
        self.assertIn("input, textarea, select", default_script)
        self.assertIn("document.activeElement", default_script)
        self.assertIn("pycmd('batch-auto-easy-undo')", default_script)
        self.assertIn("delete window.__batchAutoEasyUndoKeyHandler", cleared_script)
        self.assertNotIn("pycmd('batch-auto-easy-undo')", cleared_script)

    def test_settings_show_three_independent_rating_switches(self) -> None:
        from aqt.qt import QAbstractButton, QCheckBox, QGroupBox, QLabel, QPushButton, QWidget
        from anki_batch_auto_easy.integration import SettingsDialog

        controller = types.SimpleNamespace(
            config=lambda: dict(DEFAULT_CONFIG),
            show_timing_history=lambda _parent: None,
        )
        parent = QWidget()
        settings = SettingsDialog(parent, controller)
        for switch, name in (
            (settings.enabled, "启用插件"),
            (settings.auto_easy_enabled, "自动变简单"),
            (settings.auto_hard_enabled, "自动再复习"),
        ):
            with self.subTest(switch=name):
                self.assertIsInstance(switch, QAbstractButton)
                self.assertNotIsInstance(switch, QCheckBox)
                self.assertEqual(switch.accessibleName(), name)
                self.assertEqual(switch.width(), 48)
                self.assertEqual(switch.height(), 28)
        labels = {label.text() for label in settings.dialog.findChildren(QLabel)}
        group_titles = {
            group.title() for group in settings.dialog.findChildren(QGroupBox)
        }

        self.assertIn("启用插件", labels)
        self.assertIn("自动变简单", labels)
        self.assertIn("自动再复习", labels)
        self.assertIn("自动再复习设置", group_titles)
        self.assertIn("自动变简单设置", group_titles)
        self.assertIn("两种评分都符合时", group_titles)
        self.assertEqual(
            settings.auto_hard_priority.text(), "自动再复习优先"
        )
        self.assertEqual(
            settings.auto_easy_priority.text(), "自动变简单优先"
        )
        self.assertEqual(
            settings.auto_hard_threshold_mode.itemText(0),
            "回答时间记录上限（秒）（默认）",
        )
        self.assertNotIn("超时处理", labels)
        self.assertNotIn("空格答题超时", labels)
        self.assertIn("牌组选项", settings.auto_hard_help.text())
        self.assertIn("回答时间记录上限（秒）", settings.auto_hard_help.text())
        self.assertNotIn("当前状态", labels)
        self.assertIn(
            "最近100张答题计时",
            {button.text() for button in settings.dialog.findChildren(QPushButton)},
        )
        self.assertTrue(settings.auto_easy_group.isEnabled())
        self.assertTrue(settings.auto_hard_group.isEnabled())

        settings.auto_hard_enabled.setChecked(True)
        self.assertTrue(settings.auto_grade_priority_box.isEnabled())
        settings.auto_easy_enabled.setChecked(False)
        self.assertFalse(settings.auto_easy_group.isEnabled())
        self.assertTrue(settings.auto_hard_group.isEnabled())
        self.assertFalse(settings.auto_grade_priority_box.isEnabled())
        settings.enabled.setChecked(False)
        self.assertFalse(settings.auto_hard_group.isEnabled())
        self.assertFalse(settings.enabled.isChecked())
        self.assertFalse(settings.auto_easy_enabled.isChecked())
        self.assertTrue(settings.auto_hard_enabled.isChecked())

        settings.dialog.show()
        self.app.processEvents()
        settings.dialog.hide()
        settings.dialog.close()

    def test_settings_refresh_shortcuts_on_the_main_window(self) -> None:
        from aqt.qt import QKeySequence
        from anki_batch_auto_easy.integration import SettingsDialog

        events: list[str] = []
        written_config: list[dict[str, Any]] = []

        class _Value:
            def __init__(self, value: Any) -> None:
                self._value = value

            def isChecked(self) -> bool:
                return bool(self._value)

            def currentData(self) -> Any:
                return self._value

            def value(self) -> Any:
                return self._value

            def keySequence(self) -> Any:
                return QKeySequence(str(self._value))

        reviewer = types.SimpleNamespace(
            _shortcutKeys=lambda: [(" ", lambda: None)],
            web=types.SimpleNamespace(eval=lambda script: events.append("script")),
        )
        main_window = types.SimpleNamespace(
            state="review",
            reviewer=reviewer,
            addonManager=types.SimpleNamespace(
                writeConfig=lambda _name, config: written_config.append(config)
            ),
            clearStateShortcuts=lambda: events.append("clear"),
            setStateShortcuts=lambda shortcuts: events.append("set"),
        )
        controller = types.SimpleNamespace(mw=main_window, shortcut_conflict=False)
        dialog = types.SimpleNamespace(
            controller=controller,
            enabled=_Value(True),
            auto_easy_enabled=_Value(False),
            auto_hard_enabled=_Value(False),
            auto_easy_timer_start=_Value("answer"),
            auto_hard_threshold_mode=_Value("anki_max_taken"),
            auto_hard_threshold=_Value(60),
            auto_hard_priority=_Value(True),
            instant_undo_shortcut=_Value("X"),
            strategy=_Value("fixed"),
            threshold=_Value(1),
            distribution=_Value(20),
            smart_minimum_count=_Value(5),
            smart_minimum_mode=_Value("percent"),
            smart_minimum_percent=_Value(20),
            history=_Value(2),
            _smart_mad_multiplier=2,
            quota_enabled=_Value(False),
            quota_percent=_Value(20),
            quota_scope=_Value("eligible_pool"),
            dialog=types.SimpleNamespace(accept=lambda: events.append("accept")),
        )

        SettingsDialog._save(dialog)

        self.assertFalse(written_config[0]["auto_easy_enabled"])
        self.assertEqual(written_config[0]["instant_undo_shortcut"], "X")
        self.assertLess(events.index("clear"), events.index("set"))
        self.assertIn("script", events)
        self.assertEqual(events[-1], "accept")

    def test_conflicting_shortcut_is_disabled_and_reported(self) -> None:
        controller = BatchController.__new__(BatchController)
        controller.config = lambda: {"instant_undo_shortcut": "Z"}
        controller._main_window_shortcuts = lambda: {"Z"}
        controller.shortcut_conflict = False
        controller.last_shortcut_conflict = None
        messages = []
        controller._notify = lambda message, **_kwargs: messages.append(message)
        scripts = []
        controller.mw = types.SimpleNamespace(
            reviewer=types.SimpleNamespace(
                web=types.SimpleNamespace(eval=scripts.append)
            )
        )

        controller.reviewer_shortcuts_will_change("review", [])

        self.assertTrue(controller.shortcut_conflict)
        self.assertIn("已被占用", messages[0])
        self.assertNotIn("pycmd('batch-auto-easy-undo')", scripts[-1])
        controller.config = lambda: {"instant_undo_shortcut": "X"}
        controller.reviewer_shortcuts_will_change("review", [])
        self.assertFalse(controller.shortcut_conflict)
        self.assertIn("pycmd('batch-auto-easy-undo')", scripts[-1])

    def test_shortcut_message_calls_the_same_regrade_action(self) -> None:
        from aqt import reviewer

        class _Reviewer:
            pass

        controller = BatchController.__new__(BatchController)
        calls: list[bool] = []
        controller.undo_instant_regrade = lambda: calls.append(True)

        with patch.object(reviewer, "Reviewer", _Reviewer):
            handled = controller.handle_regrade_shortcut(
                (False, "original"), "batch-auto-easy-undo", _Reviewer()
            )
            untouched = controller.handle_regrade_shortcut(
                (False, "original"), "other-message", _Reviewer()
            )

        self.assertEqual(handled, (True, None))
        self.assertEqual(untouched, (False, "original"))
        self.assertEqual(calls, [True])

    def test_popup_button_invokes_the_shared_undo_action(self) -> None:
        from aqt.qt import QWidget
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        controller = BatchController.__new__(BatchController)
        controller.mw = QWidget()
        controller.mw.setGeometry(0, 0, 800, 600)
        controller.undo_notice = None
        controller.undo_button = None
        controller._set_review_shortcut_armed = lambda _armed: None
        calls: list[bool] = []
        controller.undo_instant_regrade = lambda: calls.append(True)

        controller._show_instant_undo_notice(3, 2)
        self.app.processEvents()

        self.assertTrue(controller.undo_notice.isVisible())
        self.assertEqual(controller.undo_button.text(), "撤销改分")
        QTest.mouseClick(controller.undo_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(calls, [True])
        controller.undo_notice.close()
        controller.mw.close()

    def test_space_wrapper_only_replaces_the_native_space_binding(self) -> None:
        from aqt.qt import QMainWindow

        class _Reviewer:
            def __init__(self) -> None:
                self.enter_count = 0

            def onEnterKey(self) -> None:
                self.enter_count += 1

        reviewer = _Reviewer()
        manual_calls: list[bool] = []
        manual_action = lambda: manual_calls.append(True)
        shortcuts = [(" ", reviewer.onEnterKey), ("C", manual_action)]
        controller = BatchController.__new__(BatchController)
        controller.mw = QMainWindow()
        controller.mw.reviewer = reviewer
        controller.space_binding_warning = False
        controller._notify = lambda *_args, **_kwargs: None

        controller._install_space_shortcut(shortcuts)

        self.assertIsNot(shortcuts[0][1], reviewer.onEnterKey)
        self.assertIs(shortcuts[1][1], manual_action)
        shortcuts[1][1]()
        self.assertEqual(manual_calls, [True])
        self.assertEqual(reviewer.enter_count, 0)
        controller.mw.close()

    def test_anki_default_and_custom_rating_keys_remain_native(self) -> None:
        import aqt
        from aqt.reviewer import Reviewer
        from aqt.qt import QMainWindow

        def no_op(*_args: Any) -> None:
            return None

        profile_manager = types.SimpleNamespace(
            default_answer_keys=[1, 2, 3, 4],
            get_answer_key=lambda ease: {1: "1", 2: "C", 3: "3", 4: "4"}[ease],
        )
        main_window = QMainWindow()
        main_window.pm = profile_manager
        main_window.flags = types.SimpleNamespace(all=lambda: [])
        main_window.onEditCurrent = no_op
        main_window.undo = no_op
        reviewer = types.SimpleNamespace(
            mw=main_window,
            korean_shortcuts=lambda: [],
            set_flag_func=lambda _index: no_op,
        )
        for name in (
            "onEnterKey",
            "_answerCard",
            "showContextMenu",
            "replayAudio",
            "toggle_mark_on_current_note",
            "bury_current_note",
            "bury_current_card",
            "suspend_current_note",
            "suspend_current_card",
            "forget_current_card",
            "on_create_copy",
            "delete_current_note",
            "on_set_due",
            "onReplayRecorded",
            "onRecordVoice",
            "onOptions",
            "on_card_info",
            "on_previous_card_info",
            "on_pause_audio",
            "on_seek_backward",
            "on_seek_forward",
            "toggle_auto_advance",
        ):
            setattr(reviewer, name, no_op)

        with patch.object(aqt, "mw", main_window):
            shortcuts = list(Reviewer._shortcutKeys(reviewer))

        original_callbacks = {
            str(key): callback
            for key, callback in shortcuts
            if str(key) in {" ", "1", "C", "3", "4"}
        }
        controller = BatchController.__new__(BatchController)
        controller.mw = main_window
        controller.mw.reviewer = reviewer
        controller.space_binding_warning = False
        controller._notify = lambda *_args, **_kwargs: None
        controller._install_space_shortcut(shortcuts)
        resulting_callbacks = {str(key): callback for key, callback in shortcuts}

        self.assertIsNot(resulting_callbacks[" "], original_callbacks[" "])
        for key in ("1", "C", "3", "4"):
            self.assertIs(resulting_callbacks[key], original_callbacks[key])
        self.assertEqual(original_callbacks["C"].args, (2,))
        self.assertEqual(original_callbacks["1"].args, (1,))
        self.assertEqual(original_callbacks["3"].args, (3,))
        self.assertEqual(original_callbacks["4"].args, (4,))
        main_window.close()

    def test_space_before_answer_uses_anki_original_handler(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut

        card = object()
        reviewer = types.SimpleNamespace(
            state="question",
            card=card,
            onEnterKey=lambda: original_calls.append(True),
        )
        original_calls: list[bool] = []
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda *_args, **_kwargs: None
        shortcuts = [(" ", reviewer.onEnterKey)]

        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)
        shortcuts[0][1]()

        self.assertEqual(original_calls, [True])
        self.assertIsNone(controller.space_pending)
        mw.close()

    def test_space_answer_uses_selected_rating_and_marks_only_its_hook(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut

        card = object()
        callbacks: list[tuple[str, Any]] = []
        observed: list[tuple[str, str]] = []
        controller = BatchController.__new__(BatchController)
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda *_args, **_kwargs: None

        class _Reviewer:
            state = "answer"

            def __init__(self) -> None:
                self.card = card
                self.bottom = types.SimpleNamespace(
                    web=types.SimpleNamespace(
                        evalWithCallback=lambda script, callback: callbacks.append(
                            (script, callback)
                        )
                    )
                )

            def onEnterKey(self) -> None:
                raise AssertionError("space handler should be wrapped")

            def _onAnswerButton(self, value: Any) -> None:
                observed.append(
                    (str(value), controller._answer_input_source(self, self.card))
                )
                controller._clear_space_input()

        reviewer = _Reviewer()
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller.mw = mw
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)

        shortcuts[0][1]()
        self.assertEqual(callbacks[0][0], "selectedAnswerButton()")
        self.assertIsNotNone(controller.space_pending)
        callbacks[0][1]("3")

        self.assertEqual(observed, [("3", "space")])
        self.assertIsNone(controller.space_pending)
        self.assertIsNone(controller.space_answer)
        mw.close()

    def test_qt_space_key_event_uses_anki_shortcut_dispatch(self) -> None:
        import aqt
        from aqt.main import AnkiQt
        from aqt.qt import QMainWindow, Qt
        from PyQt6.QtTest import QTest

        callbacks: list[Any] = []
        observed: list[tuple[str, str]] = []
        controller = BatchController.__new__(BatchController)
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller.config = lambda: {"instant_undo_shortcut": ""}
        controller._update_review_shortcut_script = lambda: None
        controller._notify = lambda *_args, **_kwargs: None

        class _Reviewer:
            state = "answer"

            def __init__(self) -> None:
                self.card = object()
                self.bottom = types.SimpleNamespace(
                    web=types.SimpleNamespace(
                        evalWithCallback=lambda script, callback: callbacks.append(
                            (script, callback)
                        )
                    )
                )

            def onEnterKey(self) -> None:
                raise AssertionError("Anki space shortcut should be intercepted")

            def _onAnswerButton(self, value: Any) -> None:
                observed.append(
                    (str(value), controller._answer_input_source(self, self.card))
                )
                controller._clear_space_input()

        class _MainWindow(QMainWindow):
            _normalize_shortcuts = AnkiQt._normalize_shortcuts
            applyShortcuts = AnkiQt.applyShortcuts

        reviewer = _Reviewer()
        main_window = _MainWindow()
        main_window.state = "review"
        main_window.reviewer = reviewer
        main_window.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller.mw = main_window
        shortcuts = [(" ", reviewer.onEnterKey)]

        with patch.object(aqt, "mw", main_window):
            controller.reviewer_shortcuts_will_change("review", shortcuts)
            main_window.stateShortcuts = main_window.applyShortcuts(shortcuts)
        main_window.show()
        main_window.activateWindow()
        main_window.setFocus()
        self.app.processEvents()
        QTest.keyClick(main_window, Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(callbacks[0][0], "selectedAnswerButton()")
        callbacks[0][1]("3")

        self.assertEqual(observed, [("3", "space")])
        main_window.close()

    def test_duplicate_space_and_stale_callback_cannot_answer_another_card(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut

        callbacks: list[Any] = []
        answered: list[Any] = []
        reviewer = types.SimpleNamespace(state="answer", card=object())
        reviewer.bottom = types.SimpleNamespace(
            web=types.SimpleNamespace(
                evalWithCallback=lambda _script, callback: callbacks.append(callback)
            )
        )
        reviewer.onEnterKey = lambda: self.fail("pending space must not repeat")
        reviewer._onAnswerButton = answered.append
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda *_args, **_kwargs: None
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)

        shortcuts[0][1]()
        pending = controller.space_pending
        shortcuts[0][1]()
        self.assertIs(controller.space_pending, pending)
        self.assertEqual(len(callbacks), 1)
        reviewer.card = object()
        callbacks[0]("3")
        reviewer.card = object()
        shortcuts[0][1]()
        mw.reviewer = object()
        callbacks[1]("3")

        self.assertEqual(answered, [])
        self.assertIsNone(controller.space_pending)
        self.assertIsNone(controller.space_answer)
        mw.close()

    def test_expired_space_callback_is_ignored_and_explains_retry(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut, QTimer

        callbacks: list[Any] = []
        expirations: list[tuple[int, Any]] = []
        answered: list[Any] = []
        messages: list[str] = []
        reviewer = types.SimpleNamespace(state="answer", card=object())
        reviewer.bottom = types.SimpleNamespace(
            web=types.SimpleNamespace(
                evalWithCallback=lambda _script, callback: callbacks.append(callback)
            )
        )
        reviewer.onEnterKey = lambda: None
        reviewer._onAnswerButton = answered.append
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda message, **_kwargs: messages.append(message)
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)

        with patch.object(
            QTimer,
            "singleShot",
            side_effect=lambda delay, callback: expirations.append((delay, callback)),
        ):
            shortcuts[0][1]()
            self.assertEqual(expirations[0][0], 3000)
            expirations[0][1]()
            callbacks[0]("3")

        self.assertIsNone(controller.space_pending)
        self.assertEqual(answered, [])
        self.assertIn("请重试", messages[0])
        mw.close()

    def test_manual_answer_clears_pending_space_and_stays_non_space(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut

        callbacks: list[Any] = []
        reviewer = types.SimpleNamespace(state="answer", card=object())
        reviewer.bottom = types.SimpleNamespace(
            web=types.SimpleNamespace(
                evalWithCallback=lambda _script, callback: callbacks.append(callback)
            )
        )
        reviewer.onEnterKey = lambda: None
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller.finalizing = False
        controller.undo_in_progress = False
        controller.recovery_blocked = False
        controller.enabled = lambda: True
        controller._notify = lambda *_args, **_kwargs: None
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)

        shortcuts[0][1]()
        controller.enabled = lambda: False
        self.assertEqual(controller._answer_input_source(reviewer, reviewer.card), "other")
        controller.will_answer((True, 2), reviewer, reviewer.card)
        callbacks[0]("3")

        self.assertIsNone(controller.space_pending)
        self.assertIsNone(controller.space_answer)
        mw.close()

    def test_space_conflict_preserves_native_handler_and_warns(self) -> None:
        from aqt.qt import QAction, QKeySequence, QMainWindow, QShortcut

        calls: list[bool] = []
        reviewer = types.SimpleNamespace(
            state="answer",
            card=object(),
            onEnterKey=lambda: calls.append(True),
        )
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: True)
        QShortcut(QKeySequence("Space"), mw)
        action = QAction("conflict", mw)
        action.setShortcut(QKeySequence("Space"))
        action.setVisible(True)
        mw.addAction(action)
        messages: list[str] = []
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda message, **_kwargs: messages.append(message)
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)

        shortcuts[0][1]()

        self.assertEqual(calls, [True])
        self.assertTrue(messages)
        self.assertIn("无法安全识别空格答题", messages[0])
        self.assertIsNone(controller.space_pending)
        mw.close()

    def test_space_setting_disabled_keeps_native_behavior(self) -> None:
        from aqt.qt import QKeySequence, QMainWindow, QShortcut

        calls: list[bool] = []
        reviewer = types.SimpleNamespace(
            state="answer",
            card=object(),
            onEnterKey=lambda: calls.append(True),
        )
        mw = QMainWindow()
        mw.state = "review"
        mw.reviewer = reviewer
        mw.pm = types.SimpleNamespace(spacebar_rates_card=lambda: False)
        controller = BatchController.__new__(BatchController)
        controller.mw = mw
        controller.space_pending = None
        controller.space_answer = None
        controller.space_binding_warning = False
        controller.enabled = lambda: True
        controller._notify = lambda *_args, **_kwargs: None
        shortcuts = [(" ", reviewer.onEnterKey)]
        controller._install_space_shortcut(shortcuts)
        QShortcut(QKeySequence("Space"), mw)

        shortcuts[0][1]()

        self.assertEqual(calls, [True])
        self.assertIsNone(controller.space_pending)
        mw.close()


if __name__ == "__main__":
    unittest.main()
