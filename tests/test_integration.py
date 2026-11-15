import ast
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from anki_batch_auto_easy.adapter import _already_replaced
from anki_batch_auto_easy.core import history_allows
from anki_batch_auto_easy.i18n import _anki_language_code, tr
from anki_batch_auto_easy import i18n as addon_i18n
from anki_batch_auto_easy.integration import (
    BatchController,
    DEFAULT_CONFIG,
    RuntimeAttempt,
    SettingsDialog,
    _auto_hard_threshold_ms,
    _answer_record_matches,
    _completed_regrade_summary,
    _is_unrescheduled_preview,
    _leaves_today_learning,
    _resolve_auto_grade,
    _scheduled_today,
    _timeout_grade,
    _state_path,
    _same_card_state,
    _timing_history_summary,
    _timing_sample,
)
from anki_batch_auto_easy.models import Attempt, DailyStats, RuleConfig


_original_anki_language_code = addon_i18n._anki_language_code


def setUpModule() -> None:
    # These assertions intentionally cover the Chinese UI strings regardless
    # of the language configured in the developer's Anki profile.
    addon_i18n._anki_language_code = lambda: "zh_CN"


def tearDownModule() -> None:
    addon_i18n._anki_language_code = _original_anki_language_code


class _NormalState:
    def __init__(self, kind: str, scheduled_secs: int = 0):
        self._kind = kind
        self.learning = type("Learning", (), {"scheduled_secs": scheduled_secs})()
        self.relearning = type(
            "Relearning",
            (),
            {
                "learning": type(
                    "Learning", (), {"scheduled_secs": scheduled_secs}
                )()
            },
        )()
        self.review = SimpleNamespace(scheduled_days=1)

    def WhichOneof(self, name: str) -> str:
        return self._kind


class _SchedulingState:
    def __init__(self, kind: str = "normal", normal_kind: str = "review"):
        self._kind = kind
        self.normal = _NormalState(normal_kind, scheduled_secs=24 * 60 * 60)
        self.filtered = type("Filtered", (), {})()

    def WhichOneof(self, name: str) -> str:
        return self._kind


