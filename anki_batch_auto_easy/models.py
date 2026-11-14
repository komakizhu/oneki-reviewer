from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    card_id: int
    deck_id: int
    sequence: int
    original_ease: int
    elapsed_ms: int
    prior_ratings: tuple[int, ...]
    leaves_today_learning: bool
    answered_at_ms: int
    prior_pass_ratings: tuple[tuple[int, ...], ...] = ()
    pass_history_known: bool = True
    elapsed_known: bool = True
    batch_auto_easy_eligible: bool = True

    @property
    def is_valid_candidate(self) -> bool:
        return (
            self.original_ease == 3
            and self.leaves_today_learning
            and self.elapsed_known
            and self.batch_auto_easy_eligible
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "card_id": self.card_id,
            "deck_id": self.deck_id,
            "sequence": self.sequence,
            "original_ease": self.original_ease,
            "elapsed_ms": self.elapsed_ms,
            "prior_ratings": list(self.prior_ratings),
            "prior_pass_ratings": [
                list(ratings) for ratings in self.prior_pass_ratings
            ],
            "pass_history_known": self.pass_history_known,
            "leaves_today_learning": self.leaves_today_learning,
            "answered_at_ms": self.answered_at_ms,
            "elapsed_known": self.elapsed_known,
            "batch_auto_easy_eligible": self.batch_auto_easy_eligible,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Attempt:
        return cls(
            attempt_id=str(value["attempt_id"]),
            card_id=int(value["card_id"]),
            deck_id=int(value["deck_id"]),
            sequence=int(value["sequence"]),
            original_ease=int(value["original_ease"]),
            elapsed_ms=int(value["elapsed_ms"]),
            prior_ratings=tuple(int(item) for item in value.get("prior_ratings", [])),
            leaves_today_learning=bool(value["leaves_today_learning"]),
            answered_at_ms=int(value["answered_at_ms"]),
            prior_pass_ratings=tuple(
                tuple(int(rating) for rating in ratings)
                for ratings in value.get("prior_pass_ratings", [])
            ),
            pass_history_known=bool(value.get("pass_history_known", False)),
            elapsed_known=bool(value.get("elapsed_known", True)),
            batch_auto_easy_eligible=bool(
                value.get("batch_auto_easy_eligible", True)
            ),
        )


@dataclass(frozen=True)
class DailyStats:
    auto_upgraded: int = 0
    eligible_total: int = 0
    reviewed_total: int = 0


@dataclass(frozen=True)
class RuleConfig:
    strategy: str = "fixed"
    threshold_ms: int = 1000
    distribution_percent: float = 0.20
    minimum_batch_size: int | None = 5
    smart_minimum_mode: str = "percent"
    smart_minimum_percent: float = 0.20
    history_window: int = 2
    smart_mad_multiplier: float = 2.0
    quota_enabled: bool = False
    quota_percent: float = 0.20
    quota_scope: str = "eligible_pool"


@dataclass(frozen=True)
class Decision:
    selected_attempt_ids: tuple[str, ...]
    candidate_attempt_ids: tuple[str, ...]
    minimum_batch_size: int
    not_applicable: bool
    reason: str
    smart_cutoff_ms: int | None = None
    quota_remaining: int | None = None

