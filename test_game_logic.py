"""
Tests for game_logic.py - run before trusting any real-money logic.
Each test name references the rulebook rule it verifies.
"""

import os
import time
import uuid

# Use a throwaway test DB so we never touch the real one.
os.environ.setdefault("TEST_MODE", "1")
import db as db_module
db_module.DB_PATH = "test_villagecard.db"
if os.path.exists(db_module.DB_PATH):
    os.remove(db_module.DB_PATH)
db_module.init_db()

import game_logic as gl
from db import db_transaction


def make_player(cur, name, balance=0):
    pid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO player (id, telegram_id, display_name, balance, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, hash(name) % 1000000, name, balance, time.time()),
    )
    return pid


def make_group(cur, stake_amount=5):
    cur.execute("SELECT id FROM stake_tier WHERE amount = ? LIMIT 1", (stake_amount,))
    tier_id = cur.fetchone()["id"]
    gid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO game_group (id, stake_tier_id, status, pot_total, created_at) "
        "VALUES (?, ?, 'active', 0, ?)",
        (gid, tier_id, time.time()),
    )
    return gid


def get_house_id(cur):
    cur.execute("SELECT id FROM player WHERE is_house = 1")
    return cur.fetchone()["id"]


# --- Rulebook Section 2: win/lose evaluation ------------------------------

def test_win_strictly_between():
    assert gl.evaluate_play(3, 12, 7) == "win"

def test_lose_equal_to_lower():
    assert gl.evaluate_play(3, 12, 3) == "lose"

def test_lose_equal_to_upper():
    assert gl.evaluate_play(3, 12, 12) == "lose"

def test_lose_below_range():
    assert gl.evaluate_play(3, 12, 1) == "lose"

def test_lose_above_range():
    assert gl.evaluate_play(3, 12, 13) == "lose"

def test_order_independence():
    # Cards can come back from the DB in either order - result must match.
    assert gl.evaluate_play(12, 3, 7) == "win"


# --- Rulebook Section 3: pairs ---------------------------------------------

def test_pair_is_auto_win():
    assert gl.is_pair_auto_win(7, 7) is True

def test_non_pair_not_auto_win():
    assert gl.is_pair_auto_win(7, 8) is False


# --- Rulebook Section 7: commission math ------------------------------------

def test_commission_is_five_percent():
    payout, commission = gl.compute_payout(100)
    assert commission == 5
    assert payout == 95

def test_commission_rounding_sums_correctly():
    # Any win amount: payout + commission must always equal the original.
    for amount in [1, 2, 3, 7, 13, 17, 33, 99, 101, 999]:
        payout, commission = gl.compute_payout(amount)
        assert payout + commission == amount, f"mismatch at {amount}"


# --- Rulebook Section 6: full win vs partial win ----------------------------

def test_full_win_resets_pot_to_zero():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Abebe", balance=50)
        gid = make_group(cur)
        house = get_house_id(cur)
        cur.execute("UPDATE game_group SET pot_total = 20 WHERE id = ?", (gid,))
        result = gl.apply_win(cur, gid, p1, house, round_id=None, bet_amount=20)
        assert result["is_full_win"] is True
        assert result["new_pot_total"] == 0
        cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (gid,))
        assert cur.fetchone()["pot_total"] == 0

def test_partial_win_leaves_remaining_pot():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Kebede", balance=50)
        gid = make_group(cur)
        house = get_house_id(cur)
        cur.execute("UPDATE game_group SET pot_total = 20 WHERE id = ?", (gid,))
        result = gl.apply_win(cur, gid, p1, house, round_id=None, bet_amount=8)
        assert result["is_full_win"] is False
        assert result["new_pot_total"] == 12
        cur.execute("SELECT balance FROM player WHERE id = ?", (p1,))
        # 8 win amount, 5% commission = round(0.4) = 0 -> payout 8
        assert cur.fetchone()["balance"] == 58

def test_win_credits_house_commission():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Almaz", balance=50)
        gid = make_group(cur)
        house = get_house_id(cur)
        cur.execute("SELECT balance FROM player WHERE id = ?", (house,))
        house_before = cur.fetchone()["balance"]
        cur.execute("UPDATE game_group SET pot_total = 100 WHERE id = ?", (gid,))
        gl.apply_win(cur, gid, p1, house, round_id=None, bet_amount=100)
        cur.execute("SELECT balance FROM player WHERE id = ?", (house,))
        house_after = cur.fetchone()["balance"]
        assert house_after - house_before == 5  # 5% of 100


