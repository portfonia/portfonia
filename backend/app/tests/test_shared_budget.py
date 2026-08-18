"""Fair-share slicing of an application-level daily budget across a
per-user fan-out (issue #128 A4, design doc §5.7 hand-off item 1)."""

from __future__ import annotations

from app.services.shared_budget import fair_share_budget


def test_only_or_last_user_may_spend_the_whole_remaining_budget() -> None:
    # users_remaining == 1 means "nobody comes after me" — holding anything
    # back would waste it, since the budget resets tomorrow.
    assert fair_share_budget(15, 1) == 15
    assert fair_share_budget(3, 1) == 3


def test_default_single_user_call_site_is_unrestricted() -> None:
    # Every pre-A4 call site (manual trigger, tests, a single-user system)
    # passes users_remaining=1 and must be unaffected by this mechanism.
    assert fair_share_budget(15, 1) == 15


def test_first_of_three_users_gets_a_third_not_the_whole_budget() -> None:
    # The bug this closes: the first user in a fixed-order fan-out could
    # consume the entire day's budget, starving every later user of the
    # SAME users every day (active_user_ids is sorted, so the order never
    # rotates). Ceil, not floor, so a budget that doesn't divide evenly is
    # fully spendable rather than leaving a permanently unusable remainder.
    assert fair_share_budget(15, 3) == 5
    assert fair_share_budget(10, 3) == 4


def test_unused_share_flows_forward_to_later_users() -> None:
    # Slices are recomputed from what is ACTUALLY left, so a user with few
    # candidates does not strand their unused share: three users, a 15-slot
    # budget, first user spends only 1.
    first = fair_share_budget(15, 3)
    assert first == 5
    spent = 1
    second = fair_share_budget(15 - spent, 2)
    assert second == 7
    third = fair_share_budget(15 - spent - second, 1)
    assert third == 7
    # Nothing is lost: every remaining slot is reachable by the last user.
    assert spent + second + third == 15


def test_no_user_can_starve_a_later_one_even_with_unlimited_candidates() -> None:
    # A user whose candidate list exceeds the whole budget is still capped at
    # their share, so the remaining users always have something left.
    budget = 15
    users = 3
    remaining = budget
    for position in range(users, 0, -1):
        share = fair_share_budget(remaining, position)
        assert share > 0, "every user in the batch must get at least one slot"
        remaining -= share


def test_exhausted_budget_yields_zero_not_a_negative_share() -> None:
    assert fair_share_budget(0, 3) == 0
    # `_attempts_today` can exceed the cap (a key's lock charges the full
    # allowance at once), so the subtraction upstream can go negative.
    assert fair_share_budget(-4, 3) == 0


def test_nonpositive_user_count_is_treated_as_a_single_caller() -> None:
    # Defensive: a caller that miscomputes its own position must not turn the
    # budget into a division error or a zero share.
    assert fair_share_budget(15, 0) == 15
    assert fair_share_budget(15, -2) == 15
