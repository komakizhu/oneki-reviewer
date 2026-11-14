from __future__ import annotations

import math
from statistics import median
from typing import Iterable

from .i18n import tr
from .models import Attempt, DailyStats, Decision, RuleConfig


VALID_STRATEGIES = {"fixed", "percent", "smart"}
VALID_QUOTA_SCOPES = {"eligible_pool", "all_reviewed"}
VALID_SMART_MINIMUM_MODES = {"percent", "count"}


def validate_config(config: RuleConfig) -> None:
    if config.strategy not in VALID_STRATEGIES:
        raise ValueError(f"unsupported strategy: {config.strategy}")
    if config.threshold_ms < 0:
        raise ValueError("threshold_ms must be non-negative")
    if not 0 < config.distribution_percent <= 1:
        raise ValueError("distribution_percent must be in (0, 1]")
    if config.minimum_batch_size is not None and config.minimum_batch_size < 1:
        raise ValueError("minimum_batch_size must be at least 1")
    if config.smart_minimum_mode not in VALID_SMART_MINIMUM_MODES:
        raise ValueError(
            f"unsupported smart minimum mode: {config.smart_minimum_mode}"
        )
    if not 0 < config.smart_minimum_percent <= 1:
        raise ValueError("smart_minimum_percent must be in (0, 1]")
    if config.smart_minimum_mode == "count" and config.minimum_batch_size is None:
        raise ValueError("minimum_batch_size is required for smart count mode")
    if config.history_window < 1:
        raise ValueError("history_window must be at least 1")
    if config.smart_mad_multiplier < 0:
        raise ValueError("smart_mad_multiplier must be non-negative")
    if config.quota_enabled and not 0 < config.quota_percent <= 1:
        raise ValueError("quota_percent must be in (0, 1]")
    if config.quota_scope not in VALID_QUOTA_SCOPES:
        raise ValueError(f"unsupported quota scope: {config.quota_scope}")


def minimum_batch_size(distribution_percent: float) -> int:
    if not 0 < distribution_percent <= 1:
        raise ValueError("distribution_percent must be in (0, 1]")
    return max(1, math.ceil(1 / distribution_percent))


def smart_minimum_size(config: RuleConfig) -> int:
    if config.smart_minimum_mode == "percent":
        return minimum_batch_size(config.smart_minimum_percent)
    if config.minimum_batch_size is None:
        raise ValueError("minimum_batch_size is required for smart count mode")
    return config.minimum_batch_size


def history_allows(attempt: Attempt, history_window: int) -> bool:
    if history_window < 1:
        raise ValueError("history_window must be at least 1")
    if attempt.original_ease != 3:
        return False
    if not attempt.pass_history_known:
        return False

    current_pass = (attempt.original_ease,) + attempt.prior_ratings
    if any(rating in (1, 2) for rating in current_pass):
        return False

    previous_passes = attempt.prior_pass_ratings[: history_window - 1]
    return not any(
        rating in (1, 2)
        for pass_ratings in previous_passes
        for rating in pass_ratings
    )


def should_auto_upgrade_new_card_now(
    attempt: Attempt,
    config: RuleConfig,
    *,
    is_initial_learning: bool,
) -> bool:
    """Return whether this is one of a new card's first two fast Good answers."""
    if config.strategy != "fixed":
        return False
    if not is_initial_learning or not attempt.elapsed_known:
        return False
    if attempt.prior_ratings.count(3) >= 2:
        return False
    return (
        attempt.elapsed_ms <= config.threshold_ms
        and history_allows(attempt, config.history_window)
    )


def should_auto_upgrade_now(
    attempt: Attempt,
    config: RuleConfig,
    *,
    is_initial_learning: bool,
) -> bool:
    if is_initial_learning:
        return should_auto_upgrade_new_card_now(
            attempt, config, is_initial_learning=True
        )
    return (
        config.strategy == "fixed"
        and attempt.is_valid_candidate
        and attempt.elapsed_ms <= config.threshold_ms
        and history_allows(attempt, config.history_window)
    )


def quota_allows_increment(
    daily_stats: DailyStats,
    config: RuleConfig,
    *,
    current_reviewed_total: int,
    current_eligible_total: int,
    current_upgraded_total: int,
) -> bool:
    """Check an in-progress batch against its daily auto-upgrade quota."""
    if not config.quota_enabled:
        return True
    if config.quota_scope == "eligible_pool":
        current_denominator = current_eligible_total
        previous_denominator = daily_stats.eligible_total
    else:
        current_denominator = current_reviewed_total
        previous_denominator = daily_stats.reviewed_total

    allowed_total = math.floor(
        (previous_denominator + current_denominator) * config.quota_percent
    )
    return daily_stats.auto_upgraded + current_upgraded_total < allowed_total


