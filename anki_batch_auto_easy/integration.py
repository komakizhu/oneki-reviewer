from __future__ import annotations

import base64
import copy
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .adapter import (
    AdapterRecoveryError,
    AnkiReplacementAdapter,
    ReplacementRecord,
    _already_replaced,
)
from .core import (
    eligible_attempts,
    evaluate_batch,
    history_allows,
    minimum_batch_size,
    quota_allows_increment,
    should_auto_upgrade_now,
    smart_minimum_size,
    validate_config,
)
from .i18n import tr, trf
from .models import Attempt, DailyStats, RuleConfig
from .state import StateStore


PACKAGE_NAME = __package__.split(".")[0]
VALID_AUTO_EASY_TIMER_STARTS = {"question", "answer"}
VALID_AUTO_HARD_THRESHOLD_MODES = {"anki_max_taken", "custom"}
VALID_AUTO_GRADE_PRIORITIES = {"hard", "easy"}
READ_COLUMNS = {
    # Keep these reads away from PRAGMA table_info(): on Anki 26.9.x the
    # schema-inspection query clears the just-created reviewer undo entry.
    "cards": (
        "id",
        "nid",
        "did",
        "ord",
        "mod",
        "usn",
        "type",
        "queue",
        "due",
        "ivl",
        "factor",
        "reps",
        "lapses",
        "left",
        "odue",
        "odid",
        "flags",
        "data",
    ),
    "revlog": (
        "id",
        "cid",
        "usn",
        "ease",
        "ivl",
        "lastIvl",
        "factor",
        "time",
        "type",
    ),
}


def _grade_label(grade: int) -> str:
    labels = {
        1: tr("重来", "Again"),
        2: tr("困难", "Hard"),
        3: tr("良好", "Good"),
        4: tr("简单", "Easy"),
    }
    return labels.get(int(grade), str(grade))


class RegradeRecoveryError(RuntimeError):
    pass


class RegradeCompensatedError(RuntimeError):
    pass


DEFAULT_CONFIG = {
    "enabled": True,
    "auto_easy_enabled": True,
    "auto_hard_enabled": True,
    "auto_easy_timer_start": "answer",
    "auto_hard_threshold_mode": "anki_max_taken",
    "auto_hard_threshold_ms": 60_000,
    "auto_grade_priority": "hard",
    "instant_undo_shortcut": "Z",
    "strategy": "fixed",
    "threshold_ms": 1000,
    "distribution_percent": 0.20,
    "minimum_batch_size": 5,
    "smart_minimum_mode": "percent",
    "smart_minimum_percent": 0.20,
    "history_window": 2,
    "smart_mad_multiplier": 2.0,
    "quota_enabled": False,
    "quota_percent": 0.20,
    "quota_scope": "eligible_pool",
}


@dataclass
class RuntimeAttempt:
    attempt: Attempt
    original_card: dict[str, Any]
    post_card: dict[str, Any] | None
    original_revlog: dict[str, Any] | None
    easy_answer_b64: str
    unrescheduled_preview: bool = False
    initial_new_learning: bool = False
    instant_candidate: bool = False
    instant_upgraded: bool = False
    user_ease: int | None = None
    grade_reverted: bool = False
    original_answer_b64: str = ""
    input_source: str = "unknown"
    review_undone: bool = False
    reviewer_undo_step: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "original_card": self.original_card,
            "post_card": self.post_card,
            "original_revlog": self.original_revlog,
            "easy_answer_b64": self.easy_answer_b64,
            "unrescheduled_preview": self.unrescheduled_preview,
            "initial_new_learning": self.initial_new_learning,
            "instant_candidate": self.instant_candidate,
            "instant_upgraded": self.instant_upgraded,
            "user_ease": self.user_ease,
            "grade_reverted": self.grade_reverted,
            "original_answer_b64": self.original_answer_b64,
            "input_source": self.input_source,
            "review_undone": self.review_undone,
            "reviewer_undo_step": self.reviewer_undo_step,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RuntimeAttempt:
        input_source = str(value.get("input_source", "unknown"))
        attempt = Attempt.from_dict(value["attempt"])
        if input_source != "space":
            attempt = replace(attempt, batch_auto_easy_eligible=False)
        return cls(
            attempt=attempt,
            original_card=dict(value["original_card"]),
            post_card=value.get("post_card"),
            original_revlog=value.get("original_revlog"),
            easy_answer_b64=str(value["easy_answer_b64"]),
            unrescheduled_preview=bool(value.get("unrescheduled_preview", False)),
            initial_new_learning=bool(value.get("initial_new_learning", False)),
            instant_candidate=bool(value.get("instant_candidate", False)),
            instant_upgraded=bool(value.get("instant_upgraded", False)),
            user_ease=(
                int(value["user_ease"]) if value.get("user_ease") is not None else None
            ),
            grade_reverted=bool(value.get("grade_reverted", False)),
            original_answer_b64=str(value.get("original_answer_b64", "")),
            input_source=input_source,
            review_undone=bool(value.get("review_undone", False)),
            reviewer_undo_step=(
                int(value["reviewer_undo_step"])
                if value.get("reviewer_undo_step") is not None
                else None
            ),
        )

    def replacement_record(self) -> ReplacementRecord:
        if self.post_card is None or self.original_revlog is None:
            raise RuntimeError(f"attempt {self.attempt.attempt_id} is incomplete")
        return ReplacementRecord(
            attempt_id=self.attempt.attempt_id,
            card_id=self.attempt.card_id,
            original_revlog_id=int(self.original_revlog["id"]),
            original_card=self.original_card,
            original_revlog=self.original_revlog,
            post_card=self.post_card,
            easy_answer_b64=self.easy_answer_b64,
            original_answer_b64=self.original_answer_b64,
            original_ease=int(self.attempt.original_ease),
            reviewer_undo_step=self.reviewer_undo_step,
        )


def _active_batch_attempts(batch: dict[str, Any]) -> list[RuntimeAttempt]:
    return [
        RuntimeAttempt.from_dict(value)
        for value in batch.get("attempts", [])
        if not value.get("review_undone", False)
    ]


def _completed_regrade_summary(attempts: list[RuntimeAttempt]) -> str:
    cards_by_grade: dict[int, set[int]] = {4: set(), 2: set(), 1: set()}
    for runtime in attempts:
        if runtime.review_undone or runtime.grade_reverted or not runtime.original_revlog:
            continue
        effective_ease = runtime.attempt.original_ease
        if effective_ease not in cards_by_grade:
            continue
        if runtime.user_ease != 3 and not runtime.instant_upgraded:
            continue
        if int(runtime.original_revlog.get("ease", 0)) != effective_ease:
            continue
        cards_by_grade[effective_ease].add(runtime.attempt.card_id)
    parts = [
        trf(
            "按「{grade}」评分 {count} 张",
            "{grade}: {count} card{suffix}",
            grade=_grade_label(grade),
            count=len(card_ids),
            suffix="" if len(card_ids) == 1 else "s",
        )
        for grade, card_ids in cards_by_grade.items()
        if card_ids
    ]
    if not parts:
        return tr("本轮复习汇总：没有自动调整评分。", "Review summary: no ratings were changed automatically.")
    return trf(
        "本轮复习汇总：答题时已自动{parts}。",
        "Review summary: ratings applied while answering: {parts}.",
        parts=tr("、", ", ").join(parts),
    )


