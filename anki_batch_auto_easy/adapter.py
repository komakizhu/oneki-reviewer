from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from .i18n import tr


class AdapterError(RuntimeError):
    pass


class AdapterRecoveryError(AdapterError):
    pass


@dataclass
class ReplacementRecord:
    attempt_id: str
    card_id: int
    original_revlog_id: int
    original_card: dict[str, Any]
    original_revlog: dict[str, Any]
    post_card: dict[str, Any]
    easy_answer_b64: str
    original_answer_b64: str
    original_ease: int
    reviewer_undo_step: int | None

    @property
    def easy_answer(self) -> bytes:
        return base64.b64decode(self.easy_answer_b64.encode("ascii"))


@dataclass
class ReplacementResult:
    replaced_attempt_ids: list[str]
    undo_steps: dict[str, int]
    skipped_reason: str | None = None
    failure: str | None = None


CARD_COLUMNS = (
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
)
REVLOG_COLUMNS = (
    "id",
    "cid",
    "usn",
    "ease",
    "ivl",
    "lastIvl",
    "factor",
    "time",
    "type",
)


class AnkiReplacementAdapter:
    def __init__(self, collection: Any):
        self.collection = collection
        self.db = collection.db

    def ensure_supported(self) -> None:
        try:
            import aqt
        except ImportError as exc:
            raise AdapterError(tr("Anki 运行环境不可用", "Anki runtime is not available")) from exc
        version = _parse_version(getattr(aqt, "appVersion", ""))
        if version[:2] != (26, 9):
            raise AdapterError(
                tr(
                    "需要 Anki 26.9.x，当前版本为 {version}",
                    "Anki 26.9.x is required, but the current version is {version}.",
                ).format(version=getattr(aqt, "appVersion", "unknown"))
            )
        if not hasattr(self.collection.sched, "answer_card"):
            raise AdapterError(
                tr(
                    "Anki 排程器的 answer_card() 不可用",
                    "Anki scheduler answer_card() is unavailable.",
                )
            )

    def replace_many(
        self,
        records: list[ReplacementRecord],
        *,
        selected_attempt_ids: set[str] | None = None,
    ) -> ReplacementResult:
        if not records:
            return ReplacementResult([], {})
        self.ensure_supported()

        requested = (
            {record.attempt_id for record in records}
            if selected_attempt_ids is None
            else set(selected_attempt_ids)
        )
        record_by_id = {record.attempt_id: record for record in records}
        requested.intersection_update(record_by_id)
        already_replaced: set[str] = set()
        eligible: set[str] = set()

        for attempt_id in requested:
            record = record_by_id[attempt_id]
            current_revlog = self._row_by_id("revlog", record.original_revlog_id)
            if current_revlog and _already_replaced(
                current_revlog, record.original_revlog
            ):
                already_replaced.add(attempt_id)
                continue
            current_card = self._row_by_id("cards", record.card_id)
            if (
                current_card
                and current_revlog
                and int(current_revlog.get("ease", 0)) == 3
                and int(current_revlog.get("cid", 0)) == record.card_id
                and _same_scheduling_state(current_card, record.post_card)
            ):
                eligible.add(attempt_id)

        if not eligible:
            skipped = bool(requested - already_replaced)
            return ReplacementResult(
                [],
                {},
                skipped_reason=(
                    tr(
                        "选中的复习记录已变化，已保留原评分",
                        "The selected review records changed; the original ratings were kept.",
                    )
                    if skipped
                    else None
                ),
            )

        ordered = sorted(
            records,
            key=lambda record: (
                record.reviewer_undo_step
                if record.reviewer_undo_step is not None
                else -1
            ),
        )
        if any(
            record.reviewer_undo_step is None or not record.original_answer_b64
            for record in ordered
        ):
            return ReplacementResult(
                sorted(already_replaced),
                {},
                skipped_reason=tr(
                    "复习记录不完整，已保留原评分",
                    "The review records are incomplete; the original ratings were kept.",
                ),
            )

        for record in ordered:
            current_revlog = self._row_by_id("revlog", record.original_revlog_id)
            if current_revlog is None:
                return ReplacementResult(
                    sorted(already_replaced),
                    {},
                    skipped_reason=tr(
                        "Anki 复习记录已变化，已保留原评分",
                        "Anki review records changed; the original ratings were kept.",
                    ),
                )
            if record.attempt_id in already_replaced:
                continue
            if not _same_row(current_revlog, record.original_revlog):
                return ReplacementResult(
                    sorted(already_replaced),
                    {},
                    skipped_reason=tr(
                        "Anki 复习记录已变化，已保留原评分",
                        "Anki review records changed; the original ratings were kept.",
                    ),
                )

        current_status = self.collection.undo_status()
        latest_step = int(ordered[-1].reviewer_undo_step or 0)
        if (
            getattr(current_status, "undo", "") != "Answer Card"
            or int(current_status.last_step) != latest_step
        ):
            return ReplacementResult(
                sorted(already_replaced),
                {},
                skipped_reason=tr(
                    "Anki 撤销历史已变化，已保留原评分",
                    "Anki's undo history changed; the original ratings were kept.",
                ),
            )

        scheduler_reps = int(self.collection.sched.reps)
        undone: list[ReplacementRecord] = []
        replayed: list[ReplacementRecord] = []
        undo_steps: dict[str, int] = {}
        selected = eligible.union(already_replaced)

        try:
            for record in reversed(ordered):
                status = self.collection.undo_status()
                if getattr(status, "undo", "") != "Answer Card":
                    self._restore_undone(undone, undo_steps)
                    return ReplacementResult(
                        sorted(already_replaced),
                        undo_steps,
                        skipped_reason=tr(
                            "复习之间有其他操作，已保留原评分",
                            "Another action occurred between reviews; the original ratings were kept.",
                        ),
                    )

                self.collection.undo()
                undone.append(record)
                current_card = self._row_by_id("cards", record.card_id)
                current_revlog = self._row_by_id(
                    "revlog", record.original_revlog_id
                )
                restored_original_state = (
                    current_revlog is None
                    and current_card is not None
                    and _same_scheduling_state(current_card, record.original_card)
                )
                if not restored_original_state:
                    self._restore_undone(undone, undo_steps)
                    return ReplacementResult(
                        sorted(already_replaced),
                        undo_steps,
                        skipped_reason=tr(
                            "Anki 撤销记录与复习卡片不匹配，已保留原评分",
                            "Anki's undo entry does not match the review card; the original ratings were kept.",
                        ),
                    )

            for record in ordered:
                rating = 4 if record.attempt_id in selected else record.original_ease
                self._answer_record(record, rating=rating)
                replayed.append(record)
                undo_steps[record.attempt_id] = int(
                    self.collection.undo_status().last_step
                )
        except Exception as exc:
            try:
                if replayed:
                    for _record in reversed(replayed):
                        self.collection.undo()
                    undone.clear()
                    replayed.clear()
                    for record in ordered:
                        self._answer_record(record, rating=record.original_ease)
                        undo_steps[record.attempt_id] = int(
                            self.collection.undo_status().last_step
                        )
                else:
                    self._restore_undone(undone, undo_steps)
            except Exception as rollback_error:
                raise AdapterRecoveryError(
                    tr(
                        "复习评分失败，恢复原评分也失败：{error}",
                        "Review rating failed, and restoring the original rating also failed: {error}",
                    ).format(error=rollback_error)
                ) from exc
            return ReplacementResult(
                [],
                undo_steps,
                failure=f"{exc}",
            )
        finally:
            self.collection.sched.reps = scheduler_reps

        return ReplacementResult(sorted(eligible), undo_steps)

    def _restore_undone(
        self,
        undone: list[ReplacementRecord],
        undo_steps: dict[str, int],
    ) -> None:
        while undone:
            record = undone[-1]
            self.collection.redo()
            undo_steps[record.attempt_id] = int(
                self.collection.undo_status().last_step
            )
            undone.pop()

    def _answer_record(self, record: ReplacementRecord, *, rating: int) -> None:
        from anki.scheduler.v3 import CardAnswer

        card = self.collection.get_card(record.card_id)
        card.start_timer()
        states = self.collection._backend.get_scheduling_states(record.card_id)
        answer = self.collection.sched.build_answer(
            card=card,
            states=states,
            rating={
                1: CardAnswer.AGAIN,
                2: CardAnswer.HARD,
                3: CardAnswer.GOOD,
                4: CardAnswer.EASY,
            }[rating],
        )
        answer.answered_at_millis = record.original_revlog_id
        answer.milliseconds_taken = int(record.original_revlog["time"])
        self.collection.sched.answer_card(answer)

    def _row_by_id(self, table: str, row_id: int) -> dict[str, Any] | None:
        columns = CARD_COLUMNS if table == "cards" else REVLOG_COLUMNS
        row = self.db.first(
            f"select {', '.join(columns)} from {table} where id=?",
            row_id,
        )
        return dict(zip(columns, row)) if row else None


def _parse_version(value: str) -> tuple[int, int, int]:
    pieces: list[int] = []
    for part in value.replace("beta", ".").replace("alpha", ".").split("."):
        digits = "".join(char for char in part if char.isdigit())
        if digits:
            pieces.append(int(digits))
        if len(pieces) == 3:
            break
    while len(pieces) < 3:
        pieces.append(0)
    return tuple(pieces[:3])


def _same_scheduling_state(
    current: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    fields = (
        "did",
        "ord",
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
    )
    return all(current.get(field) == expected.get(field) for field in fields)


def _same_row(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(current.get(field) == expected.get(field) for field in REVLOG_COLUMNS)


def _already_replaced(
    current_revlog: dict[str, Any],
    original_revlog: dict[str, Any],
) -> bool:
    return (
        int(current_revlog.get("ease", 0)) == 4
        and current_revlog.get("id") == original_revlog.get("id")
        and current_revlog.get("cid") == original_revlog.get("cid")
        and current_revlog.get("time") == original_revlog.get("time")
    )
