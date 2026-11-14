import unittest

from anki_batch_auto_easy.core import (
    evaluate_batch,
    history_allows,
    minimum_batch_size,
    quota_allows_increment,
    should_auto_upgrade_new_card_now,
    should_auto_upgrade_now,
    smart_minimum_size,
)
from anki_batch_auto_easy.models import Attempt, DailyStats, RuleConfig


def attempt(
    attempt_id: str,
    elapsed_ms: int,
    *,
    ease: int = 3,
    prior: tuple[int, ...] = (),
    prior_passes: tuple[tuple[int, ...], ...] = (),
    leaves: bool = True,
    sequence: int = 0,
    elapsed_known: bool = True,
    batch_auto_easy_eligible: bool = True,
) -> Attempt:
    return Attempt(
        attempt_id=attempt_id,
        card_id=sequence + 1,
        deck_id=10,
        sequence=sequence,
        original_ease=ease,
        elapsed_ms=elapsed_ms,
        prior_ratings=prior,
        leaves_today_learning=leaves,
        answered_at_ms=sequence + 1,
        prior_pass_ratings=prior_passes,
        elapsed_known=elapsed_known,
        batch_auto_easy_eligible=batch_auto_easy_eligible,
    )


class CoreRulesTests(unittest.TestCase):
    def test_minimum_batch_size(self):
        self.assertEqual(minimum_batch_size(0.20), 5)
        self.assertEqual(minimum_batch_size(0.10), 10)
        self.assertEqual(minimum_batch_size(0.25), 4)

    def test_history_window_checks_full_pass_sequences(self):
        dirty_current = attempt("dirty_current", 500, prior=(1, 2, 3))
        self.assertFalse(history_allows(dirty_current, 1))

        clean_current = attempt("clean_current", 500, prior=(3, 3))
        self.assertTrue(history_allows(clean_current, 1))
        self.assertTrue(
            history_allows(
                attempt(
                    "dirty_previous",
                    500,
                    prior=(3, 3),
                    prior_passes=((1, 2, 3, 3),),
                ),
                1,
            )
        )
        self.assertFalse(
            history_allows(
                attempt(
                    "dirty_previous",
                    500,
                    prior=(3, 3),
                    prior_passes=((1, 2, 3, 3),),
                ),
                2,
            )
        )

    def test_new_card_can_upgrade_either_of_its_first_two_fast_goods(self):
        config = RuleConfig(strategy="fixed", threshold_ms=1000)
        first_good = attempt("first", 1000, leaves=False)
        second_good = attempt("second", 900, prior=(3,), leaves=False)

        self.assertTrue(
            should_auto_upgrade_new_card_now(
                first_good, config, is_initial_learning=True
            )
        )
        self.assertTrue(
            should_auto_upgrade_new_card_now(
                second_good, config, is_initial_learning=True
            )
        )

    def test_new_card_realtime_upgrade_respects_speed_history_and_attempt_limit(self):
        config = RuleConfig(strategy="fixed", threshold_ms=1000)
        cases = [
            (attempt("slow", 1001), False),
            (attempt("dirty", 500, prior=(1,)), True),
            (attempt("third_good", 500, prior=(3, 3)), True),
            (attempt("review_card", 500), False),
            (attempt("unknown_time", 0, elapsed_known=False), True),
        ]
        for candidate, is_initial_learning in cases:
            with self.subTest(candidate=candidate.attempt_id):
                self.assertFalse(
                    should_auto_upgrade_new_card_now(
                        candidate,
                        config,
                        is_initial_learning=is_initial_learning,
                    )
                )

        self.assertFalse(
            should_auto_upgrade_new_card_now(
                attempt("smart_mode", 500),
                RuleConfig(strategy="smart"),
                is_initial_learning=True,
            )
        )

    def test_non_three_and_learning_step_are_not_candidates(self):
        values = [
            attempt("again", 100, ease=1),
            attempt("hard", 100, ease=2),
            attempt("easy", 100, ease=4),
            attempt("learning", 100, leaves=False),
            attempt("unknown_time", 100, elapsed_known=False),
            attempt("not_auto_easy_eligible", 100, batch_auto_easy_eligible=False),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=4,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="fixed", threshold_ms=1000),
        )
        self.assertEqual(decision.selected_attempt_ids, ())
        self.assertEqual(decision.candidate_attempt_ids, ())

    def test_fixed_threshold_inclusive(self):
        config = RuleConfig(strategy="fixed", threshold_ms=1000)
        self.assertTrue(
            should_auto_upgrade_now(
                attempt("at", 1000), config, is_initial_learning=False
            )
        )
        self.assertFalse(
            should_auto_upgrade_now(
                attempt("over", 1001), config, is_initial_learning=False
            )
        )

    def test_fixed_batch_never_selects_delayed_upgrades(self):
        decision = evaluate_batch(
            [attempt("fast", 100)], 1, DailyStats(), RuleConfig(strategy="fixed")
        )
        self.assertEqual(decision.selected_attempt_ids, ())

    def test_fixed_review_upgrade_preserves_history_and_learning_guards(self):
        for candidate in (
            attempt("dirty_current", 100, prior=(1,)),
            attempt("dirty_previous", 100, prior_passes=((2, 3),)),
            attempt("intermediate_learning", 100, leaves=False),
            attempt("unknown_time", 0, elapsed_known=False),
            attempt("hard", 100, ease=2),
        ):
            with self.subTest(attempt=candidate.attempt_id):
                self.assertFalse(
                    should_auto_upgrade_now(
                        candidate, RuleConfig(), is_initial_learning=False
                    )
                )

    def test_percent_requires_minimum_batch(self):
        values = [attempt("a", 100), attempt("b", 200, sequence=1)]
        decision = evaluate_batch(
            values,
            batch_card_count=4,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="percent", distribution_percent=0.20),
        )
        self.assertTrue(decision.not_applicable)
        self.assertEqual(decision.selected_attempt_ids, ())

    def test_percent_selects_fastest_slot(self):
        values = [
            attempt("slow", 200, sequence=0),
            attempt("fast", 100, sequence=1),
            attempt("middle", 150, sequence=2),
            attempt("other", 300, sequence=3),
            attempt("last", 400, sequence=4),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=5,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="percent", distribution_percent=0.20),
        )
        self.assertEqual(decision.selected_attempt_ids, ("fast",))

    def test_percent_tie_uses_sequence(self):
        values = [
            attempt("later", 100, sequence=2),
            attempt("earlier", 100, sequence=1),
            attempt("third", 100, sequence=3),
            attempt("fourth", 100, sequence=4),
            attempt("fifth", 100, sequence=5),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=5,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="percent", distribution_percent=0.20),
        )
        self.assertEqual(decision.selected_attempt_ids, ("earlier",))

    def test_smart_mode_rejects_outlier(self):
        values = [
            attempt("one", 100, sequence=0),
            attempt("one_again", 100, sequence=1),
            attempt("two", 200, sequence=2),
            attempt("slow", 2000, sequence=3),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=5,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="smart", distribution_percent=0.20),
        )
        self.assertEqual(
            decision.selected_attempt_ids,
            ("one", "one_again", "two"),
        )
        self.assertEqual(decision.smart_cutoff_ms, 250)

    def test_smart_mode_mad_zero_keeps_equal_times(self):
        values = [
            attempt("a", 100, sequence=0),
            attempt("b", 100, sequence=1),
            attempt("c", 100, sequence=2),
            attempt("d", 100, sequence=3),
            attempt("e", 100, sequence=4),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=5,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="smart"),
        )
        self.assertEqual(decision.selected_attempt_ids, ("a", "b", "c", "d", "e"))

    def test_smart_mode_uses_configured_minimum(self):
        values = [attempt("a", 100), attempt("b", 110, sequence=1)]
        decision = evaluate_batch(
            values,
            batch_card_count=2,
            daily_stats=DailyStats(),
            config=RuleConfig(
                strategy="smart",
                distribution_percent=0.20,
                minimum_batch_size=3,
                smart_minimum_mode="count",
            ),
        )
        self.assertTrue(decision.not_applicable)
        self.assertEqual(decision.minimum_batch_size, 3)

    def test_smart_mode_defaults_to_percentage_minimum(self):
        values = [
            attempt("a", 100),
            attempt("b", 110, sequence=1),
            attempt("c", 120, sequence=2),
            attempt("d", 130, sequence=3),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=4,
            daily_stats=DailyStats(),
            config=RuleConfig(strategy="smart"),
        )
        self.assertTrue(decision.not_applicable)
        self.assertEqual(decision.minimum_batch_size, 5)

    def test_smart_minimum_size_supports_count_and_percentage(self):
        self.assertEqual(
            smart_minimum_size(
                RuleConfig(
                    strategy="smart",
                    smart_minimum_mode="count",
                    minimum_batch_size=7,
                )
            ),
            7,
        )
        self.assertEqual(
            smart_minimum_size(
                RuleConfig(
                    strategy="smart",
                    smart_minimum_mode="percent",
                    smart_minimum_percent=0.25,
                )
            ),
            4,
        )

    def test_quota_eligible_pool(self):
        values = [
            attempt("a", 100, sequence=0),
            attempt("b", 110, sequence=1),
            attempt("c", 120, sequence=2),
            attempt("d", 130, sequence=3),
            attempt("e", 140, sequence=4),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=5,
            daily_stats=DailyStats(auto_upgraded=1, eligible_total=5),
            config=RuleConfig(
                strategy="smart",
                threshold_ms=1000,
                quota_enabled=True,
                quota_percent=0.20,
            ),
        )
        self.assertEqual(decision.selected_attempt_ids, ("a",))
        self.assertEqual(decision.quota_remaining, 1)

    def test_quota_all_reviewed(self):
        values = [
            attempt("a", 100, sequence=0),
            attempt("same_card_again", 110, sequence=1),
        ]
        decision = evaluate_batch(
            values,
            batch_card_count=1,
            daily_stats=DailyStats(auto_upgraded=2, reviewed_total=10),
            config=RuleConfig(
                strategy="smart",
                threshold_ms=1000,
                smart_minimum_mode="count",
                minimum_batch_size=1,
                quota_enabled=True,
                quota_percent=0.20,
                quota_scope="all_reviewed",
            ),
            reviewed_event_count=2,
        )
        self.assertEqual(decision.selected_attempt_ids, ())

    def test_immediate_quota_respects_eligible_pool_and_reviewed_scopes(self):
        eligible_config = RuleConfig(
            strategy="fixed",
            quota_enabled=True,
            quota_percent=0.20,
            quota_scope="eligible_pool",
        )
        self.assertTrue(
            quota_allows_increment(
                DailyStats(),
                eligible_config,
                current_reviewed_total=5,
                current_eligible_total=5,
                current_upgraded_total=0,
            )
        )
        self.assertFalse(
            quota_allows_increment(
                DailyStats(),
                eligible_config,
                current_reviewed_total=6,
                current_eligible_total=5,
                current_upgraded_total=1,
            )
        )

        reviewed_config = RuleConfig(
            strategy="fixed",
            quota_enabled=True,
            quota_percent=0.20,
            quota_scope="all_reviewed",
        )
        self.assertTrue(
            quota_allows_increment(
                DailyStats(),
                reviewed_config,
                current_reviewed_total=5,
                current_eligible_total=1,
                current_upgraded_total=0,
            )
        )
        self.assertFalse(
            quota_allows_increment(
                DailyStats(),
                reviewed_config,
                current_reviewed_total=5,
                current_eligible_total=1,
                current_upgraded_total=1,
            )
        )


if __name__ == "__main__":
    unittest.main()

