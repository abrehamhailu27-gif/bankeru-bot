import os
import time
import uuid

import db as db_module
db_module.DB_PATH = "test_group.db"
if os.path.exists(db_module.DB_PATH):
    os.remove(db_module.DB_PATH)
db_module.init_db()

from db import db_transaction
import group_logic as grp


def make_player(cur, name, balance=0):
    pid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO player (id, telegram_id, display_name, balance, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, hash(name) % 1000000, name, balance, time.time()),
    )
    return pid


# --- Rulebook Section 5: group size limits ----------------------------------

def test_group_not_ready_below_minimum():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=111)
        p1 = make_player(cur, "A")
        p2 = make_player(cur, "B")
        grp.join_group(cur, gid, p1)
        grp.join_group(cur, gid, p2)
        assert grp.group_is_ready_to_deal(cur, gid) is False

def test_group_ready_at_minimum():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=222)
        for i in range(3):
            pid = make_player(cur, f"P{i}")
            grp.join_group(cur, gid, pid)
        assert grp.group_is_ready_to_deal(cur, gid) is True

def test_full_group_not_offered_as_open():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=333)
        for i in range(6):
            pid = make_player(cur, f"Full{i}")
            grp.join_group(cur, gid, pid)
        found = grp.find_open_group(cur, tier_id, telegram_chat_id=333)
        assert found is None  # full -> a new group should be created instead

def test_new_group_created_when_none_open():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 10, is_custom=False)
        found = grp.find_open_group(cur, tier_id, telegram_chat_id=444)
        assert found is None
        gid = grp.create_group(cur, tier_id, telegram_chat_id=444)
        found_after = grp.find_open_group(cur, tier_id, telegram_chat_id=444)
        assert found_after == gid


# --- Rulebook Section 4: mid-round join restriction -------------------------

def test_cannot_leave_mid_round():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=555)
        cur.execute("UPDATE game_group SET status = 'active' WHERE id = ?", (gid,))
        assert grp.can_player_leave(cur, gid) is False

def test_can_leave_between_rounds():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=666)
        assert grp.can_player_leave(cur, gid) is True  # status='waiting' by default


# --- Rulebook Section 4: turn rotation (clockwise, rotates each round) -----

def test_first_round_starts_at_seat_zero():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=777)
        pA = make_player(cur, "SeatA")
        pB = make_player(cur, "SeatB")
        pC = make_player(cur, "SeatC")
        grp.join_group(cur, gid, pA)
        grp.join_group(cur, gid, pB)
        grp.join_group(cur, gid, pC)
        starter = grp.next_starting_player(cur, gid, previous_round_id=None)
        assert starter == pA

def test_rotation_moves_to_next_seat_each_round():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=888)
        pA = make_player(cur, "RotA")
        pB = make_player(cur, "RotB")
        pC = make_player(cur, "RotC")
        grp.join_group(cur, gid, pA)
        grp.join_group(cur, gid, pB)
        grp.join_group(cur, gid, pC)

        rid1 = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO round (id, group_id, round_number, starting_player_id, started_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (rid1, gid, pA, time.time()),
        )
        starter2 = grp.next_starting_player(cur, gid, previous_round_id=rid1)
        assert starter2 == pB  # Player A started round 1 -> Player B starts round 2

def test_turn_advances_clockwise_within_round():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=999)
        pA = make_player(cur, "TA")
        pB = make_player(cur, "TB")
        pC = make_player(cur, "TC")
        grp.join_group(cur, gid, pA)
        grp.join_group(cur, gid, pB)
        grp.join_group(cur, gid, pC)
        assert grp.next_turn_player(cur, gid, pA) == pB
        assert grp.next_turn_player(cur, gid, pB) == pC
        assert grp.next_turn_player(cur, gid, pC) == pA  # wraps around


# --- Rulebook Section 4: seat replacement -----------------------------------

def test_seat_opens_after_player_leaves():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=1010)
        p1 = make_player(cur, "Leaver")
        p2 = make_player(cur, "Stayer")
        grp.join_group(cur, gid, p1)
        grp.join_group(cur, gid, p2)
        assert grp._count_active(cur, gid) == 2
        grp.leave_group(cur, gid, p1)
        assert grp._count_active(cur, gid) == 1
        p3 = make_player(cur, "NewJoiner")
        grp.join_group(cur, gid, p3)
        assert grp._count_active(cur, gid) == 2


# --- Rulebook Section 4: grace period (2 missed rounds -> removal) ---------

def test_player_not_removed_before_two_misses():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Grace1")
        grp.record_missed_round(cur, p1)
        assert grp.should_remove_player(cur, p1) is False

def test_player_removed_after_two_misses():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Grace2")
        grp.record_missed_round(cur, p1)
        grp.record_missed_round(cur, p1)
        assert grp.should_remove_player(cur, p1) is True

def test_missed_rounds_reset_on_participation():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Grace3")
        grp.record_missed_round(cur, p1)
        grp.reset_missed_rounds(cur, p1)
        assert grp.should_remove_player(cur, p1) is False


# --- Rulebook Section 4: balance check for auto-start -----------------------

def test_player_with_insufficient_balance_flagged():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 10, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=1111)
        poor_player = make_player(cur, "Poor", balance=2)
        grp.join_group(cur, gid, poor_player)
        assert grp.player_can_afford_round(cur, poor_player, gid) is False

def test_player_with_sufficient_balance_ok():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 10, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=1212)
        rich_player = make_player(cur, "Rich", balance=50)
        grp.join_group(cur, gid, rich_player)
        assert grp.player_can_afford_round(cur, rich_player, gid) is True


# --- Rulebook Section 5: custom stake tier matching -------------------------

def test_custom_tier_same_amount_reused():
    with db_transaction() as (conn, cur):
        t1 = grp.get_or_create_stake_tier(cur, 7, is_custom=True)
        t2 = grp.get_or_create_stake_tier(cur, 7, is_custom=True)
        assert t1 == t2  # same custom amount -> same tier, so players get matched

def test_custom_tier_different_amount_different_tier():
    with db_transaction() as (conn, cur):
        t1 = grp.get_or_create_stake_tier(cur, 8, is_custom=True)
        t2 = grp.get_or_create_stake_tier(cur, 9, is_custom=True)
        assert t1 != t2


if __name__ == "__main__":
    import sys
    test_fns = [obj for name, obj in list(globals().items())
                if name.startswith("test_") and callable(obj)]
    passed, failed = 0, 0
    for fn in test_fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if os.path.exists(db_module.DB_PATH):
        os.remove(db_module.DB_PATH)
    sys.exit(1 if failed else 0)
