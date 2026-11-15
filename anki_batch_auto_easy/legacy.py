"""Compatibility path for Anki's classic reviewer and scheduler APIs.

This module intentionally avoids post-Python-3.6 syntax and APIs so it can be
imported by Anki 2.1.0. Older Anki releases use a different scheduler and hook
system; the legacy path keeps the immediate, fixed-threshold rules working and
does not import the modern integration module.
"""

import json
import logging
import os
import tempfile
import time

from .i18n import tr


PACKAGE_NAME = __package__.split(".")[0]
LOGGER = logging.getLogger("addon." + PACKAGE_NAME)
CARD_COLUMNS = (
    "id", "nid", "did", "ord", "mod", "usn", "type", "queue", "due",
    "ivl", "factor", "reps", "lapses", "left", "odue", "odid", "flags",
    "data",
)
REVLOG_COLUMNS = (
    "id", "cid", "usn", "ease", "ivl", "lastIvl", "factor", "time", "type",
)
DEFAULT_CONFIG = {
    "enabled": True,
    "auto_easy_enabled": True,
    "auto_hard_enabled": True,
    "auto_easy_timer_start": "answer",
    "auto_hard_threshold_mode": "anki_max_taken",
    "auto_hard_threshold_ms": 60000,
    "auto_grade_priority": "hard",
    "threshold_ms": 1000,
    "history_window": 2,
}


def _log(message, exc=None):
    if exc is None:
        LOGGER.warning("%s", message)
    else:
        LOGGER.warning("%s: %s", message, exc, exc_info=True)


def _state_path(mw):
    try:
        folder = mw.pm.profileFolder()
    except Exception:
        folder = None
    if folder:
        return os.path.join(folder, "anki_batch_auto_easy_state.json")
    return os.path.join(os.path.dirname(__file__), "batch_state.json")