# --- Rulebook Section 6: loss adds to pot -----------------------------------

def test_loss_adds_bet_to_pot_and_deducts_balance():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Tigist", balance=50)
        gid = make_group(cur)
        cur.execute("UPDATE game_group SET pot_total = 10 WHERE id = ?", (gid,))
        gl.apply_loss(cur, gid, p1, round_id=None, bet_amount=6)
        cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (gid,))
        assert cur.fetchone()["pot_total"] == 16
        cur.execute("SELECT balance FROM player WHERE id = ?", (p1,))
        assert cur.fetchone()["balance"] == 44


# --- Bet validation: pot ceiling AND balance ceiling ------------------------

def test_bet_rejected_above_pot():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Solomon", balance=100)
        gid = make_group(cur)
        cur.execute("UPDATE game_group SET pot_total = 5 WHERE id = ?", (gid,))
        valid, reason = gl.validate_bet(cur, gid, p1, 10)
        assert valid is False
        assert "pot" in reason.lower()

def test_bet_rejected_above_balance():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Hana", balance=3)
        gid = make_group(cur)
        cur.execute("UPDATE game_group SET pot_total = 100 WHERE id = ?", (gid,))
        valid, reason = gl.validate_bet(cur, gid, p1, 10)
        assert valid is False
        assert "balance" in reason.lower()

def test_bet_accepted_within_both_limits():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Meron", balance=50)
        gid = make_group(cur)
        cur.execute("UPDATE game_group SET pot_total = 20 WHERE id = ?", (gid,))
        valid, reason = gl.validate_bet(cur, gid, p1, 15)
        assert valid is True
        assert reason is None

def test_bet_rejected_if_zero_or_negative():
    with db_transaction() as (conn, cur):
        p1 = make_player(cur, "Yonas", balance=50)
        gid = make_group(cur)
        cur.execute("UPDATE game_group SET pot_total = 20 WHERE id = ?", (gid,))
        valid, _ = gl.validate_bet(cur, gid, p1, 0)
        assert valid is False


# --- Rulebook Section 8: deck management ------------------------------------

def test_fresh_deck_has_correct_ranks():
    deck = gl.build_fresh_deck(1)
    assert len(deck) == 52  # 4 suits x 13 ranks
    from collections import Counter
    counts = Counter(deck)
    assert set(counts.keys()) == set(range(1, 14))
    assert all(count == 4 for count in counts.values())  # 4 copies of each rank

def test_pre_round_check_reshuffles_discard_when_short():
    with db_transaction() as (conn, cur):
        gid = make_group(cur)
        for i in range(3):
            pid = make_player(cur, f"P{i}")
            cur.execute(
                "INSERT INTO group_member (group_id, player_id, seat_order, joined_at) "
                "VALUES (?, ?, ?, ?)",
                (gid, pid, i, time.time()),
            )
        # Only 2 cards in draw pile, but 3 players need 3*3=9 minimum.
        for v in (5, 6):
            cur.execute(
                "INSERT INTO deck_card (id, group_id, value, pile, updated_at) "
                "VALUES (?, ?, ?, 'draw', ?)",
                (str(uuid.uuid4()), gid, v, time.time()),
            )
        # 10 cards sitting in discard, enough to cover the shortfall.
        for v in range(1, 11):
            cur.execute(
                "INSERT INTO deck_card (id, group_id, value, pile, updated_at) "
                "VALUES (?, ?, ?, 'discard', ?)",
                (str(uuid.uuid4()), gid, v, time.time()),
            )
        gl.ensure_pre_round_deck_sufficient(cur, gid)
        draw_count = gl.count_pile(cur, gid, "draw")
        assert draw_count >= 9

def test_pre_round_check_minimum_formula():
    # players x 3, confirmed example: 4 players -> 12 cards minimum.
    assert 4 * gl.DECK_BUFFER_MULTIPLIER == 12


# --- Run everything ----------------------------------------------------------

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
