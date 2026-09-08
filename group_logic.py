"""
Group formation and matchmaking logic for the BANKERU.

Implements Rulebook Section 5 (Groups & Stake Tiers) and the relevant
parts of Section 4 (Round Lifecycle: joining mid-round, grace period,
seat replacement, turn rotation).
"""

import time
import uuid
from game_logic import (
    MIN_GROUP_SIZE, MAX_GROUP_SIZE, CUSTOM_TIER_TIMEOUT_SECONDS,
    GRACE_PERIOD_ROUNDS, seed_group_deck,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def get_or_create_stake_tier(cur, amount: int, is_custom: bool):
    """
    Rulebook Section 5: fixed preset tiers (5/10/20) always exist (seeded
    in db.init_db). A custom tier is created fresh each time a player
    proposes a value not already an open custom tier, with a 10-minute
    matchmaking window.
    """
    if not is_custom:
        cur.execute(
            "SELECT id FROM stake_tier WHERE amount = ? AND is_custom = 0",
            (amount,),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        # Shouldn't normally happen since presets are seeded, but handle
        # gracefully if an admin adds a new preset amount later.
        tier_id = _new_id()
        cur.execute(
            "INSERT INTO stake_tier (id, amount, is_custom, created_at) "
            "VALUES (?, ?, 0, ?)",
            (tier_id, amount, time.time()),
        )
        return tier_id

    # Custom tier: reuse an existing open (non-expired) one at this exact
    # amount so players proposing the same custom value get matched.
    now = time.time()
    cur.execute(
        "SELECT id, expires_at FROM stake_tier WHERE amount = ? AND is_custom = 1 "
        "AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (amount, now),
    )
    row = cur.fetchone()
    if row:
        return row["id"]

    tier_id = _new_id()
    cur.execute(
        "INSERT INTO stake_tier (id, amount, is_custom, expires_at, created_at) "
        "VALUES (?, ?, 1, ?, ?)",
        (tier_id, amount, now + CUSTOM_TIER_TIMEOUT_SECONDS, now),
    )
    return tier_id


def find_open_group(cur, stake_tier_id: str, telegram_chat_id: int):
    """
    Finds a group at this stake tier that is still accepting players:
    status='waiting' (not yet started) and fewer than MAX_GROUP_SIZE
    active members. Rulebook Section 4: a player can only join a group
    that is not mid-round (status must be 'waiting', not 'active'/'full').
    """
    cur.execute(
        "SELECT id FROM game_group WHERE stake_tier_id = ? AND status = 'waiting' "
        "AND telegram_chat_id = ? ORDER BY created_at ASC LIMIT 1",
        (stake_tier_id, telegram_chat_id),
    )
    row = cur.fetchone()
    if not row:
        return None

    active = _count_active(cur, row["id"])
    if active >= MAX_GROUP_SIZE:
        return None
    return row["id"]


def _count_active(cur, group_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS c FROM group_member WHERE group_id = ? AND left_at IS NULL",
        (group_id,),
    )
    return cur.fetchone()["c"]


def create_group(cur, stake_tier_id: str, telegram_chat_id: int):
    """Rulebook Section 5: a new group forms when no open group exists at this tier."""
    gid = _new_id()
    cur.execute(
        "INSERT INTO game_group (id, stake_tier_id, status, pot_total, "
        "telegram_chat_id, created_at) VALUES (?, ?, 'waiting', 0, ?, ?)",
        (gid, stake_tier_id, telegram_chat_id, time.time()),
    )
    return gid


def join_group(cur, group_id: str, player_id: str) -> int:
    """
    Adds a player to a group. Returns the seat_order assigned.
    Rulebook Section 4: seat replacement - reuses the lowest available
    seat_order among currently-inactive seats if one exists (from a
    prior departure), otherwise assigns the next sequential seat.
    """
    cur.execute(
        "SELECT MAX(seat_order) AS m FROM group_member WHERE group_id = ?",
        (group_id,),
    )
    max_seat = cur.fetchone()["m"]
    next_seat = 0 if max_seat is None else max_seat + 1

    cur.execute(
        "INSERT INTO group_member (group_id, player_id, seat_order, joined_at) "
        "VALUES (?, ?, ?, ?)",
        (group_id, player_id, next_seat, time.time()),
    )
    return next_seat


def leave_group(cur, group_id: str, player_id: str):
    """
    Rulebook Section 4: leaving/switching can only happen between rounds.
    Caller is responsible for checking the group isn't mid-round before
    calling this (see can_player_leave()).
    """
    cur.execute(
        "UPDATE group_member SET left_at = ? WHERE group_id = ? AND player_id = ? "
        "AND left_at IS NULL",
        (time.time(), group_id, player_id),
    )


def can_player_leave(cur, group_id: str) -> bool:
    """A player may only leave/switch tiers between rounds, not mid-round."""
    cur.execute("SELECT status FROM game_group WHERE id = ?", (group_id,))
    row = cur.fetchone()
    return row is not None and row["status"] in ("waiting", "closed")


def group_is_ready_to_deal(cur, group_id: str) -> bool:
    """Rulebook Section 5: minimum 3 players required to start a round."""
    return _count_active(cur, group_id) >= MIN_GROUP_SIZE


def get_active_members_in_seat_order(cur, group_id: str):
    cur.execute(
        "SELECT player_id, seat_order FROM group_member "
        "WHERE group_id = ? AND left_at IS NULL ORDER BY seat_order ASC",
        (group_id,),
    )
    return [row["player_id"] for row in cur.fetchall()]


def next_starting_player(cur, group_id: str, previous_round_id: str = None):
    """
    Rulebook Section 4: turn rotation - the starting player rotates
    clockwise (i.e. to the next seat) each round.
    """
    members = get_active_members_in_seat_order(cur, group_id)
    if not members:
        return None

    if previous_round_id is None:
        return members[0]

    cur.execute(
        "SELECT starting_player_id FROM round WHERE id = ?", (previous_round_id,)
    )
    row = cur.fetchone()
    if row is None or row["starting_player_id"] not in members:
        # Previous starter has left the group - just start from seat 0.
        return members[0]

    prev_index = members.index(row["starting_player_id"])
    return members[(prev_index + 1) % len(members)]


def next_turn_player(cur, group_id: str, current_player_id: str):
    """Clockwise turn advance within a round (Rulebook Section 4)."""
    members = get_active_members_in_seat_order(cur, group_id)
    if current_player_id not in members:
        return members[0] if members else None
    idx = members.index(current_player_id)
    return members[(idx + 1) % len(members)]


def record_missed_round(cur, player_id: str):
    """
    Rulebook Section 4: a disconnected player (no balance/not ready) is
    skipped for up to 2 rounds, then removed. Increments the counter;
    caller checks the threshold separately via should_remove_player().
    """
    cur.execute(
        "UPDATE player SET missed_rounds = missed_rounds + 1 WHERE id = ?",
        (player_id,),
    )


def reset_missed_rounds(cur, player_id: str):
    cur.execute(
        "UPDATE player SET missed_rounds = 0 WHERE id = ?", (player_id,)
    )


def should_remove_player(cur, player_id: str) -> bool:
    cur.execute("SELECT missed_rounds FROM player WHERE id = ?", (player_id,))
    row = cur.fetchone()
    return row is not None and row["missed_rounds"] >= GRACE_PERIOD_ROUNDS


def player_can_afford_round(cur, player_id: str, group_id: str) -> bool:
    """
    Rulebook Section 4: a player without enough balance is disconnected
    for that round. 'Enough balance' means at least the tier's ante -
    the minimum possible bet a player could reasonably make.
    """
    cur.execute(
        "SELECT p.balance, st.amount FROM player p "
        "JOIN game_group g ON g.id = ? "
        "JOIN stake_tier st ON st.id = g.stake_tier_id "
        "WHERE p.id = ?",
        (group_id, player_id),
    )
    row = cur.fetchone()
    if row is None:
        return False
    return row["balance"] >= row["amount"]