class LegacyController(object):
    """Small runtime bridge for the classic Anki reviewer.

    Old Anki has no CardAnswer or V3 scheduling states. Its reviewer passes an
    integer ease directly to Scheduler.answerCard(), so adjustments are made
    before that call and the original ease remains available for safe undo.
    """

    def __init__(self, mw):
        self.mw = mw
        self.question_started = {}
        self.answer_started = {}
        self.active_timer = {}
        self.space_answer = False
        self.undo_notice = None
        self.undo_button = None
        self.undo_shortcut = None
        self.state_path = _state_path(mw)
        self.state = self._load_state()

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                value.setdefault("timing_history", [])
                value.setdefault("instant_undo", None)
                return value
        except (IOError, ValueError, OSError):
            pass
        return {"version": 1, "timing_history": [], "instant_undo": None}

    def profile_did_open(self, *_args):
        self.state_path = _state_path(self.mw)
        self.state = self._load_state()
        self.question_started.clear()
        self.answer_started.clear()
        self.active_timer.clear()
        self.space_answer = False

    def _save_state(self):
        folder = os.path.dirname(self.state_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        fd, temporary = tempfile.mkstemp(
            prefix=".oneki-legacy-", suffix=".tmp", dir=folder or None
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def config(self):
        try:
            value = self.mw.addonManager.getConfig(PACKAGE_NAME) or {}
            if not isinstance(value, dict):
                raise ValueError("add-on configuration must be an object")
            config = dict(DEFAULT_CONFIG)
            config.update(value)
            return config
        except Exception as exc:
            _log("Could not read legacy settings; automatic rating is disabled", exc)
            config = dict(DEFAULT_CONFIG)
            config["enabled"] = False
            return config

    def did_show_question(self, *args):
        reviewer = getattr(self.mw, "reviewer", None)
        card = args[0] if args else getattr(reviewer, "card", None)
        if card is None:
            return
        card_id = int(card.id)
        now = time.monotonic()
        self.question_started[card_id] = now
        self.answer_started.pop(card_id, None)
        if self.config().get("auto_easy_timer_start") == "question":
            self.active_timer[card_id] = now

    def did_show_answer(self, *args):
        reviewer = getattr(self.mw, "reviewer", None)
        card = args[0] if args else getattr(reviewer, "card", None)
        if card is None:
            return
        card_id = int(card.id)
        now = time.monotonic()
        self.answer_started[card_id] = now
        if self.config().get("auto_easy_timer_start") != "question":
            self.active_timer[card_id] = now

    def on_enter_key(self, old, reviewer, *args, **kwargs):
        was_answer = getattr(reviewer, "state", None) == "answer"
        previous = self.space_answer
        self.space_answer = was_answer
        try:
            return old(reviewer, *args, **kwargs)
        finally:
            self.space_answer = previous

    def on_space_answer_selected(self, old, reviewer, *args, **kwargs):
        previous = self.space_answer
        self.space_answer = True
        try:
            return old(reviewer, *args, **kwargs)
        finally:
            self.space_answer = previous

    def answer_card(self, old, reviewer, ease):
        card = getattr(reviewer, "card", None)
        if card is None:
            return old(reviewer, ease)
        original_ease = int(ease)
        original_card = self._read_row("cards", CARD_COLUMNS, int(card.id))
        timing = self._timing_values(card)
        effective_ease = self._effective_ease(card, original_ease, timing)
        result = old(reviewer, effective_ease)
        if effective_ease != original_ease:
            try:
                self._record_change(
                    card, original_ease, effective_ease, timing, original_card
                )
                if self.state.get("instant_undo"):
                    self._show_undo_notice(original_ease, effective_ease)
            except Exception as exc:
                _log("Could not save legacy rating-change history", exc)
        else:
            self._record_timing(card, original_ease, effective_ease, timing)
        return result

    def _effective_ease(self, card, ease, timing):
        config = self.config()
        if (
            not self.space_answer
            or ease != 3
            or not bool(config.get("enabled", True))
        ):
            return ease
        card_id = int(card.id)
        elapsed_ms = timing["elapsed_ms"]
        easy = (
            bool(config.get("auto_easy_enabled", True))
            and str(config.get("strategy", "fixed")) == "fixed"
            and not bool(config.get("quota_enabled", False))
            and elapsed_ms is not None
        )
        easy = easy and elapsed_ms <= max(0, int(config.get("threshold_ms", 1000)))
        if easy:
            easy = self._recent_passes_allow(
                card_id, int(config.get("history_window", 2)), card
            )

        max_taken_ms = self._max_time_taken(card, config)
        question_start = timing["question_start"]
        answer_start = timing["answer_start"]
        review_elapsed_ms = (
            max(0, int((answer_start - question_start) * 1000))
            if question_start is not None and answer_start is not None
            else None
        )
        hard = (
            bool(config.get("auto_hard_enabled", True))
            and max_taken_ms > 0
            and review_elapsed_ms is not None
            and review_elapsed_ms >= max_taken_ms
        )
        if hard and easy:
            return 1 if config.get("auto_grade_priority") == "hard" else 4
        if hard:
            # The V2 scheduler exposes no next-state calculation API. Again is
            # the only stable choice that guarantees a same-day re-review.
            return 1
        if easy:
            return 4
        return ease

    def _recent_passes_allow(self, card_id, history_window, card=None):
        if history_window < 1:
            return False
        try:
            rows = self.mw.col.db.all(
                "select ease from revlog where cid=? order by id", card_id
            )
        except Exception as exc:
            _log("Could not read recent review history; keeping the selected rating", exc)
            return False
        recent = rows[-history_window:]
        if any(int(row[0]) in (1, 2) for row in recent):
            return False
        if card is not None and int(getattr(card, "type", -1)) in (0, 1):
            if int(getattr(card, "lapses", 0)) == 0:
                return sum(int(row[0]) == 3 for row in rows) < 2
        return True

    def _max_time_taken(self, card, config):
        if config.get("auto_hard_threshold_mode") == "custom":
            return max(0, int(config.get("auto_hard_threshold_ms", 60000)))
        try:
            return max(0, int(card.timeLimit()))
        except Exception:
            return 0

    def _read_row(self, table, columns, row_id):
        row = self.mw.col.db.first(
            "select %s from %s where id=?" % (", ".join(columns), table),
            row_id,
        )
        return dict(zip(columns, row)) if row else None

    def _record_change(
        self, card, original_ease, effective_ease, timing, original_card
    ):
        if original_card is None:
            raise RuntimeError("Anki did not provide the pre-answer card state")
        card_id = int(card.id)
        post_card = self._read_row("cards", CARD_COLUMNS, card_id)
        post_revlog = self.mw.col.db.first(
            "select %s from revlog where cid=? order by id desc limit 1"
            % ", ".join(REVLOG_COLUMNS),
            card_id,
        )
        if post_card is None or post_revlog is None:
            raise RuntimeError("Anki did not save the review record")
        entry = {
            "card_id": card_id,
            "original_ease": int(original_ease),
            "effective_ease": int(effective_ease),
            "original_card": original_card,
            "post_card": post_card,
            "post_revlog": dict(zip(REVLOG_COLUMNS, post_revlog)),
            "created_at": int(time.time() * 1000),
        }
        self.state["instant_undo"] = entry
        self._record_timing(card, original_ease, effective_ease, timing)
        self._save_state()

    def _timing_values(self, card):
        card_id = int(card.id)
        started = self.active_timer.pop(card_id, None)
        question_start = self.question_started.pop(card_id, None)
        answer_start = self.answer_started.pop(card_id, None)
        return {
            "elapsed_ms": (
            max(0, int((time.monotonic() - started) * 1000))
            if started is not None
            else None
            ),
            "question_start": question_start,
            "answer_start": answer_start,
        }

    def _record_timing(self, card, original_ease, effective_ease, timing):
        self._record_timing_values(
            int(card.id), int(card.did), original_ease, effective_ease,
            timing["elapsed_ms"], timing["question_start"], timing["answer_start"],
        )

    def _record_timing_values(
        self, card_id, deck_id, original_ease, effective_ease, elapsed_ms,
        question_start, answer_start,
    ):
        history = self.state.setdefault("timing_history", [])
        history.append({
            "answered_at_ms": int(time.time() * 1000),
            "card_id": card_id,
            "deck_id": deck_id,
            "user_ease": int(original_ease),
            "effective_ease": int(effective_ease),
            "elapsed_ms": elapsed_ms,
            "question_to_answer_ms": (
                max(0, int((answer_start - question_start) * 1000))
                if question_start is not None and answer_start is not None
                else None
            ),
        })
        del history[:-100]
        try:
            self._save_state()
        except Exception as exc:
            _log("Could not save review timing history", exc)

    def _show_undo_notice(self, original_ease, effective_ease):
        try:
            from aqt.qt import QFrame, QHBoxLayout, QLabel, QPushButton, Qt, QTimer

            if self.undo_notice is not None:
                self.undo_notice.close()
                self.undo_notice.deleteLater()
            notice = QFrame(
                self.mw,
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus,
            )
            layout = QHBoxLayout(notice)
            layout.addWidget(QLabel(
                tr("自动调整评分", "Rating adjusted automatically"), notice
            ))
            button = QPushButton(tr("撤销改分", "Undo rating change"), notice)
            button.clicked.connect(self.undo_rating_change)
            layout.addWidget(button)
            notice.adjustSize()
            point = self.mw.mapToGlobal(self.mw.rect().bottomRight())
            notice.move(point.x() - notice.width() - 24,
                        point.y() - notice.height() - 64)
            notice.show()
            self.undo_notice = notice
            self.undo_button = button
            QTimer.singleShot(7000, notice.close)
        except Exception as exc:
            _log("Could not show the legacy undo button", exc)

    def undo_rating_change(self):
        entry = self.state.get("instant_undo")
        if not entry:
            return
        col = self.mw.col
        try:
            status_reader = getattr(col, "undo_status", None)
            if callable(status_reader):
                undo_label = str(getattr(status_reader(), "undo", ""))
                if undo_label not in ("Review", "Answer Card", "复习"):
                    raise RuntimeError(
                        "Anki's latest undo entry is no longer a review"
                    )
            else:
                undo_entry = getattr(col, "_undo", None)
                if not undo_entry or int(undo_entry[0]) != 1:
                    raise RuntimeError(
                        "Anki's latest undo entry is no longer a review"
                    )
            card_id = int(entry["card_id"])
            current_card = self._read_row("cards", CARD_COLUMNS, card_id)
            latest = self.mw.col.db.first(
                "select %s from revlog where cid=? order by id desc limit 1"
                % ", ".join(REVLOG_COLUMNS),
                card_id,
            )
            expected = entry["post_revlog"]
            if (
                current_card != entry["post_card"]
                or latest is None
                or dict(zip(REVLOG_COLUMNS, latest)) != expected
            ):
                raise RuntimeError("The card or review record changed after rating")

            legacy_undo = getattr(col, "undo_legacy", None)
            if callable(legacy_undo):
                if legacy_undo() is None:
                    raise RuntimeError("Anki's legacy review undo entry is empty")
            else:
                col.undo()
            card_getter = getattr(col, "get_card", None) or getattr(
                col, "getCard", None
            )
            card = card_getter(card_id) if card_getter else None
            if card is None:
                raise RuntimeError("Anki could not restore the reviewed card")
            restored_card = self._read_row("cards", CARD_COLUMNS, card_id)
            if restored_card != entry.get("original_card"):
                raise RuntimeError("Anki restored an unexpected card state")
            timer_starter = getattr(card, "start_timer", None) or getattr(
                card, "startTimer", None
            )
            if callable(timer_starter):
                timer_starter()
            col.sched.answerCard(card, int(entry["original_ease"]))
            new_id = self.mw.col.db.scalar(
                "select id from revlog where cid=? order by id desc limit 1", card_id
            )
            self.mw.col.db.execute(
                "update revlog set id=?, time=? where id=?",
                int(expected["id"]), int(expected["time"]), int(new_id),
            )
            self.state["instant_undo"] = None
            self._save_state()
            if self.undo_notice is not None:
                self.undo_notice.close()
                self.undo_notice = None
                self.undo_button = None
        except Exception as exc:
            _log("Could not safely restore the original rating", exc)

    def _show_message(self, text):
        try:
            from aqt.qt import QMessageBox

            QMessageBox.information(self.mw, tr("一阅", "Oneki reviewer"), text)
        except Exception as exc:
            _log("Could not show a legacy status message", exc)

    def show_timing_history(self):
        try:
            from aqt.qt import QDialog, QPlainTextEdit, QPushButton, QVBoxLayout

            dialog = QDialog(self.mw)
            dialog.setWindowTitle(tr("最近100张答题计时", "Last 100 review timings"))
            layout = QVBoxLayout(dialog)
            history = self.state.get("timing_history", [])
            details = QPlainTextEdit(dialog)
            details.setReadOnly(True)
            lines = [
                tr("时间　卡片　原评分　最终评分　用时", "Time   Card   Selected   Final   Seconds")
            ]
            for sample in reversed(history):
                elapsed = sample.get("elapsed_ms")
                seconds = "—" if elapsed is None else "%.2f" % (float(elapsed) / 1000)
                lines.append(
                    "%s   %s   %s   %s   %s" % (
                        time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(int(sample.get("answered_at_ms", 0)) / 1000),
                        ),
                        sample.get("card_id", ""),
                        self._grade_name(sample.get("user_ease")),
                        self._grade_name(sample.get("effective_ease")),
                        seconds,
                    )
                )
            details.setPlainText("\n".join(lines))
            layout.addWidget(details)
            button = QPushButton(tr("关闭", "Close"), dialog)
            button.clicked.connect(dialog.accept)
            layout.addWidget(button)
            dialog.exec_()
        except Exception as exc:
            _log("Could not open legacy timing history", exc)

    def _grade_name(self, value):
        return {
            1: tr("重来", "Again"),
            2: tr("困难", "Hard"),
            3: tr("良好", "Good"),
            4: tr("简单", "Easy"),
        }.get(int(value or 0), tr("未知", "Unknown"))

    def install_undo_shortcut(self):
        try:
            from aqt.qt import QKeySequence, QShortcut

            if self.undo_shortcut is not None:
                self.undo_shortcut.setEnabled(False)
                self.undo_shortcut.deleteLater()
                self.undo_shortcut = None
            value = str(self.config().get("instant_undo_shortcut", "Z")).strip()
            if not value:
                return
            parent = getattr(self.mw, "reviewer", None) or self.mw
            self.undo_shortcut = QShortcut(QKeySequence(value), parent)
            self.undo_shortcut.activated.connect(self.undo_rating_change)
        except Exception as exc:
            _log("Could not install the legacy undo shortcut", exc)


def _settings_dialog(mw, controller):
    from aqt.qt import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
        QKeySequence, QKeySequenceEdit, QLabel, QPushButton, QVBoxLayout,
    )

    config = controller.config()
    dialog = QDialog(mw)
    dialog.setWindowTitle(tr("一阅（Oneki reviewer）", "Oneki reviewer"))
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    enabled = QCheckBox(tr("启用插件", "Enable add-on"), dialog)
    enabled.setChecked(bool(config.get("enabled", True)))
    auto_easy = QCheckBox(tr("自动变简单", "Automatically rate Good as Easy"), dialog)
    auto_easy.setChecked(bool(config.get("auto_easy_enabled", True)))
    auto_hard = QCheckBox(tr("自动再复习", "Automatic re-review"), dialog)
    auto_hard.setChecked(bool(config.get("auto_hard_enabled", True)))
    form.addRow(enabled)
    form.addRow(auto_easy)
    form.addRow(auto_hard)

    threshold = QDoubleSpinBox(dialog)
    threshold.setRange(0.0, 3600.0)
    threshold.setDecimals(2)
    threshold.setSuffix(" " + tr("秒", "seconds"))
    threshold.setValue(float(config.get("threshold_ms", 1000)) / 1000)
    form.addRow(tr("变简单阈值", "Easy threshold"), threshold)

    timer_mode = QComboBox(dialog)
    timer_mode.addItem(tr("牌组计时器", "Deck timer"), "anki_max_taken")
    timer_mode.addItem(tr("自定义", "Custom"), "custom")
    mode = str(config.get("auto_hard_threshold_mode", "anki_max_taken"))
    timer_mode.setCurrentIndex(max(0, timer_mode.findData(mode)))
    form.addRow(tr("自动再复习阈值", "Automatic re-review threshold"), timer_mode)

    hard_threshold = QDoubleSpinBox(dialog)
    hard_threshold.setRange(0.1, 3600.0)
    hard_threshold.setDecimals(2)
    hard_threshold.setSuffix(" " + tr("秒", "seconds"))
    hard_threshold.setValue(
        float(config.get("auto_hard_threshold_ms", 60000)) / 1000
    )
    form.addRow(tr("自定义阈值", "Custom threshold"), hard_threshold)

    timer_start = QComboBox(dialog)
    timer_start.addItem(tr("显示答案时", "When answer appears"), "answer")
    timer_start.addItem(tr("显示题目时", "When question appears"), "question")
    start = str(config.get("auto_easy_timer_start", "answer"))
    timer_start.setCurrentIndex(max(0, timer_start.findData(start)))
    form.addRow(tr("计时起点", "Timer starts"), timer_start)

    priority = QComboBox(dialog)
    priority.addItem(tr("自动再复习优先", "Re-review first"), "hard")
    priority.addItem(tr("自动变简单优先", "Easy first"), "easy")
    priority_value = str(config.get("auto_grade_priority", "hard"))
    priority.setCurrentIndex(max(0, priority.findData(priority_value)))
    form.addRow(tr("优先级", "Priority"), priority)
    undo_shortcut = QKeySequenceEdit(
        QKeySequence(str(config.get("instant_undo_shortcut", "Z"))), dialog
    )
    form.addRow(tr("撤销快捷键", "Undo shortcut"), undo_shortcut)
    layout.addLayout(form)

    limitation = QLabel(
        tr(
            "此 Anki 版本使用旧排程器：固定计时规则可用；比例、智能批次、每日上限和批量改分不可用。",
            "This Anki version uses the classic scheduler. Fixed timing rules are available; percentage, smart selection, daily limits, and batch regrading are not.",
        ),
        dialog,
    )
    limitation.setWordWrap(True)
    layout.addWidget(limitation)
    history_button = QPushButton(
        tr("最近100张答题计时", "Last 100 review timings"), dialog
    )
    history_button.clicked.connect(controller.show_timing_history)
    layout.addWidget(history_button)

    save = QPushButton(tr("保存", "Save"), dialog)
    layout.addWidget(save)

    def save_settings():
        value = dict(config)
        value.update({
            "enabled": enabled.isChecked(),
            "auto_easy_enabled": auto_easy.isChecked(),
            "auto_hard_enabled": auto_hard.isChecked(),
            "threshold_ms": int(round(threshold.value() * 1000)),
            "auto_hard_threshold_mode": str(timer_mode.currentData()),
            "auto_hard_threshold_ms": int(round(hard_threshold.value() * 1000)),
            "auto_easy_timer_start": str(timer_start.currentData()),
            "auto_grade_priority": str(priority.currentData()),
            "instant_undo_shortcut": undo_shortcut.keySequence().toString(),
        })
        try:
            mw.addonManager.writeConfig(PACKAGE_NAME, value)
        except Exception as exc:
            _log("Could not save legacy settings", exc)
            controller._show_message(
                tr("设置保存失败，请查看 Anki 后台日志。", "Could not save settings. Check Anki's debug log.")
            )
            return
        controller.install_undo_shortcut()
        dialog.accept()

    save.clicked.connect(save_settings)
    execute = getattr(dialog, "exec_", None) or dialog.exec
    execute()


