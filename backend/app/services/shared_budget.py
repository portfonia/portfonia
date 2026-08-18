"""Fair-share slicing of an application-level daily budget across a per-user
fan-out (issue #128 A4).

THE PATTERN THIS EXISTS TO CLOSE, stated once so the next shared budget does
not rediscover it a fourth time (design doc §5.7 hand-off item 1):

    a shared, capped daily resource
  + a fan-out that consumes it sequentially, one user at a time
  + a user order that never rotates (`active_user_ids` is sorted)
  = the same users are starved every single day, deterministically.

It has now surfaced three times with three different resources: A1's Tavily
search budget (handed to A2), A2's L1 daily fresh-analysis cap (handed to
A3), and A3's L2 inference cap. A3 fixed only its own instance, by splitting
the cap per event-kind (`theme:` / `fwd:`) — a solution that works only
because L2's candidates group on a key prefix. It explicitly does NOT
generalize to L1, whose candidates are per-user by nature (different users
hold different identifiers), which is why A3 handed the general problem
forward rather than patching again.

THE GENERAL SOLUTION: do not allocate from the day's total, allocate from
what is ACTUALLY LEFT at the moment each user is served, divided by how many
users still have to be served (including the current one). Two properties
fall out, and together they are the whole fix:

  - No user can starve a later one. A caller may spend at most its own
    share, so serving N users always leaves something for the N-1 after it.
  - No capacity is stranded. The divisor shrinks as the fan-out advances, so
    a share left unspent by an early user is re-offered to later ones, and
    the last user (`users_remaining == 1`) may spend everything that is left.

Deliberately NOT a reservation table, a rotation schedule, or a round-robin
merge of every user's candidate list. Those need the batch to know all
users' candidates up front, which means deriving each user's anomalies
before generating any user's report — a second pass over per-user state whose
only product is an ordering. The recomputed-remainder rule above achieves
the same two properties with one integer passed down the existing call
chain, and it degrades to "no restriction at all" for every single-user call
site (`users_remaining=1`), which is every pre-A4 caller.

WHAT THIS IS NOT: a quality ranking. Which of a user's own candidates are
worth their share is a SELECTION concern and stays per-user (`l1_identifiers
_for_user` keeps the caller's own |move| ordering — see its docstring). This
module only decides HOW MANY, never WHICH.
"""

from __future__ import annotations


def fair_share_budget(remaining: int, users_remaining: int) -> int:
    """How many units of a shared daily budget the current user may spend.

    *remaining* is what is left of the budget right now — re-read per user
    (e.g. from `SUM(attempt_count)`), never the day's total, since that is
    what lets an early user's unspent share flow forward instead of being
    stranded.

    *users_remaining* counts the current user plus everyone after them in
    this batch. `1` means "nobody comes after me" and returns the whole
    remainder: every pre-A4 call site passes this and is therefore
    unaffected. A non-positive value is treated as `1` — a caller that
    miscounts its own position must degrade to today's unrestricted
    behavior, not to a zero share that would silently disable the feature
    it is budgeting for.

    Rounds UP: with a budget that does not divide evenly, flooring would
    leave a remainder no user in the batch is ever permitted to claim.
    Rounding up can overshoot the even split by at most one unit per user,
    which the shrinking divisor then absorbs.
    """
    if remaining <= 0:
        return 0
    if users_remaining <= 1:
        return remaining
    return -(-remaining // users_remaining)