def eligible_attempts(attempts: Iterable[Attempt], history_window: int) -> list[Attempt]:
    return [
        attempt
        for attempt in attempts
        if attempt.is_valid_candidate and history_allows(attempt, history_window)
    ]


def _mad(values: list[int], center: float) -> float:
    return float(median([abs(value - center) for value in values]))


def _smart_selection(
    candidates: list[Attempt], multiplier: float
) -> tuple[list[Attempt], int]:
    if not candidates:
        return [], 0
    values = [attempt.elapsed_ms for attempt in candidates]
    center = float(median(values))
    deviation = _mad(values, center)
    cutoff = int(math.floor(center + multiplier * deviation))
    selected = [
        attempt
        for attempt in candidates
        if attempt.elapsed_ms <= cutoff
    ]
    selected.sort(key=lambda attempt: (attempt.elapsed_ms, attempt.sequence))
    return selected, cutoff


def _apply_quota(
    selected: list[Attempt],
    candidates: list[Attempt],
    batch_card_count: int,
    reviewed_event_count: int,
    daily_stats: DailyStats,
    config: RuleConfig,
) -> tuple[list[Attempt], int | None]:
    if not config.quota_enabled:
        return selected, None

    if config.quota_scope == "eligible_pool":
        current_denominator = len(candidates)
        previous_denominator = daily_stats.eligible_total
    else:
        current_denominator = reviewed_event_count
        previous_denominator = daily_stats.reviewed_total

    allowed_total = math.floor(
        (previous_denominator + current_denominator) * config.quota_percent
    )
    remaining = max(0, allowed_total - daily_stats.auto_upgraded)
    ordered = sorted(selected, key=lambda attempt: (attempt.elapsed_ms, attempt.sequence))
    return ordered[:remaining], remaining


def evaluate_batch(
    attempts: Iterable[Attempt],
    batch_card_count: int,
    daily_stats: DailyStats,
    config: RuleConfig,
    reviewed_event_count: int | None = None,
) -> Decision:
    validate_config(config)
    all_attempts = list(attempts)
    if reviewed_event_count is None:
        reviewed_event_count = len(all_attempts)
    candidates = eligible_attempts(all_attempts, config.history_window)
    candidate_ids = tuple(attempt.attempt_id for attempt in candidates)
    if config.strategy == "fixed":
        minimum = 1
    elif config.strategy == "smart":
        minimum = smart_minimum_size(config)
    else:
        minimum = minimum_batch_size(config.distribution_percent)

    if config.strategy in {"percent", "smart"} and batch_card_count < minimum:
        return Decision(
            selected_attempt_ids=(),
            candidate_attempt_ids=candidate_ids,
            minimum_batch_size=minimum,
            not_applicable=True,
            reason=tr(
                "这轮复习需要至少 {minimum} 张卡，当前只有 {count} 张",
                "This review needs at least {minimum} cards; only {count} are available.",
            ).format(minimum=minimum, count=batch_card_count),
        )

    smart_cutoff = None
    if config.strategy == "fixed":
        selected = []
    elif config.strategy == "percent":
        slots = math.floor(len(candidates) * config.distribution_percent)
        ordered = sorted(candidates, key=lambda attempt: (attempt.elapsed_ms, attempt.sequence))
        selected = ordered[:slots]
    else:
        selected, smart_cutoff = _smart_selection(
            candidates, config.smart_mad_multiplier
        )

    selected, quota_remaining = _apply_quota(
        selected,
        candidates,
        batch_card_count,
        reviewed_event_count,
        daily_stats,
        config,
    )
    selected_ids = tuple(attempt.attempt_id for attempt in selected)
    return Decision(
        selected_attempt_ids=selected_ids,
        candidate_attempt_ids=candidate_ids,
        minimum_batch_size=minimum,
        not_applicable=False,
        reason=(
            tr(
                "固定模式已在答题时完成判断，本次仅汇总记录",
                "Fixed mode was applied while answering; this step only summarizes the results.",
            )
            if config.strategy == "fixed"
            else tr("已完成这轮复习的判断", "Review evaluation completed.")
        ),
        smart_cutoff_ms=smart_cutoff,
        quota_remaining=quota_remaining,
    )