def initialize():
    import aqt
    from anki.hooks import addHook, wrap
    from aqt import mw
    from aqt.qt import QAction
    from aqt.reviewer import Reviewer

    if getattr(mw, "_anki_batch_auto_easy_initialized", False):
        return
    controller = LegacyController(mw)
    mw._anki_batch_auto_easy_controller = controller
    mw._anki_batch_auto_easy_initialized = True

    action = QAction(tr("一阅（Oneki reviewer）", "Oneki reviewer"), mw)
    action.triggered.connect(lambda: _settings_dialog(mw, controller))
    mw.form.menuTools.addAction(action)

    addHook("showQuestion", controller.did_show_question)
    addHook("showAnswer", controller.did_show_answer)
    controller.install_undo_shortcut()
    try:
        hooks = aqt.gui_hooks
        hooks.reviewer_did_show_question.append(controller.did_show_question)
        hooks.reviewer_did_show_answer.append(controller.did_show_answer)
        hooks.profile_did_open.append(controller.profile_did_open)
    except Exception:
        try:
            addHook("profileLoaded", controller.profile_did_open)
        except Exception:
            pass
    if not getattr(Reviewer, "_oneki_legacy_wrapped", False):
        Reviewer._answerCard = wrap(
            Reviewer._answerCard, controller.answer_card, pos="around"
        )
        Reviewer.onEnterKey = wrap(
            Reviewer.onEnterKey, controller.on_enter_key, pos="around"
        )
        if hasattr(Reviewer, "_onAnswerButton"):
            Reviewer._onAnswerButton = wrap(
                Reviewer._onAnswerButton,
                controller.on_space_answer_selected,
                pos="around",
            )
        Reviewer._oneki_legacy_wrapped = True

    _log(
        "Classic Anki compatibility path active (%s); advanced batch modes are unavailable"
        % getattr(aqt, "appVersion", "unknown")
    )