class BatchController:
    def __init__(self, mw: Any):
        self.mw = mw
        self.state_store = StateStore(_state_path(mw))
        self.state = self.state_store.load()
        self.answer_timer_started: dict[int, float] = {}
        self.question_timer_started: dict[int, float] = {}
        self.answer_revealed_timer_started: dict[int, float] = {}
        self.pending_by_card: dict[int, str] = {}
        self.pending_attempts: dict[int, RuntimeAttempt] = {}
        self.pending_timing_samples: dict[int, dict[str, Any]] = {}
        self.current_batch_key: str | None = None
        self.review_session_id: str | None = None
        self.finalizing = False
        self.undo_in_progress = False
        self.recovery_blocked = False
        self.undo_notice = None
        self.undo_button = None
        self.shortcut_conflict = False
        self.last_shortcut_conflict = None
        self.recovery_sync_retry_count = 0
        self.space_pending: tuple[Any, Any, object] | None = None
        self.space_answer: tuple[Any, Any, object] | None = None
        self.space_binding_warning = False
        self._recover_interrupted_regrade()

    def config(self) -> dict[str, Any]:
        try:
            raw = self.mw.addonManager.getConfig(PACKAGE_NAME)
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise ValueError("configuration must be an object")
            merged = {**DEFAULT_CONFIG, **raw}
            for key in ("enabled", "auto_hard_enabled", "quota_enabled"):
                if not isinstance(merged[key], bool):
                    raise ValueError(f"{key} must be a boolean")
            validate_config(
                RuleConfig(
                    strategy=str(merged["strategy"]),
                    threshold_ms=int(merged["threshold_ms"]),
                    distribution_percent=float(merged["distribution_percent"]),
                    minimum_batch_size=(
                        int(merged["minimum_batch_size"])
                        if merged.get("minimum_batch_size") is not None
                        else None
                    ),
                    smart_minimum_mode=str(merged["smart_minimum_mode"]),
                    smart_minimum_percent=float(merged["smart_minimum_percent"]),
                    history_window=int(merged["history_window"]),
                    smart_mad_multiplier=float(merged["smart_mad_multiplier"]),
                    quota_enabled=bool(merged["quota_enabled"]),
                    quota_percent=float(merged["quota_percent"]),
                    quota_scope=str(merged["quota_scope"]),
                )
            )
            if str(merged["auto_easy_timer_start"]) not in VALID_AUTO_EASY_TIMER_STARTS:
                raise ValueError("unsupported auto-easy timer start")
            if str(merged["auto_hard_threshold_mode"]) not in VALID_AUTO_HARD_THRESHOLD_MODES:
                raise ValueError("unsupported auto-hard threshold mode")
            if int(merged["auto_hard_threshold_ms"]) < 1:
                raise ValueError("auto-hard threshold must be positive")
            if str(merged["auto_grade_priority"]) not in VALID_AUTO_GRADE_PRIORITIES:
                raise ValueError("unsupported automatic grade priority")
        except (TypeError, ValueError, KeyError):
            return {**DEFAULT_CONFIG, "enabled": False}
        return merged

    def rule_config(self) -> RuleConfig:
        value = self.config()
        return RuleConfig(
            strategy=str(value["strategy"]),
            threshold_ms=int(value["threshold_ms"]),
            distribution_percent=float(value["distribution_percent"]),
            minimum_batch_size=(
                int(value["minimum_batch_size"])
                if value.get("minimum_batch_size") is not None
                else None
            ),
            smart_minimum_mode=str(value["smart_minimum_mode"]),
            smart_minimum_percent=float(value["smart_minimum_percent"]),
            history_window=int(value["history_window"]),
            smart_mad_multiplier=float(value["smart_mad_multiplier"]),
            quota_enabled=bool(value["quota_enabled"]),
            quota_percent=float(value["quota_percent"]),
            quota_scope=str(value["quota_scope"]),
        )

    def enabled(self) -> bool:
        return bool(self.config()["enabled"])

    def did_show_question(self, card: Any) -> None:
        self._clear_space_input()
        if getattr(self, "recovery_blocked", False):
            self._recover_interrupted_regrade()
        self._start_auto_easy_timer(card, "question")
        if getattr(self, "state", {}).get("instant_undo"):
            self._set_review_shortcut_armed(True)

    def did_show_answer(self, card: Any) -> None:
        self._clear_space_input()
        self._start_auto_easy_timer(card, "answer")

    def _answer_input_source(self, reviewer: Any, card: Any) -> str:
        active = getattr(self, "space_answer", None)
        if active is not None and active[0] is reviewer and active[1] is card:
            return "space"
        return "other"

    def _start_auto_easy_timer(self, card: Any, event: str) -> None:
        config = self.config()
        if not bool(config["enabled"]):
            return
        card_id = int(card.id)
        started = time.monotonic()
        if event == "question":
            self.question_timer_started[card_id] = started
            self.answer_revealed_timer_started.pop(card_id, None)
        else:
            self.answer_revealed_timer_started.setdefault(card_id, started)
        if config["auto_easy_timer_start"] == event:
            self.answer_timer_started[card_id] = started

    def will_answer(self, payload: tuple[bool, int], reviewer: Any, card: Any):
        proceed, ease = payload
        input_source = self._answer_input_source(reviewer, card)
        self._clear_space_input()
        if (
            not proceed
            or getattr(self, "finalizing", False)
            or getattr(self, "undo_in_progress", False)
            or getattr(self, "recovery_blocked", False)
        ):
            return payload
        if not self.enabled():
            return payload

        self._invalidate_instant_undo()

        card_id = int(card.id)
        answered_at_ms = int(time.time() * 1000)
        now_monotonic = time.monotonic()
        started = self.answer_timer_started.pop(card_id, None)
        elapsed_ms = (
            max(0, int((now_monotonic - started) * 1000))
            if started is not None
            else 0
        )
        question_started = self.question_timer_started.pop(card_id, None)
        answer_started = self.answer_revealed_timer_started.pop(card_id, None)
        question_to_answer_ms = (
            max(0, int((answer_started - question_started) * 1000))
            if question_started is not None and answer_started is not None
            else None
        )
        question_elapsed_ms = _read_card_time_taken(card)
        if question_elapsed_ms is None and question_started is not None:
            question_elapsed_ms = max(
                0,
                int((now_monotonic - question_started) * 1000),
            )
        answer_elapsed_ms = (
            max(0, int((now_monotonic - answer_started) * 1000))
            if answer_started is not None
            else None
        )
        max_answer_ms = _read_card_time_limit(card)
        if getattr(self, "recovery_blocked", False):
            return payload
        try:
            attempt, effective_ease = self._capture_attempt(
                reviewer,
                card,
                int(ease),
                elapsed_ms,
                elapsed_known=started is not None,
                question_to_answer_ms=question_to_answer_ms,
                max_answer_ms=max_answer_ms,
                input_source=input_source,
            )
        except Exception as exc:
            print(f"{PACKAGE_NAME}: unable to capture review attempt: {exc}")
            self.pending_timing_samples[card_id] = _timing_sample(
                answered_at_ms=answered_at_ms,
                card_id=card_id,
                deck_id=int(card.did),
                user_ease=int(ease),
                effective_ease=int(ease),
                question_elapsed_ms=question_elapsed_ms,
                question_to_answer_ms=question_to_answer_ms,
                answer_elapsed_ms=answer_elapsed_ms,
                max_answer_ms=max_answer_ms,
                input_source=input_source,
            )
            return payload

        self.pending_timing_samples[card_id] = _timing_sample(
            answered_at_ms=answered_at_ms,
            card_id=card_id,
            deck_id=int(card.did),
            user_ease=int(ease),
            effective_ease=effective_ease,
            question_elapsed_ms=question_elapsed_ms,
            question_to_answer_ms=question_to_answer_ms,
            answer_elapsed_ms=answer_elapsed_ms,
            max_answer_ms=max_answer_ms,
            input_source=input_source,
        )
        self.pending_by_card[card_id] = attempt.attempt.attempt_id
        self.pending_attempts[card_id] = attempt
        return (proceed, effective_ease)

    def did_answer(self, reviewer: Any, card: Any, ease: int) -> None:
        card_id = int(card.id)
        timing_sample = self.pending_timing_samples.pop(card_id, None)
        adjustment_message = None
        user_ease = None
        plugin_ease = None
        if timing_sample is not None:
            plugin_ease = timing_sample.get("effective_ease")
            timing_sample["effective_ease"] = int(ease)
            user_ease = timing_sample.get("user_ease")
            if (
                user_ease is not None
                and plugin_ease is not None
                and int(user_ease) != int(plugin_ease)
                and int(plugin_ease) == int(ease)
            ):
                adjustment_message = trf(
                    "自动调整评分：{before} → {after}",
                    "Rating adjusted automatically: {before} → {after}",
                    before=_grade_label(int(user_ease)),
                    after=_grade_label(int(ease)),
                )
        if not self.enabled():
            if timing_sample is not None:
                self._append_timing_sample(timing_sample)
            if adjustment_message:
                self._notify(adjustment_message)
            return
        attempt_id = self.pending_by_card.pop(card_id, None)
        if not attempt_id:
            if timing_sample is not None:
                self._append_timing_sample(timing_sample)
            if adjustment_message:
                self._notify(adjustment_message)
            return
        runtime_attempt = self.pending_attempts.pop(card_id, None)
        if runtime_attempt is None:
            if timing_sample is not None:
                self._append_timing_sample(timing_sample)
            if adjustment_message:
                self._notify(adjustment_message)
            return
        undo_entry_error = None
        try:
            card.load()
            runtime_attempt.post_card = self._row_by_id("cards", card_id)
            runtime_attempt.original_revlog = self._latest_revlog(card_id)
            undo_status = self.mw.col.undo_status()
            runtime_attempt.reviewer_undo_step = (
                int(undo_status.last_step)
                if getattr(undo_status, "undo", "") == "Answer Card"
                else None
            )
            if timing_sample is not None:
                timing_sample["attempt_id"] = attempt_id
            initial_cards = self.state.setdefault("initial_new_learning", {})
            card_key = str(card_id)
            post_card_type = int((runtime_attempt.post_card or {}).get("type", -1))
            if not runtime_attempt.unrescheduled_preview:
                if runtime_attempt.initial_new_learning and post_card_type == 1:
                    initial_cards[card_key] = True
                else:
                    initial_cards.pop(card_key, None)
            self._store_attempt(runtime_attempt)
            if timing_sample is not None:
                history = self.state.setdefault("timing_history", [])
                history.append(dict(timing_sample))
                del history[:-100]
            if (
                timing_sample is not None
                and user_ease is not None
                and int(user_ease) != int(ease)
                and plugin_ease is not None
                and int(plugin_ease) == int(ease)
                and runtime_attempt.original_answer_b64
            ):
                try:
                    self.state["instant_undo"] = self._instant_undo_entry(
                        runtime_attempt,
                        timing_sample,
                    )
                except Exception as exc:
                    self.state.pop("instant_undo", None)
                    undo_entry_error = exc
            self._save()
            if self.state.get("instant_undo"):
                self._show_instant_undo_notice(user_ease, ease)
            elif adjustment_message:
                self._notify(adjustment_message)
            if undo_entry_error is not None:
                self._log_background(
                    trf(
                        "评分已调整，但撤销改分暂不可用：{error}",
                        "The rating was adjusted, but undo is unavailable: {error}",
                        error=undo_entry_error,
                    ),
                    undo_entry_error,
                )
        except Exception as exc:
            self.recovery_blocked = True
            self.state.pop("instant_undo", None)
            self._log_background(
                trf(
                    "已完成评分，但插件记录保存失败，撤销改分暂不可用：{error}",
                    "The rating was applied, but add-on state could not be saved; undo is unavailable: {error}",
                    error=exc,
                ),
                exc,
            )

    def _commit_regrade_state_sync(
        self, entry: dict[str, Any], success_message: str
    ) -> bool:
        saved_state = copy.deepcopy(self.state)
        try:
            self._apply_regrade_reverted_state(entry)
            self.state.pop("regrade_recovery", None)
            self.state.pop("instant_undo", None)
            self._save()
            self.recovery_blocked = False
            self.recovery_sync_retry_count = 0
            self._set_review_shortcut_armed(False)
        except Exception as exc:
            self.state = saved_state
            self.recovery_blocked = True
            self._log_background(
                trf(
                    "评分已恢复，但插件记录待同步：{error}",
                    "The rating was restored, but the add-on state still needs syncing: {error}",
                    error=exc,
                ),
                exc,
            )
            self._schedule_recovery_sync_retry()
            return False
        self._notify(success_message)
        try:
            reviewer = getattr(self.mw, "reviewer", None)
            if reviewer is not None:
                reviewer.refresh_if_needed()
        except Exception as exc:
            print(f"{PACKAGE_NAME}: reviewer refresh after rating restore failed: {exc}")
        return True

    def _instant_undo_entry(
        self,
        runtime_attempt: RuntimeAttempt,
        timing_sample: dict[str, Any],
    ) -> dict[str, Any]:
        undo_status = self.mw.col.undo_status()
        if not getattr(undo_status, "undo", ""):
            raise RuntimeError(
                tr("Anki 未保留最近答题的撤销记录", "Anki did not keep an undo entry for the latest answer.")
            )
        original_revlog = runtime_attempt.original_revlog
        post_card = runtime_attempt.post_card
        if not original_revlog or not post_card:
            raise RuntimeError(tr("答题记录不完整，无法安全启用撤销", "The answer record is incomplete; undo cannot be enabled safely."))
        batch_key = self.current_batch_key
        batch = self.state.get("batches", {}).get(batch_key or "")
        if not batch or batch.get("status") == "finalized" or batch.get("daily_counted"):
            raise RuntimeError(tr("复习批次已结束，无法安全启用撤销", "This review batch has ended; undo cannot be enabled safely."))
        return {
            "attempt_id": runtime_attempt.attempt.attempt_id,
            "card_id": runtime_attempt.attempt.card_id,
            "batch_key": batch_key,
            "day": str(batch["day"]),
            "reviewer_undo_step": int(undo_status.last_step),
            "original_ease": int(runtime_attempt.user_ease or 3),
            "effective_ease": int(runtime_attempt.attempt.original_ease),
            "original_answer_b64": runtime_attempt.original_answer_b64,
            "original_card": runtime_attempt.original_card,
            "post_card": post_card,
            "post_revlog": original_revlog,
            "timing_sample": dict(timing_sample),
            "status": "ready",
        }

    def _show_instant_undo_notice(self, original_ease: int, effective_ease: int) -> None:
        from aqt.qt import QFrame, QHBoxLayout, QLabel, QPushButton, Qt, QTimer

        if self.undo_notice is not None:
            self.undo_notice.close()
            self.undo_notice.deleteLater()
        notice = QFrame(
            self.mw,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        notice.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(notice)
        label = QLabel(
            trf(
                "自动调整评分：{before} → {after}",
                "Rating adjusted automatically: {before} → {after}",
                before=_grade_label(int(original_ease)),
                after=_grade_label(int(effective_ease)),
            ),
            notice,
        )
        button = QPushButton(tr("撤销改分", "Undo rating change"), notice)
        button.setToolTip(
            tr(
                "恢复为你刚选的评分，保留这次答题和复习记录",
                "Restore the rating you selected while keeping this answer and review history.",
            )
        )
        button.clicked.connect(self.undo_instant_regrade)
        layout.addWidget(label)
        layout.addWidget(button)
        notice.adjustSize()
        bottom_right = self.mw.mapToGlobal(self.mw.rect().bottomRight())
        notice.move(
            bottom_right.x() - notice.width() - 24,
            bottom_right.y() - notice.height() - 64,
        )
        notice.show()
        self.undo_notice = notice
        self.undo_button = button
        self._set_review_shortcut_armed(True)
        QTimer.singleShot(7000, notice.close)

    def _set_review_shortcut_armed(self, armed: bool) -> None:
        mw = getattr(self, "mw", None)
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            return
        armed = bool(armed and not getattr(self, "shortcut_conflict", False))
        try:
            reviewer.web.eval(
                f"window.__batchAutoEasyUndoArmed = {'true' if armed else 'false'};"
            )
        except Exception:
            pass

    def _update_review_shortcut_script(self) -> None:
        reviewer = getattr(getattr(self, "mw", None), "reviewer", None)
        if reviewer is None:
            return
        shortcut = str(self.config().get("instant_undo_shortcut", "Z")).strip()
        try:
            reviewer.web.eval(
                _review_shortcut_script(
                    shortcut,
                    enabled=not getattr(self, "shortcut_conflict", False),
                )
            )
        except Exception:
            pass

    def _invalidate_instant_undo(self, reason: str | None = None) -> None:
        state = getattr(self, "state", {})
        entry = state.pop("instant_undo", None)
        self._set_review_shortcut_armed(False)
        if entry:
            try:
                self._save()
            except Exception as exc:
                self.recovery_blocked = True
                self._log_background(
                    trf(
                        "无法安全结束上次撤销状态：{error}",
                        "The previous undo state could not be closed safely: {error}",
                        error=exc,
                    ),
                    exc,
                )
            if reason:
                self._notify(reason)
        notice = getattr(self, "undo_notice", None)
        if notice is not None:
            notice.close()
            notice.deleteLater()
            self.undo_notice = None
        self.undo_button = None

    def undo_instant_regrade(self) -> None:
        if getattr(self, "undo_in_progress", False) or getattr(
            self, "recovery_blocked", False
        ):
            return
        entry = self.state.get("instant_undo")
        if not entry or entry.get("status") != "ready":
            self._log_background(
                tr("这次改分已不能撤销", "This rating change can no longer be undone.")
            )
            return
        if not self._instant_undo_is_current(self.mw.col, entry):
            self._invalidate_instant_undo()
            self._log_background(
                tr("卡片或复习记录已变化，无法安全撤销", "The card or review record changed; it is not safe to undo.")
            )
            return

        journal = copy.deepcopy(entry)
        journal["status"] = "prepared"
        journal["plugin_state_before"] = copy.deepcopy(self.state)
        self.state["regrade_recovery"] = journal
        try:
            self._save()
        except Exception as exc:
            self.state.pop("regrade_recovery", None)
            self._log_background(
                trf(
                    "撤销记录保存失败，未更改评分：{error}",
                    "Could not save the undo record; the rating was not changed: {error}",
                    error=exc,
                ),
                exc,
            )
            return

        self.undo_in_progress = True
        self._set_review_shortcut_armed(False)
        if self.undo_button is not None:
            self.undo_button.setEnabled(False)

        def regrade(col: Any):
            from anki.scheduler.v3 import CardAnswer

            if not self._instant_undo_is_current(col, entry):
                raise RuntimeError(tr("卡片或复习记录已变化，无法安全撤销", "The card or review record changed; it is not safe to undo."))
            status = col.undo_status()
            if int(status.last_step) != int(entry["reviewer_undo_step"]):
                raise RuntimeError(tr("Anki 的最近操作已改变，无法安全撤销", "Anki's most recent action changed; it is not safe to undo."))
            answer = CardAnswer.FromString(
                base64.b64decode(entry["original_answer_b64"])
            )
            if int(answer.card_id) != int(entry["card_id"]):
                raise RuntimeError(tr("保存的原评分与卡片不匹配", "The saved original rating does not match the card."))
            scheduler_reps = int(col.sched.reps)
            col.undo()
            try:
                restored_card = _row_by_id_from_collection(
                    col, "cards", int(entry["card_id"])
                )
                if not restored_card or not _same_card_state(
                    restored_card, entry["original_card"]
                ):
                    raise RuntimeError(tr("还原自动评分后，卡片状态与答题前不符", "After restoring the automatic rating, the card state did not match its pre-answer state."))
                col.sched.reps = max(0, scheduler_reps - 1)
                changes = col.sched.answer_card(answer)
                return changes
            except Exception as original_error:
                try:
                    col.redo()
                    col.sched.reps = scheduler_reps
                except Exception as recovery_error:
                    raise RegradeRecoveryError(
                        trf(
                            "按原评分重答和恢复自动评分都失败：{error}",
                            "Replaying the original rating and restoring the automatic rating both failed: {error}",
                            error=recovery_error,
                        )
                    )
                raise RegradeCompensatedError(str(original_error)) from original_error

        def success(_changes: Any) -> None:
            try:
                self._commit_regrade_state_sync(
                    entry,
                    tr("已撤销自动改分，保留本次答题", "Automatic rating undone; this answer was kept."),
                )
            finally:
                self.undo_in_progress = False
                if self.undo_notice is not None:
                    self.undo_notice.close()
                    self.undo_notice = None
                    self.undo_button = None

        def failure(exc: Exception) -> None:
            self.undo_in_progress = False
            if isinstance(exc, RegradeRecoveryError):
                self.recovery_blocked = True
                self._set_review_shortcut_armed(False)
                if self.undo_button is not None:
                    self.undo_button.setEnabled(False)
                journal = self.state.get("regrade_recovery")
                if journal:
                    journal["status"] = "compensation_failed"
                    journal["failure"] = str(exc)
                    try:
                        self._save()
                    except Exception as save_error:
                        self._log_background(
                            trf(
                                "恢复日志保存失败：{error}",
                                "Could not save the recovery log: {error}",
                                error=save_error,
                            ),
                            save_error,
                        )
                self._log_background(
                    trf(
                        "撤销补偿失败，插件已暂停自动处理：{error}",
                        "Undo recovery failed; automatic processing is paused: {error}",
                        error=exc,
                    ),
                    exc,
                )
                self._schedule_recovery_sync_retry()
                self.mw.update_undo_actions()
                return
            if isinstance(exc, RegradeCompensatedError):
                self._recover_regrade_failure(entry, exc, compensated=True)
                self.mw.update_undo_actions()
                return
            self._recover_regrade_failure(entry, exc)
            self.mw.update_undo_actions()

        from aqt.operations import CollectionOp

        CollectionOp(self.mw, regrade).success(success).failure(failure).run_in_background(
            initiator=self,
        )

    def _instant_undo_is_current(self, col: Any, entry: dict[str, Any]) -> bool:
        try:
            undo_status = col.undo_status()
            if (
                not getattr(undo_status, "undo", "")
                or int(undo_status.last_step) != int(entry["reviewer_undo_step"])
            ):
                return False
            return self._instant_undo_data_is_current(col, entry)
        except Exception:
            return False

    def _instant_undo_data_is_current(self, col: Any, entry: dict[str, Any]) -> bool:
        try:
            current_card = _row_by_id_from_collection(col, "cards", int(entry["card_id"]))
            current_revlog = _row_by_id_from_collection(
                col, "revlog", int(entry["post_revlog"]["id"])
            )
            if not current_card or not current_revlog:
                return False
            latest = col.db.first(
                "select id from revlog where cid=? order by id desc limit 1",
                int(entry["card_id"]),
            )
            return (
                latest is not None
                and int(latest[0]) == int(entry["post_revlog"]["id"])
                and int(current_revlog.get("ease", 0)) == int(entry["effective_ease"])
                and _same_row(current_revlog, entry["post_revlog"])
                and _same_card_state(current_card, entry["post_card"])
            )
        except Exception:
            return False

    def _apply_regrade_reverted_state(self, entry: dict[str, Any]) -> None:
        attempt_id = str(entry["attempt_id"])
        current_card = self._row_by_id("cards", int(entry["card_id"]))
        current_revlog = self._latest_revlog(int(entry["card_id"]))
        if current_card is None or current_revlog is None:
            raise RuntimeError(tr("找不到已恢复的卡片或复习记录，无法同步学习状态", "The restored card or review record could not be found, so the study state cannot be synced."))
        runtime = None
        for batch_key, batch in self.state.get("batches", {}).items():
            if batch_key != entry.get("batch_key"):
                continue
            if batch.get("status") == "finalized" or batch.get("daily_counted"):
                raise RuntimeError(tr("复习批次已结算，无法同步撤销", "This review batch has been finalized; the undo cannot be synced."))
            for attempt_value in batch.get("attempts", []):
                if attempt_value.get("attempt", {}).get("attempt_id") != attempt_id:
                    continue
                runtime = RuntimeAttempt.from_dict(attempt_value)
                runtime.attempt = replace(
                    runtime.attempt,
                    original_ease=int(entry["original_ease"]),
                    batch_auto_easy_eligible=False,
                )
                runtime.instant_candidate = False
                runtime.instant_upgraded = False
                runtime.grade_reverted = True
                runtime.post_card = current_card
                runtime.original_revlog = current_revlog
                attempt_value.clear()
                attempt_value.update(runtime.to_dict())
                break
        if runtime is None:
            raise RuntimeError(tr("找不到对应复习记录，无法同步撤销", "The matching review record could not be found, so the undo cannot be synced."))
        timing_found = False
        for sample in self.state.get("timing_history", []):
            if sample.get("attempt_id") == attempt_id:
                sample["effective_ease"] = int(entry["original_ease"])
                timing_found = True
                break
        if not timing_found:
            raise RuntimeError(tr("找不到对应计时记录，无法同步撤销", "The matching timing record could not be found, so the undo cannot be synced."))

        initial_cards = self.state.setdefault("initial_new_learning", {})
        key = str(entry["card_id"])
        if runtime.initial_new_learning and int(current_card.get("type", -1)) == 1:
            initial_cards[key] = True
        else:
            initial_cards.pop(key, None)

    def _recover_regrade_failure(
        self,
        entry: dict[str, Any],
        exc: Exception,
        *,
        compensated: bool = False,
    ) -> None:
        col = self.mw.col
        saved_state = copy.deepcopy(self.state)
        safe = (
            self._instant_undo_data_is_current(col, entry)
            if compensated
            else self._instant_undo_is_current(col, entry)
        )
        if safe:
            entry["reviewer_undo_step"] = int(col.undo_status().last_step)
            self.state.pop("regrade_recovery", None)
            self.state["instant_undo"] = entry
        else:
            self.state.pop("instant_undo", None)
            self._set_review_shortcut_armed(False)
            if getattr(self, "undo_notice", None) is not None:
                self.undo_notice.close()
                self.undo_button = None
            journal = self.state.get("regrade_recovery")
            if journal:
                journal["status"] = "recovery_required"
                journal["failure"] = str(exc)
                self.recovery_blocked = True
        try:
            self._save()
            if safe:
                self.recovery_blocked = False
                self._set_review_shortcut_armed(True)
                if getattr(self, "undo_button", None) is not None:
                    self.undo_button.setEnabled(True)
            if self.recovery_blocked:
                self._log_background(
                    trf(
                        "撤销后的恢复状态无法确认，插件已暂停自动处理：{error}",
                        "The post-undo recovery state could not be confirmed; automatic processing is paused: {error}",
                        error=exc,
                    ),
                    exc,
                )
            else:
                self._log_background(
                    trf(
                        "撤销失败，原评分已保留：{error}",
                        "Undo failed; the original rating was kept: {error}",
                        error=exc,
                    ),
                    exc,
                )
        except Exception as save_error:
            self.state = saved_state
            self.recovery_blocked = True
            self._set_review_shortcut_armed(False)
            if getattr(self, "undo_button", None) is not None:
                self.undo_button.setEnabled(False)
            self._log_background(
                trf(
                    "撤销失败且恢复记录保存失败：{error}",
                    "Undo failed and the recovery record could not be saved: {error}",
                    error=save_error,
                ),
                save_error,
            )
            self._schedule_recovery_sync_retry()

    def profile_will_close(self) -> None:
        self._clear_space_input()
        self._invalidate_instant_undo()

    def profile_did_open(self) -> None:
        self.state_store = StateStore(_state_path(self.mw))
        self.state = self.state_store.load()
        self.answer_timer_started.clear()
        self.question_timer_started.clear()
        self.answer_revealed_timer_started.clear()
        self.pending_by_card.clear()
        self.pending_attempts.clear()
        self.pending_timing_samples.clear()
        self.current_batch_key = None
        self.review_session_id = None
        self.recovery_sync_retry_count = 0
        self.shortcut_conflict = False
        self.last_shortcut_conflict = None
        self.space_binding_warning = False
        self._clear_space_input()
        self._recover_interrupted_regrade()

    def _recover_interrupted_regrade(self) -> None:
        journal = self.state.get("regrade_recovery")
        if not journal:
            if self.state.pop("instant_undo", None):
                try:
                    self._save()
                except Exception as exc:
                    print(f"{PACKAGE_NAME}: unable to clear stale undo entry: {exc}")
            self.recovery_blocked = False
            return
        self.recovery_blocked = True
        try:
            from anki.scheduler.v3 import CardAnswer

            card = self._row_by_id("cards", int(journal["card_id"]))
            latest = self._latest_revlog(int(journal["card_id"]))
            auto_revlog = self._row_by_id("revlog", int(journal["post_revlog"]["id"]))
            original_answer = CardAnswer.FromString(
                base64.b64decode(journal["original_answer_b64"])
            )
            if (
                _answer_record_matches(latest, original_answer, int(journal["original_ease"]))
            ):
                self._commit_regrade_state_sync(
                    journal, tr("已完成中断的评分恢复", "Interrupted rating recovery completed.")
                )
                return
            if (
                card
                and auto_revlog
                and latest
                and _same_row(latest, auto_revlog)
                and int(latest.get("ease", 0)) == int(journal["effective_ease"])
                and _same_card_state(card, journal.get("post_card", {}))
            ):
                saved_state = copy.deepcopy(self.state)
                self.state.pop("regrade_recovery", None)
                self.state.pop("instant_undo", None)
                try:
                    self._save()
                except Exception:
                    self.state = saved_state
                    raise
                self.recovery_blocked = False
                return
        except Exception as exc:
            print(f"{PACKAGE_NAME}: unable to recover interrupted grade change: {exc}")
            self._notify(
                trf(
                    "中断恢复核对失败，插件自动处理已暂停：{error}",
                    "Could not verify interrupted recovery; automatic processing is paused: {error}",
                    error=exc,
                ),
                error=True,
            )
            self._schedule_recovery_sync_retry()
            return
        if (
            card
            and _same_card_state(card, journal.get("original_card", {}))
            and (
                latest is None
                or int(latest["id"]) < int(journal["post_revlog"]["id"])
            )
        ):
            self._resume_interrupted_regrade(journal)
            return
        self._notify(
            tr(
                "发现未完成的改分恢复，状态不匹配；插件自动处理已暂停",
                "An unfinished rating recovery was found, but its state does not match; automatic processing is paused.",
            ),
            error=True,
        )

    def _schedule_recovery_sync_retry(self) -> None:
        if self.recovery_sync_retry_count >= 3:
            return
        self.recovery_sync_retry_count += 1
        from aqt.qt import QTimer

        QTimer.singleShot(1500, self._recover_interrupted_regrade)

    def _resume_interrupted_regrade(self, journal: dict[str, Any]) -> None:
        from anki.scheduler.v3 import CardAnswer
        from aqt.operations import CollectionOp

        answer = CardAnswer.FromString(
            base64.b64decode(journal["original_answer_b64"])
        )

        def regrade(col: Any) -> Any:
            card = _row_by_id_from_collection(col, "cards", int(journal["card_id"]))
            if not card or not _same_card_state(card, journal["original_card"]):
                raise RuntimeError(tr("卡片在恢复前又发生变化", "The card changed again before recovery."))
            return col.sched.answer_card(answer)

        def success(_changes: Any) -> None:
            self._commit_regrade_state_sync(
                journal, tr("已完成中断的评分恢复", "Interrupted rating recovery completed.")
            )

        def failure(exc: Exception) -> None:
            self.recovery_blocked = True
            self._notify(
                trf(
                    "中断的评分恢复仍未完成：{error}",
                    "Interrupted rating recovery is still incomplete: {error}",
                    error=exc,
                ),
                error=True,
            )
            self._schedule_recovery_sync_retry()

        CollectionOp(self.mw, regrade).success(success).failure(failure).run_in_background(
            initiator=self,
        )

    def _clear_space_input(self) -> None:
        self.space_pending = None
        self.space_answer = None

    def _space_shortcut_is_unambiguous(self, expected_count: int = 1) -> bool:
        from aqt.qt import QAction, QKeySequence, QShortcut

        try:
            target = QKeySequence("Space")
            shortcuts = [
                shortcut
                for shortcut in self.mw.findChildren(QShortcut)
                if shortcut.isEnabled() and shortcut.key() == target
            ]
            actions = [
                action
                for action in self.mw.findChildren(QAction)
                if action.isEnabled()
                and action.isVisible()
                and target in action.shortcuts()
            ]
            return len(shortcuts) == expected_count and not actions
        except Exception:
            return False

    def _warn_space_binding(self) -> None:
        if not getattr(self, "space_binding_warning", False):
            self.space_binding_warning = True
            self._notify(
                tr(
                    "无法安全识别空格答题，一阅自动改分暂不可用",
                    "Space-key answers could not be identified safely; Oneki automatic rating is unavailable.",
                ),
                error=True,
            )

    def _space_shortcut(self, reviewer: Any, original: Any) -> None:
        if (
            not self.enabled()
            or getattr(self.mw, "state", None) != "review"
            or getattr(self.mw, "reviewer", None) is not reviewer
            or getattr(reviewer, "state", None) != "answer"
        ):
            original()
            return
        try:
            if not self.mw.pm.spacebar_rates_card():
                original()
                return
            card = reviewer.card
        except Exception:
            original()
            return
        if card is None or not self._space_shortcut_is_unambiguous():
            self._warn_space_binding()
            original()
            return
        if getattr(self, "space_pending", None) is not None:
            return
        token = object()
        pending = (reviewer, card, token)
        self.space_pending = pending

        def answer_selected(value: Any) -> None:
            if getattr(self, "space_pending", None) is not pending:
                return
            self.space_pending = None
            if (
                getattr(self.mw, "state", None) != "review"
                or getattr(self.mw, "reviewer", None) is not reviewer
                or getattr(reviewer, "state", None) != "answer"
                or getattr(reviewer, "card", None) is not card
            ):
                return
            self.space_answer = pending
            try:
                reviewer._onAnswerButton(value)
            finally:
                if getattr(self, "space_answer", None) is pending:
                    self.space_answer = None

        def expire() -> None:
            if getattr(self, "space_pending", None) is pending:
                self.space_pending = None
                self._notify(
                    tr(
                        "空格答题未响应，请重试或手动选择评分",
                        "The space-key answer did not respond. Try again or choose a rating manually.",
                    ),
                    error=True,
                )

        try:
            from aqt.qt import QTimer

            reviewer.bottom.web.evalWithCallback("selectedAnswerButton()", answer_selected)
            QTimer.singleShot(3000, expire)
        except Exception as exc:
            if getattr(self, "space_pending", None) is pending:
                self.space_pending = None
            self._notify(
                trf(
                    "空格答题未完成，请重试或手动选择评分：{error}",
                    "The space-key answer did not complete. Try again or choose a rating manually: {error}",
                    error=exc,
                ),
                error=True,
            )

    def _install_space_shortcut(self, shortcuts: list[tuple]) -> None:
        from aqt.qt import QKeySequence

        reviewer = getattr(self.mw, "reviewer", None)
        original = getattr(reviewer, "onEnterKey", None)
        if not callable(original):
            return
        target = QKeySequence("Space")
        try:
            matches = [
                index
                for index, value in enumerate(shortcuts)
                if value and QKeySequence(value[0]) == target
            ]
        except (TypeError, ValueError):
            self._warn_space_binding()
            return
        if len(matches) != 1 or shortcuts[matches[0]][1] != original:
            self._warn_space_binding()
            return
        if not self._space_shortcut_is_unambiguous(expected_count=0):
            self._warn_space_binding()
            return
        index = matches[0]
        key = shortcuts[index][0]
        shortcuts[index] = (
            key,
            lambda: self._space_shortcut(reviewer, original),
        )
        self.space_binding_warning = False

    def reviewer_shortcuts_will_change(self, state: str, shortcuts: list[tuple]) -> None:
        if state != "review":
            return
        self._install_space_shortcut(shortcuts)
        shortcut = str(self.config().get("instant_undo_shortcut", "Z")).strip()
        self.shortcut_conflict = False
        if not shortcut:
            self._update_review_shortcut_script()
            return
        from aqt.qt import QKeySequence

        sequence = QKeySequence(shortcut)
        if sequence.isEmpty():
            self.shortcut_conflict = True
            if self.last_shortcut_conflict != shortcut:
                self.last_shortcut_conflict = shortcut
                self._notify(
                    trf(
                        "撤销快捷键 {shortcut} 无效，请在设置中重新选择",
                        "Undo shortcut {shortcut} is invalid. Choose another in Settings.",
                        shortcut=shortcut,
                    ),
                    error=True,
                )
            self._update_review_shortcut_script()
            return
        target = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        existing = {
            QKeySequence(value[0]).toString(QKeySequence.SequenceFormat.PortableText)
            for value in shortcuts
            if value
        }
        existing.update(self._main_window_shortcuts())
        if target in existing:
            self.shortcut_conflict = True
            if self.last_shortcut_conflict != shortcut:
                self.last_shortcut_conflict = shortcut
                self._notify(
                    trf(
                        "撤销快捷键 {shortcut} 已被占用，请在设置中更换",
                        "Undo shortcut {shortcut} is already in use. Choose another in Settings.",
                        shortcut=shortcut,
                    ),
                    error=True,
                )
        else:
            self.last_shortcut_conflict = None
        self._update_review_shortcut_script()

    def _main_window_shortcuts(self) -> set[str]:
        from aqt.qt import QAction, QKeySequence, QShortcut

        values: set[str] = set()
        for shortcut in self.mw.findChildren(QShortcut):
            value = shortcut.key().toString(QKeySequence.SequenceFormat.PortableText)
            if value:
                values.add(value)
        for action in self.mw.findChildren(QAction):
            for sequence in action.shortcuts():
                value = sequence.toString(QKeySequence.SequenceFormat.PortableText)
                if value:
                    values.add(value)
        return values

    def native_undo_occurred(self, _changes: Any) -> None:
        if not self.undo_in_progress:
            self._invalidate_instant_undo(
                tr("Anki 撤销或重做后，撤销改分入口已失效", "Undo for the rating change is no longer available after an Anki undo or redo.")
            )
            try:
                self._reconcile_review_history()
            except Exception as exc:
                self.recovery_blocked = True
                self._log_background(
                    trf(
                        "撤销后的复习记录同步失败，自动处理已暂停：{error}",
                        "Could not sync review history after undo; automatic processing is paused: {error}",
                        error=exc,
                    ),
                    exc,
                )

    def _reconcile_review_history(self, batch_key: str | None = None) -> None:
        changed = False
        timing_status: dict[str, bool] = {}
        initial_card_ids: set[int] = set()
        for key, batch in self.state.get("batches", {}).items():
            if batch_key is not None and key != batch_key:
                continue
            replaced_ids = set(batch.get("replaced_attempt_ids", []))
            for value in batch.get("attempts", []):
                runtime = RuntimeAttempt.from_dict(value)
                attempt_id = runtime.attempt.attempt_id
                original = runtime.original_revlog
                if original is None:
                    continue
                current = self._row_by_id("revlog", int(original["id"]))
                if attempt_id in replaced_ids:
                    review_undone = current is None or not _already_replaced(
                        current, original
                    )
                else:
                    review_undone = current is None or any(
                        current.get(field) != original.get(field)
                        for field in READ_COLUMNS["revlog"]
                        if field != "usn"
                    )
                if runtime.review_undone == review_undone:
                    continue
                value["review_undone"] = review_undone
                timing_status[attempt_id] = review_undone
                changed = True
                if runtime.initial_new_learning:
                    initial_card_ids.add(runtime.attempt.card_id)
                if batch.get("daily_counted", False):
                    day = str(batch["day"])
                    self._daily_stats(day)
                    daily = self.state["daily"][day]
                    delta = -1 if review_undone else 1
                    daily["reviewed_total"] += delta
                    if attempt_id in batch.get("decision", {}).get("candidates", []):
                        daily["eligible_total"] += delta
                    if runtime.instant_upgraded or attempt_id in replaced_ids:
                        daily["auto_upgraded"] += delta
        for sample in self.state.get("timing_history", []):
            attempt_id = sample.get("attempt_id")
            if attempt_id in timing_status:
                sample["review_undone"] = timing_status[attempt_id]
        for card_id in initial_card_ids:
            current_card = self._row_by_id("cards", card_id)
            initial_cards = self.state.setdefault("initial_new_learning", {})
            if current_card and int(current_card.get("type", -1)) == 1:
                initial_cards[str(card_id)] = True
            else:
                initial_cards.pop(str(card_id), None)
        if changed:
            self._save()

    def handle_regrade_shortcut(
        self, handled: tuple[bool, Any], message: str, context: Any
    ) -> tuple[bool, Any]:
        from aqt.reviewer import Reviewer

        if message == "batch-auto-easy-undo" and isinstance(context, Reviewer):
            self.undo_instant_regrade()
            return (True, None)
        return handled

    def operation_did_execute(self, _changes: Any, initiator: Any) -> None:
        if initiator is self:
            return
        entry = self.state.get("instant_undo")
        if entry and not self.undo_in_progress:
            try:
                if self.mw.col.undo_status().last_step != int(
                    entry["reviewer_undo_step"]
                ):
                    self._invalidate_instant_undo(
                        tr("Anki 中发生了其他操作，撤销改分入口已失效", "Undo for the rating change is no longer available after another Anki action.")
                    )
            except Exception:
                self._invalidate_instant_undo(
                    tr("无法确认 Anki 操作状态，撤销改分入口已失效", "Undo for the rating change is no longer available because the Anki action state could not be confirmed.")
                )

    def _append_timing_sample(self, sample: dict[str, Any]) -> None:
        history = self.state.setdefault("timing_history", [])
        history.append(dict(sample))
        del history[:-100]
        self._save()

    def show_timing_history(self, parent: Any = None) -> None:
        from aqt.qt import (
            QAbstractItemView,
            QDialog,
            QLabel,
            QPushButton,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
        )

        history = [
            sample
            for sample in self.state.get("timing_history", [])
            if not sample.get("review_undone", False)
        ][-100:]
        dialog = QDialog(parent or self.mw)
        dialog.setWindowTitle(tr("最近100张答题计时", "Last 100 Review Timings"))
        dialog.setMinimumSize(1000, 600)
        layout = QVBoxLayout(dialog)
        summary_label = QLabel(_timing_history_summary(history))
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        headers = [
            tr("时间", "Time"),
            tr("卡片编号", "Card ID"),
            tr("牌组编号", "Deck ID"),
            tr("用户评分", "Your Rating"),
            tr("最终评分", "Final Rating"),
            tr("题目到答案（慢答判定）", "Question to Answer (Slow Answer Check)"),
            tr("题目到评分", "Question to Rating"),
            tr("答案到评分", "Answer to Rating"),
            tr("最大答题秒数", "Maximum Answer Time"),
            tr("总用时触及上限", "Total Time Reached Limit"),
        ]
        table = QTableWidget(len(history), len(headers), dialog)
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        for row_index, sample in enumerate(reversed(history)):
            answered_at = datetime.fromtimestamp(
                int(sample["answered_at_ms"]) / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
            hit_max = sample.get("hit_max_answer_time")
            values = (
                answered_at,
                str(sample.get("card_id", "")),
                str(sample.get("deck_id", "")),
                _grade_label(int(sample.get("user_ease", 0))) if int(sample.get("user_ease", 0)) in {1, 2, 3, 4} else tr("未知", "Unknown"),
                _grade_label(int(sample.get("effective_ease", 0))) if int(sample.get("effective_ease", 0)) in {1, 2, 3, 4} else tr("未知", "Unknown"),
                _format_timing_ms(sample.get("question_to_answer_ms")),
                _format_timing_ms(sample.get("question_to_grade_ms")),
                _format_timing_ms(sample.get("answer_to_grade_ms")),
                _format_timing_ms(sample.get("max_answer_ms")),
                tr("是", "Yes") if hit_max is True else tr("否", "No") if hit_max is False else tr("未知", "Unknown"),
            )
            for column, value in enumerate(values):
                table.setItem(row_index, column, QTableWidgetItem(value))

        table.resizeColumnsToContents()
        layout.addWidget(table)
        close_button = QPushButton(tr("关闭", "Close"), dialog)
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()

    def will_end(self) -> None:
        if not self.enabled() or self.finalizing or self.recovery_blocked:
            return
        if not self._reviewer_queue_empty():
            self._persist_active_batch()
            self.review_session_id = None
            return
        session_id = self.review_session_id
        if session_id:
            fixed_mode = self.rule_config().strategy == "fixed"
            batch_keys = [
                key
                for key, batch in self.state.get("batches", {}).items()
                if batch.get("session_id") == session_id
                and batch.get("status") != "finalized"
            ]
            for batch_key in batch_keys:
                self.finalize(batch_key, notify=not fixed_mode)
            if fixed_mode and batch_keys and all(
                self.state["batches"][key].get("status") == "finalized"
                for key in batch_keys
            ):
                self._notify(_completed_regrade_summary(self._session_attempts(session_id)))
        self.review_session_id = None

    def _session_attempts(self, session_id: str) -> list[RuntimeAttempt]:
        return [
            runtime
            for batch in self.state.get("batches", {}).values()
            if batch.get("session_id") == session_id
            for runtime in _active_batch_attempts(batch)
        ]

    def finalize(self, batch_key: str, *, notify: bool = True) -> None:
        batch = self.state.get("batches", {}).get(batch_key)
        if not batch or batch.get("status") == "finalized":
            return
        self.finalizing = True
        try:
            config = self.rule_config()
            self._reconcile_review_history(batch_key)
            if self.state.get("instant_undo", {}).get("batch_key") == batch_key:
                self._invalidate_instant_undo(
                    None
                    if config.strategy == "fixed"
                    else tr(
                        "本轮复习已结束，撤销改分入口已失效",
                        "Undo for the rating change is no longer available because this review has ended.",
                    )
                )
            attempts = _active_batch_attempts(batch)
            if not attempts:
                batch["status"] = "finalized"
                self._save()
                return
            auto_easy_enabled = bool(self.config().get("auto_easy_enabled", True))
            day = str(batch["day"])
            daily = self._daily_stats(day)
            history_window = config.history_window
            pending_elsewhere = self._pending_daily_stats(
                day,
                history_window,
                exclude_batch_key=batch_key,
            )
            instant_upgraded = [
                value
                for value in attempts
                if value.instant_upgraded
            ]
            instant_count = len(instant_upgraded)
            instant_candidate_ids = {
                value.attempt.attempt_id
                for value in attempts
                if value.instant_candidate
            }
            recorded_batch_candidates = eligible_attempts(
                [
                    value.attempt
                    for value in attempts
                    if value.attempt.batch_auto_easy_eligible
                ],
                history_window,
            )
            batch_candidates = (
                recorded_batch_candidates if auto_easy_enabled else []
            )
            batch_candidate_ids = {
                value.attempt_id for value in recorded_batch_candidates
            }
            decision_daily = DailyStats(
                auto_upgraded=(
                    daily.auto_upgraded
                    + pending_elsewhere.auto_upgraded
                    + instant_count
                ),
                eligible_total=(
                    daily.eligible_total
                    + pending_elsewhere.eligible_total
                    + len(instant_candidate_ids - batch_candidate_ids)
                ),
                reviewed_total=(
                    daily.reviewed_total + pending_elsewhere.reviewed_total
                ),
            )
            decision = evaluate_batch(
                batch_candidates,
                batch_card_count=len({value.attempt.card_id for value in attempts}),
                daily_stats=decision_daily,
                config=config,
                reviewed_event_count=len(attempts),
            )

            selected = {
                attempt_id
                for attempt_id in decision.selected_attempt_ids
            }
            replaced_ids = set(batch.get("replaced_attempt_ids", []))
            regrade_failure = None
            regrade_skip = None
            replaced: list[str] = []
            if config.strategy != "fixed" and selected:
                session_id = str(batch.get("session_id") or "")
                session_attempts = (
                    self._session_attempts(session_id)
                    if session_id
                    else attempts
                )
                session_attempts.sort(
                    key=lambda value: value.reviewer_undo_step
                    if value.reviewer_undo_step is not None
                    else -1
                )
                previously_replaced = {
                    attempt_id
                    for other_batch in self.state.get("batches", {}).values()
                    if session_id
                    and other_batch.get("session_id") == session_id
                    for attempt_id in other_batch.get("replaced_attempt_ids", [])
                }
                records = [value.replacement_record() for value in session_attempts]
                self.undo_in_progress = True
                try:
                    result = AnkiReplacementAdapter(self.mw.col).replace_many(
                        records,
                        selected_attempt_ids=(
                            previously_replaced | selected | replaced_ids
                        ),
                    )
                finally:
                    self.undo_in_progress = False
                replaced = sorted(set(result.replaced_attempt_ids) & selected)
                regrade_failure = result.failure
                regrade_skip = result.skipped_reason
                if result.undo_steps:
                    step_by_id = result.undo_steps
                    for other_batch in self.state.get("batches", {}).values():
                        if session_id and other_batch.get("session_id") != session_id:
                            continue
                        for attempt_value in other_batch.get("attempts", []):
                            attempt_id = attempt_value.get("attempt", {}).get(
                                "attempt_id"
                            )
                            if attempt_id in step_by_id:
                                attempt_value["reviewer_undo_step"] = step_by_id[
                                    attempt_id
                                ]
            if not batch.get("daily_counted", False):
                self._update_daily_denominators(
                    day,
                    eligible_total=len(batch_candidate_ids | instant_candidate_ids),
                    reviewed_total=len(attempts),
                )
                batch["daily_counted"] = True
            batch["replaced_attempt_ids"] = sorted(
                replaced_ids.union(replaced)
            )
            for sample in self.state.get("timing_history", []):
                if sample.get("attempt_id") in batch["replaced_attempt_ids"]:
                    sample["effective_ease"] = 4
            self._increment_daily_upgraded(day, len(replaced) + instant_count)
            batch["status"] = "finalized"
            batch.pop("last_error", None)
            batch["decision"] = {
                "selected": list(
                    sorted(
                        set(decision.selected_attempt_ids)
                        | {
                            value.attempt.attempt_id
                            for value in instant_upgraded
                        }
                    )
                ),
                "candidates": sorted(
                    batch_candidate_ids | instant_candidate_ids
                ),
                "not_applicable": decision.not_applicable,
                "reason": decision.reason,
            }
            if regrade_failure:
                batch["last_error"] = regrade_failure
            elif regrade_skip:
                batch["regrade_skip_reason"] = regrade_skip
            else:
                batch.pop("regrade_skip_reason", None)
            self._save()
            if notify and (regrade_failure or regrade_skip):
                reason = regrade_failure or regrade_skip
                self._log_background(
                    trf(
                        "本轮自动改分未完成，原评分和 Anki 撤销记录已保留：{reason}",
                        "Automatic rating changes were not completed for this review. The original ratings and Anki undo history were kept: {reason}",
                        reason=reason,
                    )
                )
            elif notify:
                if config.strategy == "fixed":
                    self._notify(_completed_regrade_summary(attempts))
                elif replaced:
                    self._notify(
                        trf(
                            "复习结束：已将 {count} 张卡的评分由良好调整为简单",
                            "Review complete: changed {count} {card_word} from Good to Easy.",
                            count=len(replaced),
                            card_word="card" if len(replaced) == 1 else "cards",
                        )
                    )
                else:
                    self._notify(decision.reason)
        except Exception as exc:
            batch["last_error"] = str(exc)
            recovery_failed = isinstance(exc, AdapterRecoveryError)
            if recovery_failed:
                self.recovery_blocked = True
                self._set_review_shortcut_armed(False)
            self._save()
            if recovery_failed:
                message = trf(
                    "复习评分失败且未能恢复原记录，自动处理已暂停：{error}",
                    "Review rating failed and the original records could not be restored; automatic processing is paused: {error}",
                    error=exc,
                )
            else:
                message = (
                    trf(
                        "本轮答题评分已生效，但复习汇总失败：{error}",
                        "Ratings applied while answering, but the review summary failed: {error}",
                        error=exc,
                    )
                    if self.rule_config().strategy == "fixed"
                    else trf(
                        "这轮复习处理失败：{error}",
                        "This review could not be processed: {error}",
                        error=exc,
                    )
                )
            self._notify(message, error=True)
        finally:
            self.finalizing = False

    def _capture_attempt(
        self,
        reviewer: Any,
        card: Any,
        ease: int,
        elapsed_ms: int,
        *,
        elapsed_known: bool = True,
        question_to_answer_ms: int | None,
        max_answer_ms: int | None,
        input_source: str = "unknown",
    ) -> tuple[RuntimeAttempt, int]:
        now_ms = int(time.time() * 1000)
        card_id = int(card.id)
        deck_id = int(card.did)
        day = date.today().isoformat()
        if self.review_session_id is None:
            self.review_session_id = uuid.uuid4().hex
        self._ensure_batch(f"{day}:{deck_id}", day, deck_id)
        batch_key = self.current_batch_key
        if batch_key is None:
            raise RuntimeError("unable to create a review batch")
        self.state["batches"][batch_key]["session_id"] = self.review_session_id
        sequence = len(self.state["batches"][batch_key].get("attempts", []))
        attempt_id = f"{batch_key}:{sequence}:{card_id}:{now_ms}"

        states = reviewer._v3.states
        from anki.scheduler.v3 import CardAnswer

        good_answer = self.mw.col.sched.build_answer(
            card=card,
            states=states,
            rating=CardAnswer.GOOD,
        )
        easy_answer = self.mw.col.sched.build_answer(
            card=card,
            states=states,
            rating=CardAnswer.EASY,
        )
        original_ease = int(ease)
        original_rating = {
            1: CardAnswer.AGAIN,
            2: CardAnswer.HARD,
            3: CardAnswer.GOOD,
            4: CardAnswer.EASY,
        }.get(original_ease)
        original_answer = (
            self.mw.col.sched.build_answer(
                card=card,
                states=states,
                rating=original_rating,
            )
            if original_rating is not None
            else None
        )
        is_preview = _is_unrescheduled_preview(good_answer.new_state)
        config = self.config()
        answer_time_known = question_to_answer_ms is not None
        answer_time_ms = question_to_answer_ms or 0
        answer_time_limit_ms = max_answer_ms or 0
        auto_hard_candidate = (
            bool(config.get("enabled", True))
            and bool(config.get("auto_hard_enabled", False))
            and input_source == "space"
            and not is_preview
            and answer_time_known
            and original_ease == 3
            and _auto_hard_threshold_ms(
                config, anki_max_taken_ms=answer_time_limit_ms
            ) > 0
            and answer_time_ms >= _auto_hard_threshold_ms(
                config, anki_max_taken_ms=answer_time_limit_ms
            )
        )
        timeout_ease = None
        timeout_unknown = False
        if auto_hard_candidate:
            try:
                cutoff = int(self.mw.col.sched.day_cutoff)
                timeout_ease = _timeout_grade(states, now_ms / 1000, cutoff)
            except (AttributeError, TypeError, ValueError):
                pass
            timeout_unknown = timeout_ease is None

        rule_config = self.rule_config()
        history_window = rule_config.history_window
        original_card = self._row_by_id("cards", card_id) or {}
        card_type = int(original_card.get("type", -1))
        prior, prior_passes, has_prior_pass = self._pass_history(
            card_id,
            history_window,
            current_card_type=card_type,
        )
        pure = Attempt(
            attempt_id=attempt_id,
            card_id=card_id,
            deck_id=deck_id,
            sequence=sequence,
            original_ease=original_ease,
            elapsed_ms=elapsed_ms,
            prior_ratings=prior,
            leaves_today_learning=_leaves_today_learning(good_answer.new_state),
            answered_at_ms=now_ms,
            prior_pass_ratings=tuple(prior_passes),
            pass_history_known=True,
            elapsed_known=elapsed_known,
        )
        initial_cards = self.state.setdefault("initial_new_learning", {})
        is_initial_learning = not is_preview and not has_prior_pass and (
            card_type == 0
            or (card_type == 1 and bool(initial_cards.get(str(card_id))))
        )
        automatic_allowed = (
            bool(config.get("enabled", True))
            and input_source == "space"
            and not is_preview
            and not timeout_unknown
        )
        auto_easy_enabled = bool(config.get("auto_easy_enabled", True))
        instant_candidate = (
            automatic_allowed
            and auto_easy_enabled
            and should_auto_upgrade_now(
                pure,
                rule_config,
                is_initial_learning=is_initial_learning,
            )
        )
        batch_auto_easy_eligible = (
            automatic_allowed
            and auto_easy_enabled
            and not (
                rule_config.strategy == "fixed"
                and is_initial_learning
                and original_ease == 3
                and prior.count(3) >= 2
            )
        )
        fixed_candidate = (
            rule_config.strategy == "fixed"
            and batch_auto_easy_eligible
            and (
                instant_candidate
                or (
                    pure.is_valid_candidate
                    and history_allows(pure, history_window)
                )
            )
        )
        instant_upgrade_allowed = instant_candidate and self._instant_quota_allows(
            rule_config, day
        )
        effective_ease = _resolve_auto_grade(
            original_ease,
            auto_hard_candidate=timeout_ease,
            auto_easy_candidate=instant_upgrade_allowed,
            priority=str(config["auto_grade_priority"]),
        )
        instant_upgraded = effective_ease == 4 and instant_upgrade_allowed
        tracked_instant_candidate = (
            fixed_candidate and effective_ease not in (1, 2)
        )
        pure = replace(
            pure,
            original_ease=effective_ease,
            batch_auto_easy_eligible=(
                batch_auto_easy_eligible and rule_config.strategy != "fixed"
            ),
        )
        return RuntimeAttempt(
            attempt=pure,
            original_card=original_card,
            post_card=None,
            original_revlog=None,
            easy_answer_b64=base64.b64encode(
                easy_answer.SerializeToString()
            ).decode("ascii"),
            unrescheduled_preview=is_preview,
            initial_new_learning=is_initial_learning,
            instant_candidate=tracked_instant_candidate,
            instant_upgraded=instant_upgraded,
            user_ease=original_ease,
            input_source=input_source,
            original_answer_b64=(
                base64.b64encode(original_answer.SerializeToString()).decode("ascii")
                if original_answer is not None
                else ""
            ),
        ), effective_ease

    def _ensure_batch(self, batch_key: str, day: str, deck_id: int) -> None:
        if (
            self.current_batch_key == batch_key
            and self.state["batches"].get(batch_key, {}).get("status") != "finalized"
        ):
            return
        batches = self.state.setdefault("batches", {})
        open_batches = [
            (key, value)
            for key, value in batches.items()
            if value.get("day") == day
            and int(value.get("deck_id", -1)) == deck_id
            and value.get("status") != "finalized"
        ]
        if open_batches:
            self.current_batch_key = max(
                open_batches,
                key=lambda item: _batch_number(item[0]),
            )[0]
            return
        prefix = f"{day}:{deck_id}:"
        counters = [
            int(key.rsplit(":", 1)[-1])
            for key in batches
            if key.startswith(prefix) and key.rsplit(":", 1)[-1].isdigit()
        ]
        next_number = max(counters, default=-1) + 1
        self.current_batch_key = f"{prefix}{next_number}"
        batches[self.current_batch_key] = {
            "day": day,
            "deck_id": deck_id,
            "status": "open",
            "attempts": [],
            "daily_counted": False,
            "replaced_attempt_ids": [],
            "session_id": None,
        }

    def _store_attempt(self, attempt: RuntimeAttempt) -> None:
        batch_key = self.current_batch_key
        if batch_key is None:
            return
        self.state["batches"][batch_key].setdefault("attempts", []).append(
            attempt.to_dict()
        )

    def _persist_active_batch(self) -> None:
        if self.current_batch_key:
            self._save()

    def _pass_history(
        self,
        card_id: int,
        history_window: int,
        *,
        current_card_type: int,
    ) -> tuple[tuple[int, ...], list[tuple[int, ...]], bool]:
        rows = self.mw.col.db.all(
            "select ease, ivl, type from revlog "
            "where cid=? and type in (0, 1, 2, 3) order by id",
            card_id,
        )
        completed_passes: list[tuple[int, ...]] = []
        current_pass: list[int] = []
        for row in rows:
            rating = int(row[0])
            interval = int(row[1])
            review_type = int(row[2])
            if review_type == 3:
                continue
            if review_type not in {0, 1, 2}:
                continue
            if review_type == 1 and current_pass:
                completed_passes.append(tuple(current_pass))
                current_pass = []
            current_pass.append(rating)
            # Review answers finish a pass immediately; learning/relearning
            # intervals may cross the day boundary without graduating the card.
            if review_type == 1 and rating in (2, 3, 4) and interval > 0:
                completed_passes.append(tuple(current_pass))
                current_pass = []

        if current_card_type in {0, 2} and current_pass:
            completed_passes.append(tuple(current_pass))
            current_pass = []

        previous_count = max(0, history_window - 1)
        if previous_count:
            previous_passes = list(reversed(completed_passes[-previous_count:]))
        else:
            previous_passes = []
        return tuple(current_pass), previous_passes, bool(completed_passes)

    def _pending_daily_stats(
        self,
        day: str,
        history_window: int,
        *,
        exclude_batch_key: str | None = None,
    ) -> DailyStats:
        pending_auto_upgraded = 0
        pending_eligible = 0
        pending_reviewed = 0
        for batch_key, batch in self.state.get("batches", {}).items():
            if (
                batch_key == exclude_batch_key
                or str(batch.get("day")) != day
                or batch.get("daily_counted", False)
            ):
                continue
            attempts = _active_batch_attempts(batch)
            pending_auto_upgraded += sum(
                value.instant_upgraded for value in attempts
            )
            pending_reviewed += len(attempts)
            candidates = eligible_attempts(
                [
                    value.attempt
                    for value in attempts
                    if value.attempt.batch_auto_easy_eligible
                ],
                history_window,
            )
            eligible_ids = {value.attempt_id for value in candidates}
            eligible_ids.update(
                value.attempt.attempt_id
                for value in attempts
                if value.instant_candidate
            )
            pending_eligible += len(eligible_ids)
        return DailyStats(
            auto_upgraded=pending_auto_upgraded,
            eligible_total=pending_eligible,
            reviewed_total=pending_reviewed,
        )

    def _instant_quota_allows(
        self,
        config: RuleConfig,
        day: str,
    ) -> bool:
        if not config.quota_enabled:
            return True
        pending = self._pending_daily_stats(day, config.history_window)
        return quota_allows_increment(
            self._daily_stats(day),
            config,
            current_reviewed_total=pending.reviewed_total + 1,
            current_eligible_total=pending.eligible_total + 1,
            current_upgraded_total=pending.auto_upgraded,
        )

    def _latest_revlog(self, card_id: int) -> dict[str, Any] | None:
        columns = READ_COLUMNS["revlog"]
        row = self.mw.col.db.first(
            f"select {', '.join(columns)} from revlog where cid=? order by id desc limit 1",
            card_id,
        )
        return dict(zip(columns, row)) if row else None

    def _row_by_id(self, table: str, row_id: int) -> dict[str, Any] | None:
        columns = READ_COLUMNS[table]
        row = self.mw.col.db.first(
            f"select {', '.join(columns)} from {table} where id=?",
            row_id,
        )
        return dict(zip(columns, row)) if row else None

    def _reviewer_queue_empty(self) -> bool:
        try:
            return not self.mw.col.sched.get_queued_cards(fetch_limit=1).cards
        except Exception:
            return False

    def _daily_stats(self, day: str) -> DailyStats:
        value = self.state.setdefault("daily", {}).setdefault(
            day,
            {"auto_upgraded": 0, "eligible_total": 0, "reviewed_total": 0},
        )
        return DailyStats(
            auto_upgraded=int(value.get("auto_upgraded", 0)),
            eligible_total=int(value.get("eligible_total", 0)),
            reviewed_total=int(value.get("reviewed_total", 0)),
        )

    def _update_daily_denominators(
        self,
        day: str,
        *,
        eligible_total: int,
        reviewed_total: int,
    ) -> None:
        value = self.state.setdefault("daily", {}).setdefault(
            day,
            {"auto_upgraded": 0, "eligible_total": 0, "reviewed_total": 0},
        )
        value["eligible_total"] = int(value.get("eligible_total", 0)) + eligible_total
        value["reviewed_total"] = int(value.get("reviewed_total", 0)) + reviewed_total

    def _increment_daily_upgraded(self, day: str, count: int) -> None:
        value = self.state["daily"][day]
        value["auto_upgraded"] = int(value.get("auto_upgraded", 0)) + count

    def _save(self) -> None:
        self.state_store.save(self.state)

    def _notify(self, message: str, error: bool = False) -> None:
        try:
            from aqt.utils import showWarning, tooltip

            if error:
                showWarning(message)
            else:
                tooltip(message)
        except Exception:
            print(f"{PACKAGE_NAME}: {message}")

    def _log_background(
        self, message: str, exc: BaseException | None = None
    ) -> None:
        """Record a diagnostic in Anki's add-on log without showing UI feedback."""
        level = logging.ERROR if exc is not None else logging.WARNING
        exc_info = (
            (type(exc), exc, exc.__traceback__) if exc is not None else None
        )
        try:
            from aqt.addons import AddonManager

            logger = AddonManager.get_logger(PACKAGE_NAME)
            logger.log(level, "%s", message, exc_info=exc_info)
            return
        except Exception as log_error:
            # Keep failure handling silent even when Anki's log file is unavailable.
            try:
                logging.getLogger(f"addon.{PACKAGE_NAME}").log(
                    level, "%s", message, exc_info=exc_info
                )
            except Exception:
                print(
                    f"{PACKAGE_NAME}: background logging failed: {log_error}; {message}",
                    file=sys.stderr,
                )

    def batch_preview(self, *, auto_easy_enabled: bool | None = None) -> str:
        if not self.current_batch_key:
            return tr("当前没有进行中的复习", "No review is currently in progress.")
        batch = self.state.get("batches", {}).get(self.current_batch_key)
        if not batch or batch.get("status") == "finalized":
            return tr("当前没有进行中的复习", "No review is currently in progress.")
        attempts = _active_batch_attempts(batch)
        config = self.rule_config()
        if auto_easy_enabled is None:
            auto_easy_enabled = bool(
                self.config().get("auto_easy_enabled", True)
            )
        if config.strategy == "fixed":
            if self.review_session_id:
                attempts = self._session_attempts(self.review_session_id)
            summary = trf(
                "已记录 {count} 次答题。{summary}",
                "Recorded {count} {answer_word}. {summary}",
                count=len(attempts),
                answer_word="answer" if len(attempts) == 1 else "answers",
                summary=_completed_regrade_summary(attempts),
            )
            if not auto_easy_enabled:
                return trf(
                    "{summary} 自动变简单已关闭，本轮不再自动改为‘简单’。",
                    "{summary} Automatic Easy ratings are disabled for this review.",
                    summary=summary,
                )
            day = date.today().isoformat()
            daily = self._daily_stats(day)
            pending = self._pending_daily_stats(day, config.history_window)
            decision = evaluate_batch(
                [],
                batch_card_count=0,
                daily_stats=DailyStats(
                    auto_upgraded=daily.auto_upgraded + pending.auto_upgraded,
                    eligible_total=daily.eligible_total + pending.eligible_total,
                    reviewed_total=daily.reviewed_total + pending.reviewed_total,
                ),
                config=config,
            )
            if decision.quota_remaining is None:
                quota_summary = tr(
                    "今天自动变简单不设上限。",
                    "There is no daily limit on Easy ratings.",
                )
            else:
                quota_summary = trf(
                    "今天还可以自动变简单 {count} 张。",
                    "Easy ratings remaining today: {count}.",
                    count=decision.quota_remaining,
                )
            return f"{summary} {quota_summary}"
        batch_candidates = [
            value.attempt
            for value in attempts
            if auto_easy_enabled and value.attempt.batch_auto_easy_eligible
        ]
        candidates = eligible_attempts(
            batch_candidates,
            config.history_window,
        )
        batch_candidate_ids = {value.attempt_id for value in candidates}
        instant_candidate_ids = {
            value.attempt.attempt_id
            for value in attempts
            if value.instant_candidate
        }
        instant_count = sum(value.instant_upgraded for value in attempts)
        if config.strategy == "smart":
            minimum = smart_minimum_size(config)
        else:
            minimum = minimum_batch_size(config.distribution_percent)
        day = date.today().isoformat()
        daily = self._daily_stats(day)
        pending_elsewhere = self._pending_daily_stats(
            day,
            config.history_window,
            exclude_batch_key=self.current_batch_key,
        )
        decision_daily = DailyStats(
            auto_upgraded=(
                daily.auto_upgraded
                + pending_elsewhere.auto_upgraded
                + instant_count
            ),
            eligible_total=(
                daily.eligible_total
                + pending_elsewhere.eligible_total
                + len(instant_candidate_ids - batch_candidate_ids)
            ),
            reviewed_total=daily.reviewed_total + pending_elsewhere.reviewed_total,
        )
        decision = evaluate_batch(
            batch_candidates,
            batch_card_count=len({value.attempt.card_id for value in attempts}),
            daily_stats=decision_daily,
            config=config,
            reviewed_event_count=len(attempts),
        )
        preview = trf(
            "已记录 {count} 次复习，符合条件 {eligible} 张；当前模式至少需要 {minimum} 张",
            "Recorded {count} {review_word}; {eligible} {eligible_word} qualify. This mode requires at least {minimum} {minimum_word}.",
            count=len(attempts),
            review_word="review" if len(attempts) == 1 else "reviews",
            eligible=len(batch_candidate_ids | instant_candidate_ids),
            eligible_word="card" if len(batch_candidate_ids | instant_candidate_ids) == 1 else "cards",
            minimum=minimum,
            minimum_word="card" if minimum == 1 else "cards",
        )
        if auto_easy_enabled:
            selected = len(decision.selected_attempt_ids) + instant_count
            if decision.quota_remaining is None:
                quota_summary = tr("今天自动变简单不设上限", "There is no daily limit on Easy ratings")
            else:
                quota_summary = trf(
                    "今天还可以自动变简单 {count} 张",
                    "Easy ratings remaining today: {count}",
                    count=decision.quota_remaining,
                )
            return trf(
                "{preview}；预计自动变简单 {selected} 张；{quota_summary}",
                "{preview} Estimated Easy rating changes: {selected}. {quota_summary}.",
                preview=preview,
                selected=selected,
                quota_summary=quota_summary,
            )
        return trf(
            "{preview}；自动变简单已关闭，本轮不再自动改为“简单”",
            "{preview} Automatic Easy ratings are disabled for this review.",
            preview=preview,
        )


def _leaves_today_learning(state: Any) -> bool:
    kind = state.WhichOneof("kind")
    if kind == "normal":
        normal_kind = state.normal.WhichOneof("kind")
        if normal_kind == "review":
            return True
        if normal_kind in {"learning", "relearning"}:
            return False
    if kind == "filtered":
        filtered_kind = state.filtered.WhichOneof("kind")
        if filtered_kind == "rescheduling":
            return _leaves_today_learning(state.filtered.rescheduling.original_state)
    return False


def _scheduled_today(state: Any, now: float, cutoff: int) -> bool | None:
    try:
        if now >= cutoff or now < 0:
            return None
        kind = state.WhichOneof("kind")
        if kind == "filtered":
            if state.filtered.WhichOneof("kind") != "rescheduling":
                return None
            normal = state.filtered.rescheduling.original_state
        elif kind == "normal":
            normal = state.normal
        else:
            return None
        normal_kind = normal.WhichOneof("kind")
        if normal_kind == "learning":
            seconds = int(normal.learning.scheduled_secs)
        elif normal_kind == "relearning":
            seconds = int(normal.relearning.learning.scheduled_secs)
        elif normal_kind == "review":
            return False if int(normal.review.scheduled_days) > 0 else None
        else:
            return None
        if seconds < 0:
            return None
        return now + seconds < cutoff
    except (AttributeError, TypeError, ValueError):
        return None


def _timeout_grade(states: Any, now: float, cutoff: int) -> int | None:
    try:
        hard_today = _scheduled_today(states.hard, now, cutoff)
        if hard_today is None:
            return None
        if hard_today:
            return 2
        if _scheduled_today(states.again, now, cutoff) is not None:
            return 1
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _is_unrescheduled_preview(state: Any) -> bool:
    if state.WhichOneof("kind") != "filtered":
        return False
    return state.filtered.WhichOneof("kind") == "preview"


def _read_card_time_taken(card: Any) -> int | None:
    try:
        return max(0, int(card.time_taken(capped=False)))
    except Exception:
        return None


def _read_card_time_limit(card: Any) -> int | None:
    try:
        return max(0, int(card.time_limit()))
    except Exception:
        return None


def _timing_sample(
    *,
    answered_at_ms: int,
    card_id: int,
    deck_id: int,
    user_ease: int,
    effective_ease: int,
    question_elapsed_ms: int | None,
    answer_elapsed_ms: int | None,
    max_answer_ms: int | None,
    question_to_answer_ms: int | None = None,
    input_source: str = "unknown",
) -> dict[str, Any]:
    hit_max_answer_time = None
    if question_elapsed_ms is not None and max_answer_ms is not None:
        hit_max_answer_time = (
            max_answer_ms > 0 and question_elapsed_ms >= max_answer_ms
        )
    return {
        "answered_at_ms": answered_at_ms,
        "card_id": card_id,
        "deck_id": deck_id,
        "user_ease": user_ease,
        "effective_ease": effective_ease,
        "input_source": input_source,
        "question_to_answer_ms": question_to_answer_ms,
        "question_to_grade_ms": question_elapsed_ms,
        "answer_to_grade_ms": answer_elapsed_ms,
        "max_answer_ms": max_answer_ms,
        "hit_max_answer_time": hit_max_answer_time,
    }


def _format_timing_ms(value: Any) -> str:
    if value is None:
        return tr("未知", "Unknown")
    return trf("{seconds:.2f} 秒", "{seconds:.2f} s", seconds=int(value) / 1000)


def _timing_history_summary(history: list[dict[str, Any]]) -> str:
    history = [sample for sample in history if not sample.get("review_undone", False)]

    def summary_for_grade(grade: int, label: str) -> str:
        matching = [
            sample
            for sample in history
            if int(sample.get("effective_ease", 0)) == grade
        ]
        question_times = [
            int(sample["question_to_grade_ms"])
            for sample in matching
            if sample.get("question_to_grade_ms") is not None
        ]
        answer_times = [
            int(sample["answer_to_grade_ms"])
            for sample in matching
            if sample.get("answer_to_grade_ms") is not None
        ]
        hits = sum(sample.get("hit_max_answer_time") is True for sample in matching)
        avg_question = (
            trf(
                "{seconds:.2f} 秒",
                "{seconds:.2f} s",
                seconds=sum(question_times) / len(question_times) / 1000,
            )
            if question_times
            else tr("未知", "Unknown")
        )
        avg_answer = (
            trf(
                "{seconds:.2f} 秒",
                "{seconds:.2f} s",
                seconds=sum(answer_times) / len(answer_times) / 1000,
            )
            if answer_times
            else tr("未知", "Unknown")
        )
        return trf(
            "{label} {count} 张：题目到评分均值 {question}，答案到评分均值 {answer}，总用时触及最大答题秒数 {hits} 张。",
            "{label}: {count} {card_word}; average question-to-rating time {question}, answer-to-rating time {answer}; the maximum answer time was reached on {hits} {hit_word}.",
            label=label,
            count=len(matching),
            card_word="card" if len(matching) == 1 else "cards",
            question=avg_question,
            answer=avg_answer,
            hits=hits,
            hit_word="card" if hits == 1 else "cards",
        )

    return "\n".join(
        (
            trf(
                "最近记录 {count} 张（最多保存100张）。",
                "Recent records: {count} {card_word} (up to 100 are kept).",
                count=len(history),
                card_word="card" if len(history) == 1 else "cards",
            ),
            summary_for_grade(4, tr("最终简单", "Final Easy")),
            summary_for_grade(2, tr("最终困难", "Final Hard")),
        )
    )


def _batch_number(batch_key: str) -> int:
    suffix = batch_key.rsplit(":", 1)[-1]
    return int(suffix) if suffix.isdigit() else -1


def _auto_hard_threshold_ms(
    config: dict[str, Any],
    *,
    anki_max_taken_ms: int,
) -> int:
    if config.get("auto_hard_threshold_mode", "anki_max_taken") == "custom":
        return int(config.get("auto_hard_threshold_ms", 60_000))
    return anki_max_taken_ms


def _resolve_auto_grade(
    ease: int,
    *,
    auto_hard_candidate: int | None,
    auto_easy_candidate: bool,
    priority: str,
) -> int:
    if ease != 3:
        return ease
    if priority == "hard":
        if auto_hard_candidate:
            return int(auto_hard_candidate)
        if auto_easy_candidate:
            return 4
    else:
        if auto_easy_candidate:
            return 4
        if auto_hard_candidate:
            return int(auto_hard_candidate)
    return ease


def _state_path(mw: Any) -> Path:
    try:
        profile_folder = mw.pm.profileFolder()
    except Exception:
        profile_folder = None
    if profile_folder:
        return Path(profile_folder) / "anki_batch_auto_easy_state.json"
    return Path(__file__).with_name("batch_state.json")


def _row_by_id_from_collection(
    collection: Any, table: str, row_id: int
) -> dict[str, Any] | None:
    columns = READ_COLUMNS[table]
    row = collection.db.first(
        f"select {', '.join(columns)} from {table} where id=?",
        row_id,
    )
    return dict(zip(columns, row)) if row else None


def _same_card_state(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return current == expected


def _same_row(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return current == expected


def _answer_record_matches(row: dict[str, Any] | None, answer: Any, ease: int) -> bool:
    if not row:
        return False
    return (
        int(row.get("id", 0)) == int(answer.answered_at_millis)
        and int(row.get("ease", 0)) == int(ease)
        and int(row.get("time", -1)) == int(answer.milliseconds_taken)
    )


def _review_shortcut_script(shortcut: str, *, enabled: bool = True) -> str:
    from aqt.qt import QKeySequence

    clear_script = """
    (() => {
      window.__batchAutoEasyUndoArmed = false;
      const old = window.__batchAutoEasyUndoKeyHandler;
      if (old) document.removeEventListener('keydown', old, true);
      delete window.__batchAutoEasyUndoKeyHandler;
    })();
    """
    if not enabled or not shortcut:
        return clear_script
    parts = QKeySequence(shortcut).toString(
        QKeySequence.SequenceFormat.PortableText
    ).split("+")
    if not parts or not parts[-1]:
        return clear_script
    spec = json.dumps(
        {"key": parts[-1].lower(), "modifiers": [part.lower() for part in parts[:-1]]}
    )
    return f"""
    (() => {{
      const spec = {spec};
      window.__batchAutoEasyUndoArmed = false;
      const old = window.__batchAutoEasyUndoKeyHandler;
      if (old) document.removeEventListener('keydown', old, true);
      const handler = (event) => {{
        if (!window.__batchAutoEasyUndoArmed) return;
        const isEditor = (target) => target && (
          target.isContentEditable ||
          (typeof target.closest === 'function' && target.closest('input, textarea, select, [contenteditable=true]'))
        );
        if (isEditor(event.target) || isEditor(document.activeElement)) return;
        if (event.repeat || !event.key || event.key.toLowerCase() !== spec.key) return;
        const ctrl = event.ctrlKey || event.metaKey;
        if (ctrl !== (spec.modifiers.includes('ctrl') || spec.modifiers.includes('meta')) || event.altKey !== spec.modifiers.includes('alt') || event.shiftKey !== spec.modifiers.includes('shift')) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        pycmd('batch-auto-easy-undo');
      }};
      window.__batchAutoEasyUndoKeyHandler = handler;
      document.addEventListener('keydown', handler, true);
    }})();
    """


def _toggle_switch_type() -> type:
    from aqt.qt import QAbstractButton, QColor, QPainter, QRectF, Qt

    class ToggleSwitch(QAbstractButton):
        def __init__(self, accessible_name: str):
            super().__init__()
            self.setCheckable(True)
            self.setAccessibleName(accessible_name)
            self.setFixedSize(48, 28)
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        def paintEvent(self, _event: Any) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            track_color = "#34C759" if self.isChecked() else "#AEAEB2"
            if not self.isEnabled():
                track_color = "#D1D1D6"
            painter.setBrush(QColor(track_color))
            painter.drawRoundedRect(QRectF(1, 1, 46, 26), 13, 13)
            knob_x = 24 if self.isChecked() else 4
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QRectF(knob_x, 4, 20, 20))
            if self.hasFocus():
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor("#007AFF"))
                painter.drawRoundedRect(QRectF(1, 1, 46, 26), 13, 13)
            painter.end()

    return ToggleSwitch


def _right_align_form(form: Any) -> None:
    from aqt.qt import Qt

    form.setLabelAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    form.setFormAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    )


def _right_aligned_label(text: str, width: int) -> Any:
    from aqt.qt import QLabel, Qt

    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    label.setFixedWidth(width)
    return label


class SettingsDialog:
    def __init__(self, mw: Any, controller: BatchController):
        from aqt.qt import (
            QButtonGroup,
            QComboBox,
            QDialog,
            QDoubleSpinBox,
            QFormLayout,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QKeySequence,
            QKeySequenceEdit,
            QLabel,
            QPushButton,
            QRadioButton,
            QSizePolicy,
            QSpinBox,
            QStackedWidget,
            Qt,
            QVBoxLayout,
            QWidget,
        )

        ToggleSwitch = _toggle_switch_type()

        def switch_row(label_text: str, switch: Any, label_width: int) -> QWidget:
            row = QWidget()
            row.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(12)
            label = _right_aligned_label(label_text, label_width)
            label.setBuddy(switch)
            row_layout.addWidget(label)
            row_layout.addWidget(switch)
            row_layout.addStretch(1)
            return row

        def label_width(texts: list[str]) -> int:
            return max(QLabel(text).sizeHint().width() for text in texts) + 2

        class CurrentPageStack(QStackedWidget):
            def _fit_current_page(self) -> None:
                page = self.currentWidget()
                width = self.contentsRect().width()
                if page is None or width <= 0:
                    return
                height = (
                    page.heightForWidth(width)
                    if page.hasHeightForWidth()
                    else page.sizeHint().height()
                )
                height = max(height, page.minimumSizeHint().height())
                if self.height() != height:
                    self.setFixedHeight(height)

            def resizeEvent(self, event: Any) -> None:
                super().resizeEvent(event)
                from aqt.qt import QTimer

                QTimer.singleShot(0, self._fit_current_page)

            def sizeHint(self) -> Any:
                page = self.currentWidget()
                return page.sizeHint() if page is not None else super().sizeHint()

            def hasHeightForWidth(self) -> bool:
                page = self.currentWidget()
                return page.hasHeightForWidth() if page is not None else False

            def heightForWidth(self, width: int) -> int:
                page = self.currentWidget()
                if page is None:
                    return super().heightForWidth(width)
                if page.hasHeightForWidth():
                    return page.heightForWidth(width)
                return page.sizeHint().height()

            def minimumSizeHint(self) -> Any:
                page = self.currentWidget()
                return (
                    page.minimumSizeHint()
                    if page is not None
                    else super().minimumSizeHint()
                )

        self.dialog = QDialog(mw)
        self.dialog.setWindowTitle(tr("一阅（Oneki reviewer）", "Oneki reviewer"))
        self.dialog.setMinimumWidth(500)
        self.controller = controller
        config = controller.config()
        self._smart_mad_multiplier = float(
            config.get("smart_mad_multiplier", 2.0)
        )
        self._auto_easy_label_width = label_width(
            [
                tr("启用插件", "Enable add-on"),
                tr("自动变简单", "Automatic Easy"),
                tr("计时起点", "Timer starts"),
                tr("运行模式", "Mode"),
                tr("启用每日上限", "Enable daily limit"),
                tr("每天最多", "Maximum per day"),
            ]
        )
        self._auto_easy_value_label_width = label_width(
            [
                tr("答错屏蔽变简单", "Block Easy after incorrect answer"),
                tr("变简单阈值", "Time limit"),
                tr("最快的前", "Fastest"),
                tr("最低门槛按", "Minimum threshold"),
                tr("本轮至少需要", "Minimum for this review"),
                tr("计算方式", "Quota basis"),
            ]
        )
        self._auto_hard_label_width = label_width(
            [
                tr("自动再复习", "Automatic re-review"),
                tr("自动再复习阈值", "Automatic re-review threshold"),
                tr("自定义用时", "Custom time limit"),
            ]
        )

        self.enabled = ToggleSwitch(tr("启用插件", "Enable add-on"))
        self.enabled.setChecked(bool(config["enabled"]))
        self.auto_easy_enabled = ToggleSwitch(tr("自动变简单", "Automatically rate Good as Easy"))
        self.auto_easy_enabled.setChecked(
            bool(config.get("auto_easy_enabled", True))
        )
        self.auto_hard_enabled = ToggleSwitch(tr("自动再复习", "Automatic re-review"))
        self.auto_hard_enabled.setChecked(
            bool(config.get("auto_hard_enabled", False))
        )
        self.auto_hard_help = QLabel(
            tr(
                "牌组-选项-计时器，修改“回答时间记录上限（秒）”，即可自定义自动再复习的阈值\n建议搭配「计时器」功能使用",
                "Deck Options → Timer: change Maximum answer seconds to set the automatic re-review threshold.\nWe recommend using this with the Timer feature.",
            )
        )
        self.auto_hard_help.setWordWrap(True)
        self.instant_undo_shortcut = QKeySequenceEdit(
            QKeySequence(str(config.get("instant_undo_shortcut", "Z")))
        )
        self.instant_undo_shortcut.setMaximumSequenceLength(1)
        self.instant_undo_shortcut.setToolTip(
            tr(
                "默认 Z。恢复为你刚选的评分，保留这次答题和复习记录。"
                "遇到 Anki 或其他快捷键冲突时会提示且不会注册。清空可关闭快捷键。",
                "Default: Z. Restore the rating you selected while keeping this answer and review history. "
                "A shortcut that conflicts with Anki or another shortcut will be reported and not registered. Clear the field to disable it.",
            )
        )

        self.auto_easy_timer_start = QComboBox()
        self.auto_easy_timer_start.addItem(tr("显示答案时", "When the answer is shown"), "answer")
        self.auto_easy_timer_start.addItem(tr("显示题目时", "When the question is shown"), "question")
        self.auto_easy_timer_start.setCurrentIndex(
            max(
                0,
                self.auto_easy_timer_start.findData(
                    config["auto_easy_timer_start"]
                ),
            )
        )

        self.auto_hard_threshold_mode = QComboBox()
        self.auto_hard_threshold_mode.addItem(
            tr("回答时间记录上限（秒）", "Maximum answer seconds"), "anki_max_taken"
        )
        self.auto_hard_threshold_mode.addItem(tr("自定义秒数", "Custom time limit"), "custom")
        self.auto_hard_threshold_mode.setCurrentIndex(
            max(
                0,
                self.auto_hard_threshold_mode.findData(
                    config["auto_hard_threshold_mode"]
                ),
            )
        )
        self.auto_hard_threshold = QDoubleSpinBox()
        self.auto_hard_threshold.setRange(0.1, 3600)
        self.auto_hard_threshold.setDecimals(1)
        self.auto_hard_threshold.setSingleStep(0.5)
        self.auto_hard_threshold.setSuffix(tr(" 秒", " s"))
        self.auto_hard_threshold.setValue(
            float(config["auto_hard_threshold_ms"]) / 1000
        )
        self.auto_hard_threshold_label = _right_aligned_label(
            tr("自定义用时", "Custom time limit"),
            self._auto_hard_label_width,
        )
        self.auto_hard_threshold_mode.currentIndexChanged.connect(
            self._update_auto_hard_threshold_visibility
        )

        self.auto_grade_priority_box = QGroupBox(tr("优先级", "Priority"))
        priority_layout = QVBoxLayout(self.auto_grade_priority_box)
        self.auto_hard_priority = QRadioButton(tr("自动再复习优先", "Prioritize automatic re-review"))
        self.auto_easy_priority = QRadioButton(tr("自动变简单优先", "Prioritize Easy rating"))
        self.auto_grade_priority_buttons = QButtonGroup(
            self.auto_grade_priority_box
        )
        self.auto_grade_priority_buttons.addButton(self.auto_hard_priority)
        self.auto_grade_priority_buttons.addButton(self.auto_easy_priority)
        if config["auto_grade_priority"] == "easy":
            self.auto_easy_priority.setChecked(True)
        else:
            self.auto_hard_priority.setChecked(True)
        priority_layout.addWidget(self.auto_hard_priority)
        priority_layout.addWidget(self.auto_easy_priority)

        self.strategy = QComboBox()
        self.strategy.addItem(tr("固定模式", "Fixed mode"), "fixed")
        self.strategy.addItem(tr("比例模式", "Percentage mode"), "percent")
        self.strategy.addItem(tr("智能模式", "Smart mode"), "smart")

        self.mode_stack = CurrentPageStack()
        self._build_fixed_page(
            QDoubleSpinBox,
            QGridLayout,
            QWidget,
            config,
            self._auto_easy_value_label_width,
        )
        self._build_percent_page(
            QDoubleSpinBox,
            QGridLayout,
            QWidget,
            QLabel,
            config,
            self._auto_easy_value_label_width,
        )
        self._build_smart_page(
            QGridLayout,
            QLabel,
            QComboBox,
            QDoubleSpinBox,
            QSpinBox,
            QStackedWidget,
            QHBoxLayout,
            QWidget,
            config,
            self._auto_easy_value_label_width,
        )
        self.strategy.currentIndexChanged.connect(self._update_strategy_page)

        self.history = QSpinBox()
        self.history.setRange(1, 10)
        self.history.setValue(int(config["history_window"]))
        self.quota_enabled = ToggleSwitch(
            tr("启用每日上限", "Enable daily limit")
        )
        self.quota_enabled.setChecked(bool(config["quota_enabled"]))
        self.quota_details = QWidget()
        quota_details_layout = QGridLayout(self.quota_details)
        quota_details_layout.setContentsMargins(0, 0, 0, 0)
        quota_details_layout.setHorizontalSpacing(12)
        quota_details_layout.setVerticalSpacing(8)
        self.quota_percent = QDoubleSpinBox()
        self.quota_percent.setRange(0.1, 100.0)
        self.quota_percent.setSingleStep(1.0)
        self.quota_percent.setDecimals(1)
        self.quota_percent.setSuffix("%")
        self.quota_percent.setValue(float(config["quota_percent"]) * 100)
        quota_details_layout.addWidget(
            _right_aligned_label(
                tr("每天最多", "Maximum per day"),
                self._auto_easy_label_width,
            ),
            0,
            0,
        )
        quota_details_layout.addWidget(self.quota_percent, 0, 1)

        self.quota_scope = QComboBox()
        self.quota_scope.addItem(tr("只按可升级的卡计算", "Count only eligible cards"), "eligible_pool")
        self.quota_scope.addItem(tr("按当天全部复习卡计算", "Count all cards reviewed today"), "all_reviewed")
        self.quota_scope.setCurrentIndex(
            max(0, self.quota_scope.findData(config["quota_scope"]))
        )
        quota_details_layout.addWidget(
            _right_aligned_label(
                tr("计算方式", "Quota basis"),
                self._auto_easy_value_label_width,
            ),
            0,
            2,
        )
        quota_details_layout.addWidget(self.quota_scope, 0, 3)
        quota_details_layout.setColumnStretch(1, 1)
        quota_details_layout.setColumnStretch(3, 1)
        self.quota_enabled.toggled.connect(self._update_quota_visibility)

        self.auto_easy_group = QGroupBox(tr("自动变简单设置", "Automatic Easy rating settings"))
        auto_easy_container = QVBoxLayout(self.auto_easy_group)
        auto_easy_container.setSpacing(8)
        auto_easy_container.addWidget(
            switch_row(
                tr("自动变简单", "Automatic Easy"),
                self.auto_easy_enabled,
                self._auto_easy_label_width,
            )
        )
        self.auto_easy_options = QWidget()
        auto_easy_layout = QGridLayout(self.auto_easy_options)
        auto_easy_layout.setContentsMargins(0, 0, 0, 0)
        auto_easy_layout.setHorizontalSpacing(12)
        auto_easy_layout.setVerticalSpacing(8)
        auto_easy_layout.addWidget(
            _right_aligned_label(
                tr("计时起点", "Timer starts"), self._auto_easy_label_width
            ),
            0,
            0,
        )
        auto_easy_layout.addWidget(self.auto_easy_timer_start, 0, 1)
        self.history.setSuffix(tr(" 次", ""))
        auto_easy_layout.addWidget(
            _right_aligned_label(
                tr("答错屏蔽变简单", "Block Easy after incorrect answer"),
                self._auto_easy_value_label_width,
            ),
            0,
            2,
        )
        auto_easy_layout.addWidget(self.history, 0, 3)
        auto_easy_layout.addWidget(
            _right_aligned_label(
                tr("运行模式", "Mode"), self._auto_easy_label_width
            ),
            1,
            0,
        )
        auto_easy_layout.addWidget(self.strategy, 1, 1)
        auto_easy_layout.addWidget(self.mode_stack, 1, 2, 1, 2)
        auto_easy_layout.addWidget(
            _right_aligned_label(
                tr("启用每日上限", "Enable daily limit"),
                self._auto_easy_label_width,
            ),
            2,
            0,
        )
        auto_easy_layout.addWidget(self.quota_enabled, 2, 1)
        auto_easy_layout.addWidget(self.quota_details, 3, 0, 1, 4)
        auto_easy_layout.setColumnStretch(1, 1)
        auto_easy_layout.setColumnStretch(3, 1)
        auto_easy_container.addWidget(self.auto_easy_options)

        self.auto_hard_group = QGroupBox(tr("自动再复习设置", "Automatic re-review settings"))
        auto_hard_container = QVBoxLayout(self.auto_hard_group)
        auto_hard_container.setSpacing(8)
        auto_hard_container.addWidget(
            switch_row(
                tr("自动再复习", "Automatic re-review"),
                self.auto_hard_enabled,
                self._auto_hard_label_width,
            )
        )
        self.auto_hard_options = QWidget()
        auto_hard_layout = QGridLayout(self.auto_hard_options)
        auto_hard_layout.setContentsMargins(0, 0, 0, 0)
        auto_hard_layout.setHorizontalSpacing(12)
        auto_hard_layout.setVerticalSpacing(8)
        auto_hard_layout.addWidget(
            _right_aligned_label(
                tr("自动再复习阈值", "Automatic re-review threshold"),
                self._auto_hard_label_width,
            ),
            0,
            0,
        )
        auto_hard_layout.addWidget(self.auto_hard_threshold_mode, 0, 1)
        auto_hard_layout.addWidget(self.auto_hard_threshold_label, 1, 0)
        auto_hard_layout.addWidget(self.auto_hard_threshold, 1, 1)
        auto_hard_layout.addWidget(self.auto_hard_help, 2, 0, 1, 2)
        auto_hard_layout.setColumnStretch(1, 1)
        auto_hard_container.addWidget(self.auto_hard_options)

        shortcut_row = QWidget()
        shortcut_layout = QHBoxLayout(shortcut_row)
        shortcut_layout.setContentsMargins(0, 0, 0, 0)
        shortcut_layout.addWidget(self.instant_undo_shortcut)
        timing_button = QPushButton(tr("最近100张答题计时", "Last 100 Review Timings"))
        timing_button.clicked.connect(lambda: controller.show_timing_history(self.dialog))
        shortcut_layout.addWidget(timing_button)

        enabled_row = switch_row(
            tr("启用插件", "Enable add-on"),
            self.enabled,
            self._auto_easy_label_width,
        )

        shortcut_form = QFormLayout()
        _right_align_form(shortcut_form)
        shortcut_form.addRow(
            tr("撤销快捷键", "Undo shortcut"), shortcut_row
        )

        save = QPushButton(tr("保存", "Save"))
        save.clicked.connect(self._save)
        layout = QVBoxLayout()
        layout.addWidget(enabled_row)
        layout.addWidget(self.auto_easy_group)
        layout.addWidget(self.auto_hard_group)
        layout.addWidget(self.auto_grade_priority_box)
        layout.addLayout(shortcut_form)
        layout.addWidget(save)

        try:
            manifest = json.loads(
                Path(__file__).with_name("manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            version = str(manifest["human_version"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            version = tr("未知", "Unknown")
        footer = QLabel(
            trf(
                '版本 {version} · <a href="https://github.com/komakizhu/oneki-reviewer">GitHub 主页</a> · 开发者 komakizhu',
                'Version {version} · <a href="https://github.com/komakizhu/oneki-reviewer">GitHub</a> · Developer komakizhu',
                version=version,
            )
        )
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        footer.setOpenExternalLinks(True)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(footer)
        self.dialog.setLayout(layout)

        self.strategy.setCurrentIndex(
            max(0, self.strategy.findData(config["strategy"]))
        )
        self._update_strategy_page(self.strategy.currentIndex())
        self._update_quota_visibility(self.quota_enabled.isChecked())
        self._update_auto_hard_threshold_visibility()
        self.enabled.toggled.connect(self._update_feature_visibility)
        self.auto_easy_enabled.toggled.connect(self._update_feature_visibility)
        self.auto_hard_enabled.toggled.connect(self._update_feature_visibility)
        self._update_feature_visibility()

    def _build_fixed_page(
        self,
        double_spin_box: Any,
        grid_layout: Any,
        widget: Any,
        config: dict[str, Any],
        label_width: int,
    ) -> None:
        page = widget()
        form = grid_layout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.threshold = double_spin_box()
        self.threshold.setRange(0, 3600)
        self.threshold.setDecimals(3)
        self.threshold.setSingleStep(0.1)
        self.threshold.setSuffix(tr(" 秒", " s"))
        self.threshold.setValue(float(config["threshold_ms"]) / 1000)
        self.threshold.setToolTip(
            tr(
                "符合条件时，评分在本次答题时立即生效；复习结束后仅汇总已完成的自动评分。",
                "When the criteria are met, the rating changes as you answer. At the end of review, only completed automatic ratings are summarized.",
            )
        )
        form.addWidget(
            _right_aligned_label(tr("变简单阈值", "Time limit"), label_width),
            0,
            0,
        )
        form.addWidget(self.threshold, 0, 1)
        form.setColumnStretch(1, 1)

        self.mode_stack.addWidget(page)

    def _build_percent_page(
        self,
        double_spin_box: Any,
        grid_layout: Any,
        widget: Any,
        label: Any,
        config: dict[str, Any],
        label_width: int,
    ) -> None:
        page = widget()
        form = grid_layout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.distribution = double_spin_box()
        self.distribution.setRange(0.1, 100.0)
        self.distribution.setSingleStep(1.0)
        self.distribution.setDecimals(1)
        self.distribution.setSuffix("%")
        self.distribution.setValue(float(config["distribution_percent"]) * 100)
        self.distribution.valueChanged.connect(self._update_percent_help)
        form.addWidget(
            _right_aligned_label(tr("最快的前", "Fastest"), label_width),
            0,
            0,
        )
        form.addWidget(self.distribution, 0, 1)
        self.percent_help = label()
        self.percent_help.setWordWrap(True)
        form.addWidget(self.percent_help, 1, 0, 1, 2)
        form.setColumnStretch(1, 1)
        self.mode_stack.addWidget(page)

    def _build_smart_page(
        self,
        grid_layout: Any,
        label: Any,
        combo_box: Any,
        double_spin_box: Any,
        spin_box: Any,
        stacked_widget: Any,
        value_layout: Any,
        widget: Any,
        config: dict[str, Any],
        label_width: int,
    ) -> None:
        page = widget()
        form = grid_layout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.smart_minimum_mode = combo_box()
        self.smart_minimum_mode.addItem(tr("按百分比", "By percentage"), "percent")
        self.smart_minimum_mode.addItem(tr("按固定张数", "By fixed card count"), "count")
        self.smart_minimum_mode.setCurrentIndex(
            max(
                0,
                self.smart_minimum_mode.findData(
                    config.get("smart_minimum_mode", "percent")
                ),
            )
        )
        form.addWidget(
            _right_aligned_label(
                tr("最低门槛按", "Minimum threshold"), label_width
            ),
            0,
            0,
        )
        form.addWidget(self.smart_minimum_mode, 0, 1)

        self.smart_minimum_percent = double_spin_box()
        self.smart_minimum_percent.setRange(0.1, 100.0)
        self.smart_minimum_percent.setSingleStep(1.0)
        self.smart_minimum_percent.setDecimals(1)
        self.smart_minimum_percent.setSuffix("%")
        self.smart_minimum_percent.setValue(
            float(config.get("smart_minimum_percent", 0.20)) * 100
        )

        self.smart_minimum_count = spin_box()
        self.smart_minimum_count.setRange(1, 10000)
        self.smart_minimum_count.setValue(
            int(config.get("minimum_batch_size", 5))
        )

        self.smart_value_stack = stacked_widget()
        percent_page = widget()
        percent_form = value_layout(percent_page)
        percent_form.setContentsMargins(0, 0, 0, 0)
        percent_form.addWidget(self.smart_minimum_percent)
        percent_form.addStretch(1)
        count_page = widget()
        count_form = value_layout(count_page)
        count_form.setContentsMargins(0, 0, 0, 0)
        count_form.addWidget(self.smart_minimum_count)
        count_form.addStretch(1)
        self.smart_value_stack.addWidget(percent_page)
        self.smart_value_stack.addWidget(count_page)
        form.addWidget(
            _right_aligned_label(
                tr("本轮至少需要", "Minimum for this review"), label_width
            ),
            1,
            0,
        )
        form.addWidget(self.smart_value_stack, 1, 1)

        self.smart_help = label()
        self.smart_help.setWordWrap(True)
        form.addWidget(self.smart_help, 2, 0, 1, 2)
        form.setColumnStretch(1, 1)
        self.smart_minimum_mode.currentIndexChanged.connect(
            self._update_smart_minimum_page
        )
        self.smart_minimum_percent.valueChanged.connect(
            self._update_smart_minimum_help
        )
        self.smart_minimum_count.valueChanged.connect(
            self._update_smart_minimum_help
        )
        self._update_smart_minimum_page(self.smart_minimum_mode.currentIndex())
        self.mode_stack.addWidget(page)

    def _update_strategy_page(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)
        self.mode_stack._fit_current_page()
        self.mode_stack.updateGeometry()
        from aqt.qt import QTimer

        QTimer.singleShot(0, self.mode_stack._fit_current_page)
        if self.strategy.currentData() == "percent":
            self._update_percent_help(self.distribution.value())

    def _update_percent_help(self, value: float) -> None:
        import math

        ratio = float(value) / 100
        minimum = max(1, math.ceil(1 / ratio))
        self.percent_help.setText(
            trf(
                "这轮复习至少需要 {minimum} 张卡；少于这个数量时不处理，也不会改用其他模式。",
                "This review needs at least {minimum} {card_word}. Below this count, no cards are processed and no other mode is used.",
                minimum=minimum,
                card_word="card" if minimum == 1 else "cards",
            )
        )

    def _update_smart_minimum_page(self, index: int) -> None:
        self.smart_value_stack.setCurrentIndex(index)
        self._update_smart_minimum_help()

    def _update_smart_minimum_help(self, *_args: Any) -> None:
        if self.smart_minimum_mode.currentData() == "percent":
            percent = self.smart_minimum_percent.value() / 100
            minimum = minimum_batch_size(percent)
            self.smart_help.setText(
                trf(
                    "智能模式会自动找出这轮复习里明显答得快的卡；按 {percent:g}% 计算，至少需要 {minimum} 张卡。",
                    "Smart mode finds cards answered noticeably faster than the rest. At {percent:g}%, this review needs at least {minimum} {card_word}.",
                    percent=self.smart_minimum_percent.value(),
                    minimum=minimum,
                    card_word="card" if minimum == 1 else "cards",
                )
            )
        else:
            self.smart_help.setText(
                trf(
                    "智能模式会自动找出这轮复习里明显答得快的卡；至少需要 {minimum} 张卡。",
                    "Smart mode finds cards answered noticeably faster than the rest. This review needs at least {minimum} {card_word}.",
                    minimum=self.smart_minimum_count.value(),
                    card_word="card" if self.smart_minimum_count.value() == 1 else "cards",
                )
            )

    def _update_quota_visibility(self, enabled: bool) -> None:
        self.quota_details.setVisible(bool(enabled))

    def _update_feature_visibility(self, *_args: Any) -> None:
        plugin_enabled = self.enabled.isChecked()
        auto_easy_enabled = self.auto_easy_enabled.isChecked()
        auto_hard_enabled = self.auto_hard_enabled.isChecked()
        self.auto_easy_group.setEnabled(plugin_enabled)
        self.auto_hard_group.setEnabled(plugin_enabled)
        self.auto_easy_options.setEnabled(plugin_enabled and auto_easy_enabled)
        self.auto_hard_options.setEnabled(plugin_enabled and auto_hard_enabled)
        self.auto_grade_priority_box.setEnabled(
            plugin_enabled and auto_easy_enabled and auto_hard_enabled
        )

    def _update_auto_hard_threshold_visibility(self, *_args: Any) -> None:
        visible = self.auto_hard_threshold_mode.currentData() == "custom"
        self.auto_hard_threshold_label.setVisible(visible)
        self.auto_hard_threshold.setVisible(visible)

    def _save(self) -> None:
        from aqt.qt import QKeySequence

        config = {
            "enabled": self.enabled.isChecked(),
            "auto_easy_enabled": self.auto_easy_enabled.isChecked(),
            "auto_hard_enabled": self.auto_hard_enabled.isChecked(),
            "auto_easy_timer_start": self.auto_easy_timer_start.currentData(),
            "auto_hard_threshold_mode": self.auto_hard_threshold_mode.currentData(),
            "auto_hard_threshold_ms": int(
                round(self.auto_hard_threshold.value() * 1000)
            ),
            "auto_grade_priority": (
                "hard" if self.auto_hard_priority.isChecked() else "easy"
            ),
            "instant_undo_shortcut": self.instant_undo_shortcut.keySequence().toString(
                QKeySequence.SequenceFormat.PortableText
            ),
            "strategy": str(self.strategy.currentData()),
            "threshold_ms": int(round(self.threshold.value() * 1000)),
            "distribution_percent": self.distribution.value() / 100,
            "minimum_batch_size": self.smart_minimum_count.value(),
            "smart_minimum_mode": str(self.smart_minimum_mode.currentData()),
            "smart_minimum_percent": self.smart_minimum_percent.value() / 100,
            "history_window": self.history.value(),
            "smart_mad_multiplier": self._smart_mad_multiplier,
            "quota_enabled": self.quota_enabled.isChecked(),
            "quota_percent": self.quota_percent.value() / 100,
            "quota_scope": self.quota_scope.currentData(),
        }
        self.controller.mw.addonManager.writeConfig(PACKAGE_NAME, config)
        if getattr(self.controller.mw, "state", None) == "review":
            reviewer = self.controller.mw.reviewer
            self.controller.mw.clearStateShortcuts()
            self.controller.mw.setStateShortcuts(reviewer._shortcutKeys())
            script = _review_shortcut_script(
                config["instant_undo_shortcut"],
                enabled=not self.controller.shortcut_conflict,
            )
            reviewer.web.eval(script)
        self.dialog.accept()

    def show(self) -> None:
        self.dialog.exec()


def initialize() -> None:
    import aqt
    from aqt import gui_hooks, mw
    from aqt.qt import QAction

    if getattr(mw, "_anki_batch_auto_easy_initialized", False):
        return

    controller = BatchController(mw)
    mw._anki_batch_auto_easy_controller = controller
    mw._anki_batch_auto_easy_initialized = True

    def open_settings() -> None:
        SettingsDialog(mw, controller).show()

    settings_action = QAction(tr("一阅（Oneki reviewer）", "Oneki reviewer"), mw)
    settings_action.triggered.connect(open_settings)
    mw.form.menuTools.addAction(settings_action)

    gui_hooks.reviewer_did_show_question.append(controller.did_show_question)
    gui_hooks.reviewer_did_show_answer.append(controller.did_show_answer)
    gui_hooks.reviewer_will_answer_card.append(controller.will_answer)
    gui_hooks.reviewer_did_answer_card.append(controller.did_answer)
    gui_hooks.reviewer_will_end.append(controller.will_end)
    gui_hooks.state_shortcuts_will_change.append(
        controller.reviewer_shortcuts_will_change
    )
    gui_hooks.state_did_undo.append(controller.native_undo_occurred)
    gui_hooks.operation_did_execute.append(controller.operation_did_execute)
    gui_hooks.profile_will_close.append(controller.profile_will_close)
    gui_hooks.profile_did_open.append(controller.profile_did_open)

    def add_regrade_shortcut(web_content: Any, context: Any) -> None:
        from aqt.reviewer import Reviewer

        if not isinstance(context, Reviewer):
            return
        shortcut = str(controller.config().get("instant_undo_shortcut", "Z")).strip()
        web_content.head += (
            f"<script>{_review_shortcut_script(shortcut, enabled=not controller.shortcut_conflict)}</script>"
        )

    gui_hooks.webview_will_set_content.append(add_regrade_shortcut)
    gui_hooks.webview_did_receive_js_message.append(controller.handle_regrade_shortcut)

    version = getattr(aqt, "appVersion", "")
    if not version.startswith("26.9"):
        print(f"{PACKAGE_NAME}: target Anki version is 26.9.x, found {version}")