class IntegrationTests(unittest.TestCase):
    def test_manifest_allows_anki_21_and_newer(self):
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "anki_batch_auto_easy"
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["min_point_version"], 0)
        self.assertNotIn("max_point_version", manifest)

    def test_selected_diagnostics_are_logged_instead_of_notified(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "anki_batch_auto_easy"
            / "integration.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        targets = (
            "评分已调整，但撤销改分暂不可用",
            "已完成评分，但插件记录保存失败",
            "评分已恢复，但插件记录待同步",
            "无法安全结束上次撤销状态",
            "这次改分已不能撤销",
            "卡片或复习记录已变化，无法安全撤销",
            "撤销记录保存失败，未更改评分",
            "恢复日志保存失败",
            "撤销补偿失败，插件已暂停自动处理",
            "撤销后的恢复状态无法确认",
            "撤销失败，原评分已保留",
            "撤销失败且恢复记录保存失败",
            "撤销后的复习记录同步失败",
            "本轮自动改分未完成",
        )
        routed: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            target = next((value for value in targets if node.value.startswith(value)), None)
            if target is None:
                continue
            current = node
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Call)
                    and isinstance(current.func, ast.Attribute)
                    and current.func.attr in {"_notify", "_log_background"}
                ):
                    routed[target] = current.func.attr
                    break

        self.assertEqual(set(routed), set(targets))
        self.assertTrue(all(destination == "_log_background" for destination in routed.values()))

    def test_background_diagnostics_use_anki_addon_logger(self):
        logger = MagicMock()
        addon_manager = SimpleNamespace(get_logger=MagicMock(return_value=logger))
        aqt_module = types.ModuleType("aqt")
        addons_module = types.ModuleType("aqt.addons")
        addons_module.AddonManager = addon_manager
        error = RuntimeError("state write failed")

        with patch.dict(
            sys.modules,
            {"aqt": aqt_module, "aqt.addons": addons_module},
        ):
            controller = object.__new__(BatchController)
            controller._log_background("diagnostic detail", error)

        addon_manager.get_logger.assert_called_once_with("anki_batch_auto_easy")
        logger.log.assert_called_once()
        args, kwargs = logger.log.call_args
        self.assertEqual(args[:3], (logging.ERROR, "%s", "diagnostic detail"))
        self.assertEqual(kwargs["exc_info"], (RuntimeError, error, error.__traceback__))

    def test_tools_menu_entry_opens_settings_directly(self):
        class Signal:
            def connect(self, callback):
                self.callback = callback

            def emit(self):
                self.callback()

        class Action:
            def __init__(self, text, _parent):
                self.text = text
                self.triggered = Signal()

        class ToolsMenu:
            def __init__(self):
                self.actions = []

            def addAction(self, action):
                self.actions.append(action)

        hook_names = (
            "reviewer_did_show_question",
            "reviewer_did_show_answer",
            "reviewer_will_answer_card",
            "reviewer_did_answer_card",
            "reviewer_will_end",
            "state_shortcuts_will_change",
            "state_did_undo",
            "operation_did_execute",
            "profile_will_close",
            "profile_did_open",
            "webview_will_set_content",
            "webview_did_receive_js_message",
        )
        gui_hooks = SimpleNamespace(**{name: [] for name in hook_names})
        tools_menu = ToolsMenu()
        mw = SimpleNamespace(
            form=SimpleNamespace(menuTools=tools_menu),
            _anki_batch_auto_easy_initialized=False,
        )
        aqt_module = types.ModuleType("aqt")
        aqt_module.__path__ = []
        aqt_module.gui_hooks = gui_hooks
        aqt_module.mw = mw
        aqt_module.appVersion = "26.9.4"
        qt_module = types.ModuleType("aqt.qt")
        qt_module.QAction = Action
        controller = SimpleNamespace(
            **{name: MagicMock() for name in (
                "did_show_question",
                "did_show_answer",
                "will_answer",
                "did_answer",
                "will_end",
                "reviewer_shortcuts_will_change",
                "native_undo_occurred",
                "operation_did_execute",
                "profile_will_close",
                "profile_did_open",
                "handle_regrade_shortcut",
            )}
        )
        dialog = SimpleNamespace(show=MagicMock())
        dialog_factory = MagicMock(return_value=dialog)

        with (
            patch.dict(sys.modules, {"aqt": aqt_module, "aqt.qt": qt_module}),
            patch("anki_batch_auto_easy.integration.BatchController", return_value=controller),
            patch("anki_batch_auto_easy.integration.SettingsDialog", dialog_factory),
        ):
            from anki_batch_auto_easy.integration import initialize

            initialize()
            self.assertEqual(len(tools_menu.actions), 1)
            action = tools_menu.actions[0]
            self.assertIn(action.text, {"一阅（Oneki reviewer）", "Oneki reviewer"})
            action.triggered.emit()

        dialog_factory.assert_called_once_with(mw, controller)
        dialog.show.assert_called_once_with()

    def test_ui_language_follows_anki_and_defaults_other_languages_to_english(self):
        translations = ("中文", "English")
        for language, expected in (
            ("zh_CN", "中文"),
            ("en_US", "English"),
            ("ja_JP", "English"),
        ):
            with self.subTest(language=language):
                with patch(
                    "anki_batch_auto_easy.i18n._anki_language_code",
                    return_value=language,
                ):
                    self.assertEqual(tr(*translations), expected)

    def test_language_code_comes_from_active_anki_profile(self):
        profile_manager = SimpleNamespace(meta={"defaultLang": "zh_TW"})
        fake_aqt = SimpleNamespace(
            mw=SimpleNamespace(pm=profile_manager)
        )
        with patch.dict("sys.modules", {"aqt": fake_aqt}):
            self.assertEqual(_anki_language_code(), "zh_TW")

    def test_completed_summary_uses_actual_scores_and_distinct_cards(self):
        def record(card_id, grade, *, user_ease=3, **flags):
            return RuntimeAttempt(
                attempt=Attempt(
                    attempt_id=f"{card_id}:{grade}",
                    card_id=card_id,
                    deck_id=1,
                    sequence=0,
                    original_ease=grade,
                    elapsed_ms=100,
                    prior_ratings=(),
                    leaves_today_learning=True,
                    answered_at_ms=1,
                ),
                original_card={},
                post_card={},
                original_revlog={"ease": grade},
                easy_answer_b64="",
                user_ease=user_ease,
                **flags,
            )

        overridden = record(8, 4)
        overridden.original_revlog = {"ease": 3}
        summary = _completed_regrade_summary(
            [
                record(1, 4),
                record(2, 2),
                record(2, 2),
                record(3, 1),
                record(4, 4, user_ease=4),
                record(5, 4, review_undone=True),
                record(6, 4, grade_reverted=True),
                record(7, 3),
                overridden,
            ]
        )

        self.assertEqual(
            summary,
            "本轮复习汇总：答题时已自动按「简单」评分 1 张、按「困难」评分 1 张、按「重来」评分 1 张。",
        )

    def test_invalid_config_disables_automatic_grading(self):
        invalid_configs = [
            {"enabled": False, "strategy": "invalid"},
            {"enabled": True, "threshold_ms": -1},
            {"enabled": "false"},
            [],
        ]
        for config in invalid_configs:
            with self.subTest(config=config):
                controller = BatchController.__new__(BatchController)
                controller.mw = SimpleNamespace(
                    addonManager=SimpleNamespace(getConfig=lambda _package: config)
                )
                self.assertFalse(controller.enabled())

    def test_valid_config_keeps_user_settings(self):
        config = {
            "enabled": False,
            "threshold_ms": 2500,
            "quota_enabled": True,
            "quota_percent": 0.1,
        }
        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            addonManager=SimpleNamespace(getConfig=lambda _package: config)
        )
        self.assertEqual(controller.config(), {**DEFAULT_CONFIG, **config})
        self.assertEqual(config["threshold_ms"], 2500)

    def test_missing_config_keeps_initial_defaults(self):
        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            addonManager=SimpleNamespace(getConfig=lambda _package: None)
        )
        self.assertEqual(controller.config(), DEFAULT_CONFIG)

    def test_timing_summary_excludes_natively_undone_answers(self):
        summary = _timing_history_summary(
            [
                {"effective_ease": 3},
                {"effective_ease": 4, "review_undone": True},
            ]
        )
        self.assertIn("最近记录 1 张", summary)
        self.assertIn("最终简单 0 张", summary)

    def test_auto_easy_timer_starts_when_answer_is_revealed(self):
        controller = BatchController.__new__(BatchController)
        controller.answer_timer_started = {}
        controller.question_timer_started = {}
        controller.answer_revealed_timer_started = {}
        controller.pending_by_card = {}
        controller.pending_attempts = {}
        controller.pending_timing_samples = {}
        controller.finalizing = False
        controller.config = lambda: {
            "enabled": True,
            "auto_easy_timer_start": "answer",
        }
        captured_elapsed = {}

        def capture_attempt(_reviewer, _card, ease, elapsed_ms, **kwargs):
            captured_elapsed["elapsed_ms"] = elapsed_ms
            captured_elapsed.update(kwargs)
            return (
                SimpleNamespace(attempt=SimpleNamespace(attempt_id="attempt-42")),
                ease,
            )

        controller._capture_attempt = capture_attempt
        card = SimpleNamespace(
            id=42,
            did=9,
            time_taken=lambda capped=False: 61_000,
            time_limit=lambda: 60_000,
        )

        with patch(
            "anki_batch_auto_easy.integration.time.monotonic",
            side_effect=(11.0, 12.5, 13.5),
        ):
            controller.did_show_question(card)
            controller.did_show_answer(card)
            result = controller.will_answer((True, 3), object(), card)

        self.assertEqual(captured_elapsed["elapsed_ms"], 1000)
        self.assertEqual(captured_elapsed["question_to_answer_ms"], 1500)
        self.assertEqual(captured_elapsed["max_answer_ms"], 60_000)
        self.assertEqual(captured_elapsed["input_source"], "other")
        sample = controller.pending_timing_samples[42]
        self.assertEqual(sample["question_to_answer_ms"], 1500)
        self.assertEqual(sample["answer_to_grade_ms"], 1000)
        self.assertTrue(sample["hit_max_answer_time"])
        self.assertEqual(result, (True, 3))
        self.assertNotIn(42, controller.answer_timer_started)

    def test_auto_easy_timer_can_start_when_question_is_shown(self):
        controller = BatchController.__new__(BatchController)
        controller.answer_timer_started = {}
        controller.question_timer_started = {}
        controller.answer_revealed_timer_started = {}
        controller.config = lambda: {
            "enabled": True,
            "auto_easy_timer_start": "question",
        }
        card = SimpleNamespace(id=42)

        with patch(
            "anki_batch_auto_easy.integration.time.monotonic",
            return_value=12.5,
        ):
            controller.did_show_question(card)
            controller.did_show_answer(card)

        self.assertEqual(controller.answer_timer_started[42], 12.5)

    def test_question_timer_stops_at_first_reveal_and_resets_on_next_question(self):
        controller = BatchController.__new__(BatchController)
        controller.answer_timer_started = {}
        controller.question_timer_started = {}
        controller.answer_revealed_timer_started = {}
        controller.config = lambda: dict(DEFAULT_CONFIG)
        card = SimpleNamespace(id=42)

        with patch(
            "anki_batch_auto_easy.integration.time.monotonic",
            side_effect=(10.0, 40.0, 100.0, 110.0, 120.0),
        ):
            controller.did_show_question(card)
            controller.did_show_answer(card)
            controller.did_show_answer(card)
            self.assertEqual(controller.answer_revealed_timer_started[42], 40.0)
            controller.did_show_question(card)
            self.assertNotIn(42, controller.answer_revealed_timer_started)
            controller.did_show_answer(card)

        self.assertEqual(controller.question_timer_started[42], 110.0)
        self.assertEqual(controller.answer_revealed_timer_started[42], 120.0)

    def test_timing_sample_records_three_durations_and_total_time_limit_hit(self):
        sample = _timing_sample(
            answered_at_ms=100,
            card_id=42,
            deck_id=5,
            user_ease=3,
            effective_ease=4,
            question_elapsed_ms=61_000,
            question_to_answer_ms=59_750,
            answer_elapsed_ms=1_250,
            max_answer_ms=60_000,
        )

        self.assertEqual(sample["question_to_grade_ms"], 61_000)
        self.assertEqual(sample["question_to_answer_ms"], 59_750)
        self.assertEqual(sample["answer_to_grade_ms"], 1_250)
        self.assertEqual(sample["max_answer_ms"], 60_000)
        self.assertTrue(sample["hit_max_answer_time"])
        summary = _timing_history_summary([sample])
        self.assertIn("最终简单 1 张", summary)
        self.assertIn("触及最大答题秒数 1 张", summary)

    def test_timing_history_keeps_only_the_recent_100_samples(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {"timing_history": []}
        controller.state_store = SimpleNamespace(save=lambda _state: None)

        for card_id in range(105):
            controller._append_timing_sample({"card_id": card_id})

        self.assertEqual(len(controller.state["timing_history"]), 100)
        self.assertEqual(controller.state["timing_history"][0]["card_id"], 5)
        self.assertEqual(controller.state["timing_history"][-1]["card_id"], 104)

    def test_answer_hook_persists_timing_sample_even_without_batch_attempt(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {"timing_history": []}
        controller.state_store = SimpleNamespace(save=lambda _state: None)
        controller.pending_timing_samples = {
            42: {
                "card_id": 42,
                "effective_ease": 3,
            }
        }
        controller.pending_by_card = {}
        controller.enabled = lambda: True

        controller.did_answer(None, SimpleNamespace(id=42), 4)

        self.assertEqual(len(controller.state["timing_history"]), 1)
        self.assertEqual(
            controller.state["timing_history"][0]["effective_ease"], 4
        )

    def test_timer_and_precedence_defaults(self):
        self.assertTrue(DEFAULT_CONFIG["auto_easy_enabled"])
        self.assertTrue(DEFAULT_CONFIG["auto_hard_enabled"])
        self.assertEqual(DEFAULT_CONFIG["auto_easy_timer_start"], "answer")
        self.assertEqual(DEFAULT_CONFIG["auto_hard_threshold_mode"], "anki_max_taken")
        self.assertEqual(DEFAULT_CONFIG["auto_grade_priority"], "hard")
        self.assertEqual(DEFAULT_CONFIG["instant_undo_shortcut"], "Z")

    def test_auto_easy_switch_defaults_on_for_legacy_config(self):
        controller = BatchController.__new__(BatchController)
        stored_config = {}
        controller.mw = SimpleNamespace(
            addonManager=SimpleNamespace(
                getConfig=lambda _name: dict(stored_config),
            ),
        )

        self.assertTrue(controller.config()["auto_easy_enabled"])

        stored_config["auto_easy_enabled"] = False
        self.assertFalse(controller.config()["auto_easy_enabled"])

    def test_feature_settings_enable_only_the_selected_groups(self):
        class _Switch:
            def __init__(self, checked):
                self.checked = checked

            def isChecked(self):
                return self.checked

        class _Panel:
            def __init__(self):
                self.enabled = None

            def setEnabled(self, enabled):
                self.enabled = bool(enabled)

        dialog = SettingsDialog.__new__(SettingsDialog)
        dialog.auto_easy_group = _Panel()
        dialog.auto_hard_group = _Panel()
        dialog.auto_easy_options = _Panel()
        dialog.auto_hard_options = _Panel()
        dialog.auto_grade_priority_box = _Panel()

        for plugin_enabled in (False, True):
            for auto_easy_enabled in (False, True):
                for auto_hard_enabled in (False, True):
                    with self.subTest(
                        plugin_enabled=plugin_enabled,
                        auto_easy_enabled=auto_easy_enabled,
                        auto_hard_enabled=auto_hard_enabled,
                    ):
                        dialog.enabled = _Switch(plugin_enabled)
                        dialog.auto_easy_enabled = _Switch(auto_easy_enabled)
                        dialog.auto_hard_enabled = _Switch(auto_hard_enabled)

                        SettingsDialog._update_feature_visibility(dialog)

                        self.assertEqual(
                            dialog.auto_easy_group.enabled,
                            plugin_enabled,
                        )
                        self.assertEqual(
                            dialog.auto_hard_group.enabled,
                            plugin_enabled,
                        )
                        self.assertEqual(
                            dialog.auto_easy_options.enabled,
                            plugin_enabled and auto_easy_enabled,
                        )
                        self.assertEqual(
                            dialog.auto_hard_options.enabled,
                            plugin_enabled and auto_hard_enabled,
                        )
                        self.assertEqual(
                            dialog.auto_grade_priority_box.enabled,
                            plugin_enabled
                            and auto_easy_enabled
                            and auto_hard_enabled,
                        )

    def test_finalize_when_auto_easy_is_off_skips_batch_regrade_and_keeps_stats(self):
        day = "2026-11-13"
        batch_key = f"{day}:10:0"

        def runtime_attempt(
            attempt_id,
            card_id,
            *,
            ease=3,
            batch_candidate=False,
            instant_candidate=False,
            instant_upgraded=False,
        ):
            attempt = Attempt(
                attempt_id=attempt_id,
                card_id=card_id,
                deck_id=10,
                sequence=card_id,
                original_ease=ease,
                elapsed_ms=500,
                prior_ratings=(),
                leaves_today_learning=True,
                answered_at_ms=1,
                batch_auto_easy_eligible=batch_candidate,
            )
            return RuntimeAttempt(
                attempt=attempt,
                original_card={},
                post_card={"id": card_id},
                original_revlog={"id": card_id + 100},
                easy_answer_b64="",
                instant_candidate=instant_candidate,
                instant_upgraded=instant_upgraded,
                input_source="space",
            ).to_dict()

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(col=SimpleNamespace(db=object()))
        controller._row_by_id = lambda table, row_id: (
            {"id": row_id} if table == "revlog" else None
        )
        controller.state_store = SimpleNamespace(save=lambda _state: None)
        controller.state = {
            "daily": {},
            "batches": {
                batch_key: {
                    "day": day,
                    "status": "open",
                    "attempts": [
                        runtime_attempt(
                            "batch-candidate", 1, batch_candidate=True
                        ),
                        runtime_attempt(
                            "instant-upgrade",
                            2,
                            ease=4,
                            instant_candidate=True,
                            instant_upgraded=True,
                        ),
                    ],
                }
            },
        }
        controller.config = lambda: {
            **DEFAULT_CONFIG,
            "auto_easy_enabled": False,
        }
        controller.rule_config = lambda: RuleConfig()
        controller._notify = lambda *_args, **_kwargs: None
        replaced_records = []

        def replace_many(_adapter, records):
            replaced_records.extend(records)
            return set()

        with patch(
            "anki_batch_auto_easy.integration.AnkiReplacementAdapter.replace_many",
            new=replace_many,
        ):
            controller.finalize(batch_key)

        batch = controller.state["batches"][batch_key]
        self.assertEqual(batch["status"], "finalized", batch.get("last_error"))
        self.assertEqual(replaced_records, [])
        self.assertEqual(batch["decision"]["selected"], ["instant-upgrade"])
        self.assertIn("batch-candidate", batch["decision"]["candidates"])
        self.assertEqual(controller.state["daily"][day]["reviewed_total"], 2)
        self.assertEqual(controller.state["daily"][day]["eligible_total"], 2)
        self.assertEqual(controller.state["daily"][day]["auto_upgraded"], 1)

    def test_answer_notification_only_when_grade_changes(self):
        for final_grade in (1, 2, 3, 4):
            with self.subTest(final_grade=final_grade):
                controller = BatchController.__new__(BatchController)
                controller.pending_timing_samples = {
                    42: {"user_ease": 3, "effective_ease": final_grade}
                }
                controller.pending_by_card = {}
                controller.enabled = lambda: True
                controller._append_timing_sample = lambda sample: None
                messages = []
                controller._notify = messages.append

                controller.did_answer(None, SimpleNamespace(id=42), final_grade)

                expected = (
                    [f"自动调整评分：良好 → { {1: '重来', 2: '困难', 4: '简单'}[final_grade]}"]
                    if final_grade != 3 else []
                )
                self.assertEqual(messages, expected)

    def test_auto_hard_uses_anki_max_or_custom_threshold(self):
        self.assertEqual(
            _auto_hard_threshold_ms(
                {"auto_hard_threshold_mode": "anki_max_taken"},
                anki_max_taken_ms=60_000,
            ),
            60_000,
        )
        self.assertEqual(
            _auto_hard_threshold_ms(
                {
                    "auto_hard_threshold_mode": "custom",
                    "auto_hard_threshold_ms": 15_000,
                },
                anki_max_taken_ms=60_000,
            ),
            15_000,
        )

    def test_auto_grade_priority_resolves_simultaneous_candidates(self):
        self.assertEqual(
            _resolve_auto_grade(
                3,
                auto_hard_candidate=2,
                auto_easy_candidate=True,
                priority="hard",
            ),
            2,
        )
        self.assertEqual(
            _resolve_auto_grade(
                3,
                auto_hard_candidate=2,
                auto_easy_candidate=True,
                priority="easy",
            ),
            4,
        )

    def test_timeout_uses_day_cutoff_and_closest_rating(self):
        state = _SchedulingState(normal_kind="learning")
        state.normal.learning.scheduled_secs = 59
        self.assertTrue(_scheduled_today(state, 100, 160))
        states = SimpleNamespace(hard=state, again=state)
        self.assertEqual(_timeout_grade(states, 100, 160), 2)
        state.normal.learning.scheduled_secs = 60
        self.assertFalse(_scheduled_today(state, 100, 160))
        self.assertEqual(_timeout_grade(states, 100, 160), 1)
        again = _SchedulingState(normal_kind="relearning")
        again.normal.relearning.learning.scheduled_secs = 30
        self.assertEqual(
            _timeout_grade(SimpleNamespace(hard=state, again=again), 100, 160), 1
        )
        self.assertFalse(_scheduled_today(_SchedulingState(), 100, 160))
        self.assertEqual(
            _resolve_auto_grade(
                3, auto_hard_candidate=1, auto_easy_candidate=False, priority="hard"
            ),
            1,
        )

    def test_timeout_checks_hard_then_again_and_respects_exact_cutoff(self):
        hard_today = _SchedulingState(normal_kind="learning")
        hard_today.normal.learning.scheduled_secs = 10
        again_today = _SchedulingState(normal_kind="relearning")
        again_today.normal.relearning.learning.scheduled_secs = 30
        self.assertEqual(
            _timeout_grade(
                SimpleNamespace(hard=hard_today, again=again_today), 100, 160
            ),
            2,
        )

        hard_tomorrow = _SchedulingState(normal_kind="review")
        self.assertEqual(
            _timeout_grade(
                SimpleNamespace(hard=hard_tomorrow, again=again_today), 100, 160
            ),
            1,
        )

        hard_at_cutoff = _SchedulingState(normal_kind="learning")
        hard_at_cutoff.normal.learning.scheduled_secs = 60
        again_at_cutoff = _SchedulingState(normal_kind="relearning")
        again_at_cutoff.normal.relearning.learning.scheduled_secs = 60
        self.assertEqual(
            _timeout_grade(
                SimpleNamespace(hard=hard_at_cutoff, again=again_at_cutoff),
                100,
                160,
            ),
            1,
        )
        self.assertIsNone(_scheduled_today(hard_today, 160, 160))

    def test_timeout_rejects_unknown_or_preview_schedules(self):
        hard_tomorrow = _SchedulingState(normal_kind="review")
        unknown = _SchedulingState(kind="unknown")
        self.assertIsNone(
            _timeout_grade(SimpleNamespace(hard=unknown, again=hard_tomorrow), 100, 160)
        )
        self.assertIsNone(
            _timeout_grade(SimpleNamespace(hard=hard_tomorrow, again=unknown), 100, 160)
        )
        self.assertIsNone(_scheduled_today(unknown, 100, 160))
        self.assertIsNone(_scheduled_today(None, 100, 160))
        preview = _SchedulingState(kind="filtered")
        preview.filtered.WhichOneof = lambda _name: "preview"
        self.assertIsNone(_scheduled_today(preview, 100, 160))

    def test_unknown_and_non_space_attempts_cannot_enter_batch_candidates(self):
        attempt = Attempt(
            attempt_id="batch:0:42",
            card_id=42,
            deck_id=9,
            sequence=0,
            original_ease=3,
            elapsed_ms=600,
            prior_ratings=(),
            leaves_today_learning=True,
            answered_at_ms=123,
        )
        runtime = RuntimeAttempt(
            attempt=attempt,
            original_card={},
            post_card=None,
            original_revlog=None,
            easy_answer_b64="",
            input_source="space",
        )
        serialized = runtime.to_dict()
        legacy = dict(serialized)
        legacy.pop("input_source")
        manual = {**serialized, "input_source": "other"}

        self.assertTrue(RuntimeAttempt.from_dict(serialized).attempt.is_valid_candidate)
        self.assertFalse(RuntimeAttempt.from_dict(legacy).attempt.is_valid_candidate)
        self.assertFalse(RuntimeAttempt.from_dict(manual).attempt.is_valid_candidate)
        self.assertTrue(legacy["attempt"]["batch_auto_easy_eligible"])

    def test_regrade_recovery_matches_the_replayed_answer_record(self):
        answer = SimpleNamespace(answered_at_millis=1000, milliseconds_taken=250)
        row = {"id": 1000, "ease": 3, "time": 250, "cid": 7}
        self.assertTrue(_answer_record_matches(row, answer, 3))
        self.assertFalse(_answer_record_matches({**row, "id": 1001}, answer, 3))
        self.assertFalse(_answer_record_matches({**row, "ease": 4}, answer, 3))
        self.assertFalse(_answer_record_matches({**row, "time": 251}, answer, 3))
        self.assertFalse(_answer_record_matches(None, answer, 3))

    def test_card_state_check_rejects_any_changed_field(self):
        expected = {"did": 1, "type": 2, "queue": 2, "reps": 4, "mod": 9}
        self.assertTrue(_same_card_state(dict(expected), expected))
        self.assertFalse(_same_card_state({**expected, "mod": 10}, expected))

    def test_regrade_reverted_attempt_is_removed_from_upgrade_stats(self):
        attempt = Attempt(
            attempt_id="batch:0:42",
            card_id=42,
            deck_id=9,
            sequence=0,
            original_ease=4,
            elapsed_ms=600,
            prior_ratings=(),
            leaves_today_learning=True,
            answered_at_ms=123,
            batch_auto_easy_eligible=True,
        )
        runtime = RuntimeAttempt(
            attempt=attempt,
            original_card={"type": 0},
            post_card={"type": 1},
            original_revlog={"id": 456, "ease": 4},
            easy_answer_b64="easy",
            initial_new_learning=True,
            instant_candidate=True,
            instant_upgraded=True,
            user_ease=3,
            original_answer_b64="good",
        )
        serialized = runtime.to_dict()
        day = "2026-11-13"
        controller = BatchController.__new__(BatchController)
        controller.state = {
            "batches": {
                "batch": {
                    "day": day,
                    "status": "open",
                    "daily_counted": False,
                    "attempts": [serialized],
                }
            },
            "timing_history": [
                {"attempt_id": "batch:0:42", "effective_ease": 4}
            ],
            "initial_new_learning": {},
        }
        controller._row_by_id = lambda table, _row_id: (
            {"type": 1} if table == "cards" else None
        )
        controller._latest_revlog = lambda _card_id: {"id": 789, "ease": 3}
        controller._apply_regrade_reverted_state(
            {
                "attempt_id": "batch:0:42",
                "batch_key": "batch",
                "card_id": 42,
                "original_ease": 3,
            }
        )

        restored = RuntimeAttempt.from_dict(
            controller.state["batches"]["batch"]["attempts"][0]
        )
        stats = controller._pending_daily_stats(day, 2)
        self.assertEqual(restored.attempt.original_ease, 3)
        self.assertFalse(restored.attempt.batch_auto_easy_eligible)
        self.assertFalse(restored.instant_candidate)
        self.assertFalse(restored.instant_upgraded)
        self.assertTrue(restored.grade_reverted)
        self.assertEqual(restored.original_revlog, {"id": 789, "ease": 3})
        self.assertEqual(restored.attempt.elapsed_ms, 600)
        self.assertEqual(stats.reviewed_total, 1)
        self.assertEqual(stats.auto_upgraded, 0)
        self.assertEqual(stats.eligible_total, 0)
        self.assertEqual(controller.state["timing_history"][0]["effective_ease"], 3)
        self.assertTrue(controller.state["initial_new_learning"]["42"])

    def test_regrade_state_save_failure_keeps_recovery_journal(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {
            "instant_undo": {"attempt_id": "attempt"},
            "regrade_recovery": {"attempt_id": "attempt", "status": "prepared"},
        }
        original = dict(controller.state)
        controller._apply_regrade_reverted_state = lambda _entry: controller.state.update(
            {"partially_synced": True}
        )
        controller._save = lambda: (_ for _ in ()).throw(OSError("disk full"))
        diagnostics = []
        controller._notify = lambda *_args, **_kwargs: self.fail(
            "recovery diagnostics must not use the foreground notification helper"
        )
        controller._log_background = lambda message, _exc=None: diagnostics.append(message)
        retry = []
        controller._schedule_recovery_sync_retry = lambda: retry.append(True)

        result = controller._commit_regrade_state_sync(
            {"attempt_id": "attempt"}, "done"
        )

        self.assertFalse(result)
        self.assertEqual(controller.state, original)
        self.assertTrue(controller.recovery_blocked)
        self.assertEqual(retry, [True])
        self.assertIn("插件记录待同步", diagnostics[0])

    def test_prepared_journal_save_failure_does_not_start_regrade(self):
        entry = {"attempt_id": "attempt", "status": "ready"}
        controller = BatchController.__new__(BatchController)
        controller.state = {"instant_undo": entry}
        controller.mw = SimpleNamespace(col=object())
        controller.undo_in_progress = False
        controller.recovery_blocked = False
        controller.undo_button = None
        controller._instant_undo_is_current = lambda *_args: True
        controller._save = lambda: (_ for _ in ()).throw(OSError("disk full"))
        messages = []
        diagnostics = []
        controller._notify = lambda message, **_kwargs: messages.append(message)
        controller._log_background = lambda message, _exc=None: diagnostics.append(message)

        controller.undo_instant_regrade()

        self.assertIs(controller.state["instant_undo"], entry)
        self.assertNotIn("regrade_recovery", controller.state)
        self.assertFalse(controller.undo_in_progress)
        self.assertEqual(messages, [])
        self.assertIn("未更改评分", diagnostics[0])

    def test_invalidating_regrade_disables_token_and_explains_why(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {"instant_undo": {"attempt_id": "attempt"}}
        controller.undo_notice = None
        controller._save = lambda: None
        messages = []
        controller._notify = lambda message, **_kwargs: messages.append(message)

        controller._invalidate_instant_undo("Anki 中发生了其他操作，撤销改分入口已失效")

        self.assertNotIn("instant_undo", controller.state)
        self.assertEqual(messages, ["Anki 中发生了其他操作，撤销改分入口已失效"])

    def test_cross_day_learning_intervals_do_not_end_the_new_card_pass(self):
        class _Db:
            def __init__(self, rows):
                self.rows = rows

            def all(self, *_args):
                return self.rows

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            col=SimpleNamespace(db=_Db([(3, 1, 0), (3, -600, 0)]))
        )

        current, previous, has_prior = controller._pass_history(
            12, 2, current_card_type=1
        )

        self.assertEqual(current, (3, 3))
        self.assertEqual(previous, [])
        self.assertFalse(has_prior)

    def test_new_card_history_can_be_checked_for_instant_upgrade(self):
        class _Db:
            def all(self, *_args):
                return []

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(col=SimpleNamespace(db=_Db()))

        current, previous, has_prior = controller._pass_history(
            12, 2, current_card_type=0
        )
        attempt = Attempt(
            attempt_id="new-card",
            card_id=12,
            deck_id=1,
            sequence=0,
            original_ease=3,
            elapsed_ms=500,
            prior_ratings=current,
            prior_pass_ratings=tuple(previous),
            pass_history_known=True,
            leaves_today_learning=True,
            answered_at_ms=0,
        )

        self.assertFalse(has_prior)
        self.assertTrue(history_allows(attempt, 2))

    def test_pass_history_keeps_dirty_cross_day_learning_ratings_together(self):
        class _Db:
            def all(self, *_args):
                return [(1, -60, 0), (2, 1, 0), (3, -600, 0)]

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            col=SimpleNamespace(db=_Db())
        )

        current, previous, has_prior = controller._pass_history(
            12, 1, current_card_type=1
        )

        self.assertEqual(current, (1, 2, 3))
        self.assertEqual(previous, [])
        self.assertFalse(has_prior)

    def test_review_state_completes_a_trailing_learning_pass(self):
        class _Db:
            def all(self, *_args):
                return [(3, 1, 0), (3, -600, 0)]

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            col=SimpleNamespace(db=_Db())
        )

        current, previous, has_prior = controller._pass_history(
            12, 2, current_card_type=2
        )

        self.assertEqual(current, ())
        self.assertEqual(previous, [(3, 3)])
        self.assertTrue(has_prior)

    def test_cross_day_relearning_intervals_do_not_end_a_dirty_pass(self):
        class _Db:
            def all(self, *_args):
                return [(1, -60, 1), (3, 1, 2), (3, -600, 2)]

        controller = BatchController.__new__(BatchController)
        controller.mw = SimpleNamespace(
            col=SimpleNamespace(db=_Db())
        )

        current, previous, has_prior = controller._pass_history(
            12, 2, current_card_type=3
        )

        self.assertEqual(current, (1, 3, 3))
        self.assertEqual(previous, [])
        self.assertFalse(has_prior)

    def test_unrescheduled_preview_state_is_excluded(self):
        class _Filtered:
            def WhichOneof(self, _name):
                return "preview"

        class _State:
            filtered = _Filtered()

            def WhichOneof(self, _name):
                return "filtered"

        self.assertTrue(_is_unrescheduled_preview(_State()))
        self.assertFalse(_is_unrescheduled_preview(_SchedulingState()))

    def test_instant_quota_counts_other_open_batches_before_approving(self):
        def runtime(attempt_id, card_id, *, instant_candidate=False, instant_upgraded=False):
            pure = Attempt(
                attempt_id=attempt_id,
                card_id=card_id,
                deck_id=10,
                sequence=0,
                original_ease=4 if instant_upgraded else 3,
                elapsed_ms=300,
                prior_ratings=(),
                leaves_today_learning=False,
                answered_at_ms=1,
            )
            return RuntimeAttempt(
                attempt=pure,
                original_card={},
                post_card=None,
                original_revlog=None,
                easy_answer_b64="",
                instant_candidate=instant_candidate,
                instant_upgraded=instant_upgraded,
            ).to_dict()

        day = "2026-11-10"
        controller = BatchController.__new__(BatchController)
        controller.state = {
            "daily": {
                day: {
                    "auto_upgraded": 0,
                    "eligible_total": 0,
                    "reviewed_total": 4,
                }
            },
            "batches": {
                f"{day}:10:0": {
                    "day": day,
                    "daily_counted": False,
                    "attempts": [
                        runtime(
                            "instant-a",
                            1,
                            instant_candidate=True,
                            instant_upgraded=True,
                        )
                    ],
                },
                f"{day}:20:0": {
                    "day": day,
                    "daily_counted": False,
                    "attempts": [runtime("review-b", 2)],
                },
            },
        }

        self.assertFalse(
            controller._instant_quota_allows(
                RuleConfig(
                    strategy="fixed",
                    quota_enabled=True,
                    quota_percent=0.20,
                    quota_scope="all_reviewed",
                ),
                day,
            )
        )

    def test_percent_page_builds_with_explicit_label_factory(self):
        class _Signal:
            def connect(self, _callback):
                pass

        class _Spin:
            def __init__(self):
                self.valueChanged = _Signal()

            def setRange(self, *_args):
                pass

            def setSingleStep(self, *_args):
                pass

            def setDecimals(self, *_args):
                pass

            def setSuffix(self, *_args):
                pass

            def setValue(self, *_args):
                pass

        class _Label:
            def __init__(self, *_args):
                self.word_wrap = False

            def setWordWrap(self, value):
                self.word_wrap = value

            def setAlignment(self, *_args):
                pass

        class _Form:
            def __init__(self, _parent):
                self.rows = []

            def setContentsMargins(self, *_args):
                pass

            def setHorizontalSpacing(self, *_args):
                pass

            def setVerticalSpacing(self, *_args):
                pass

            def setColumnStretch(self, *_args):
                pass

            def addWidget(self, *row):
                self.rows.append(row)

            def addRow(self, *row):
                self.rows.append(row)

        class _Page:
            pass

        class _Widget:
            def __call__(self):
                return _Page()

        class _Stack:
            def __init__(self):
                self.widgets = []

            def addWidget(self, page):
                self.widgets.append(page)

        dialog = SettingsDialog.__new__(SettingsDialog)
        dialog.mode_stack = _Stack()
        with patch(
            "anki_batch_auto_easy.integration._right_aligned_label",
            side_effect=lambda *_args: _Label(),
        ):
            dialog._build_percent_page(
                _Spin,
                _Form,
                _Widget,
                _Label,
                {"distribution_percent": 0.2},
                120,
            )

        self.assertIsInstance(dialog.percent_help, _Label)
        self.assertTrue(dialog.percent_help.word_wrap)
        self.assertEqual(len(dialog.mode_stack.widgets), 1)

    def test_batch_resumes_open_and_increments_after_finalized(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {"batches": {}}
        controller.current_batch_key = None

        controller._ensure_batch("2026-11-10:10", "2026-11-10", 10)
        first = controller.current_batch_key
        self.assertEqual(first, "2026-11-10:10:0")

        controller.state["batches"][first]["status"] = "finalized"
        controller._ensure_batch("2026-11-10:10", "2026-11-10", 10)
        second = controller.current_batch_key
        self.assertEqual(second, "2026-11-10:10:1")

        controller.current_batch_key = None
        controller._ensure_batch("2026-11-10:10", "2026-11-10", 10)
        self.assertEqual(controller.current_batch_key, second)

    def test_batch_number_is_numeric(self):
        controller = BatchController.__new__(BatchController)
        controller.state = {
            "batches": {
                "2026-11-10:10:9": {
                    "day": "2026-11-10",
                    "deck_id": 10,
                    "status": "open",
                    "attempts": [],
                },
                "2026-11-10:10:10": {
                    "day": "2026-11-10",
                    "deck_id": 10,
                    "status": "open",
                    "attempts": [],
                },
            }
        }
        controller.current_batch_key = None
        controller._ensure_batch("2026-11-10:10", "2026-11-10", 10)
        self.assertEqual(controller.current_batch_key, "2026-11-10:10:10")

    def test_learning_state_does_not_count_as_leaving_learning(self):
        self.assertTrue(_leaves_today_learning(_SchedulingState(normal_kind="review")))
        self.assertFalse(
            _leaves_today_learning(
                _SchedulingState(normal_kind="learning")
            )
        )
        self.assertFalse(
            _leaves_today_learning(
                _SchedulingState(normal_kind="relearning")
            )
        )

    def test_already_replaced_marker_is_idempotent(self):
        original = {"id": 10, "cid": 20, "time": 1234, "ease": 3}
        replaced = {"id": 10, "cid": 20, "time": 1234, "ease": 4}
        self.assertTrue(_already_replaced(replaced, original))
        self.assertFalse(_already_replaced(original, original))

    def test_state_path_is_profile_scoped(self):
        profile = type(
            "ProfileManager",
            (),
            {"profileFolder": lambda self: "/tmp/anki-profile"},
        )()
        mw = type("MainWindow", (), {"pm": profile})()
        self.assertEqual(
            _state_path(mw),
            Path("/tmp/anki-profile/anki_batch_auto_easy_state.json"),
        )


if __name__ == "__main__":
    unittest.main()
