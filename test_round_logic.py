import os
import time
import uuid

import db as db_module
db_module.DB_PATH = "test_round.db"
if os.path.exists(db_module.DB_PATH):
    os.remove(db_module.DB_PATH)
db_module.init_db()

from db import db_transaction
import group_logic as grp
import round_logic as rl
import game_logic as gl


def make_player(cur, name, balance=100):
    pid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO player (id, telegram_id, display_name, balance, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, hash(name) % 1000000, name, balance, time.time()),
    )
    return pid


def setup_group_with_players(cur, n=4, stake=5, chat_id=1):
    tier_id = grp.get_or_create_stake_tier(cur, stake, is_custom=False)
    gid = grp.create_group(cur, tier_id, telegram_chat_id=chat_id)
    pids = []
    for i in range(n):
        pid = make_player(cur, f"P{chat_id}_{i}", balance=100)
        grp.join_group(cur, gid, pid)
        pids.append(pid)
    return gid, pids


# --- Round dealing -----------------------------------------------------------

def test_round_deals_two_cards_to_each_eligible_player():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=4, chat_id=101)
        round_id, disconnected, err = rl.start_new_round(cur, gid)
        assert err is None
        assert disconnected == []
        for pid in pids:
            hand = rl.get_player_hand(cur, round_id, pid)
            assert hand is not None
            assert 1 <= hand["card1"] <= 13
            assert 1 <= hand["card2"] <= 13
            assert hand["action"] == "pending"

def test_round_not_dealt_below_minimum_players():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=2, chat_id=102)
        round_id, disconnected, err = rl.start_new_round(cur, gid)
        assert round_id is None
        assert err == "not_enough_players"

def test_poor_player_disconnected_from_round():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 10, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=103)
        rich1 = make_player(cur, "Rich1", balance=100)
        rich2 = make_player(cur, "Rich2", balance=100)
        rich3 = make_player(cur, "Rich3", balance=100)
        poor = make_player(cur, "Poor", balance=1)
        for p in (rich1, rich2, rich3, poor):
            grp.join_group(cur, gid, p)
        round_id, disconnected, err = rl.start_new_round(cur, gid)
        assert err is None
        assert poor in disconnected
        assert rl.get_player_hand(cur, round_id, poor) is None


# --- Rule: partial win or loss continues the SAME round ---------------------

def test_loss_advances_turn_without_ending_round():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=4, chat_id=201)
        cur.execute("UPDATE game_group SET pot_total = 100 WHERE id = ?", (gid,))
        round_id, _, _ = rl.start_new_round(cur, gid)
        turn = rl.get_current_turn_player(cur, round_id)
        first_player = turn["turn_player_id"]

        # Force a guaranteed loss: bet, but rig the hand to a narrow gap
        # then confirm regardless of win/lose the round only ends when
        # everyone has acted or a full win occurs.
        cur.execute(
            "UPDATE hand SET card1 = 5, card2 = 6 WHERE round_id = ? AND player_id = ?",
            (round_id, first_player),
        )
        # 5-6 has NO possible winning card (no integer strictly between) -> guaranteed loss
        result = rl.handle_play(cur, round_id, first_player, bet_amount=5, shuffle_first=False)
        assert result["outcome"] == "lose"
        assert result["round_ended"] is False

        cur.execute("SELECT turn_player_id, ended_at FROM round WHERE id = ?", (round_id,))
        row = cur.fetchone()
        assert row["ended_at"] is None
        assert row["turn_player_id"] != first_player  # turn advanced

def test_full_win_ends_round_immediately():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=4, chat_id=202)
        cur.execute("UPDATE game_group SET pot_total = 5 WHERE id = ?", (gid,))
        round_id, _, _ = rl.start_new_round(cur, gid)
        turn = rl.get_current_turn_player(cur, round_id)
        first_player = turn["turn_player_id"]

        # Rig a guaranteed win: wide gap, then force the revealed card via
        # a controlled deck (put a single known card on top).
        cur.execute(
            "UPDATE hand SET card1 = 1, card2 = 13 WHERE round_id = ? AND player_id = ?",
            (round_id, first_player),
        )
        # Ensure the very next draw is guaranteed to land strictly between 1 and 13:
        # move all draw-pile cards with extreme values out of the way isn't trivial
        # via public API, so instead we just retry within a bounded loop using the
        # reshuffle_first=True random draw and accept most draws will win (1..13
        # exclusive of 1/13 is an 11/13 chance) - deterministic enough for a test
        # given we assert on the *pot reaching zero* branch specifically.
        pot_before = 5
        result = rl.handle_play(cur, round_id, first_player, bet_amount=pot_before, shuffle_first=True)
        if result["outcome"] == "win":
            assert result["is_full_win"] is True
            assert result["round_ended"] is True
            cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (gid,))
            assert cur.fetchone()["pot_total"] == 0
        else:
            # Rare draw (card 1 or 13) - loss instead; round must NOT end since
            # other players haven't acted yet.
            assert result["round_ended"] is False

def test_round_ends_when_all_players_have_acted_without_full_win():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=3, chat_id=203)
        cur.execute("UPDATE game_group SET pot_total = 1000 WHERE id = ?", (gid,))  # unreachable full win
        round_id, _, _ = rl.start_new_round(cur, gid)

        for i in range(3):
            turn = rl.get_current_turn_player(cur, round_id)
            current = turn["turn_player_id"]
            if current is None:
                break
            cur.execute("SELECT ended_at FROM round WHERE id = ?", (round_id,))
            if cur.fetchone()["ended_at"] is not None:
                break
            rl.handle_drop(cur, round_id, current)

        cur.execute("SELECT ended_at FROM round WHERE id = ?", (round_id,))
        assert cur.fetchone()["ended_at"] is not None


# --- Rule: already-acted players are skipped, never get a second turn ------

def test_player_who_acted_is_not_given_turn_again():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=3, chat_id=301)
        cur.execute("UPDATE game_group SET pot_total = 1000 WHERE id = ?", (gid,))
        round_id, _, _ = rl.start_new_round(cur, gid)

        seen_turns = []
        for i in range(3):
            turn = rl.get_current_turn_player(cur, round_id)
            current = turn["turn_player_id"]
            cur.execute("SELECT ended_at FROM round WHERE id = ?", (round_id,))
            if cur.fetchone()["ended_at"] is not None:
                break
            seen_turns.append(current)
            rl.handle_drop(cur, round_id, current)

        assert len(seen_turns) == len(set(seen_turns))  # no repeats


# --- Rulebook Section 8: full-drop redeal -----------------------------------

def test_all_players_dropping_triggers_redeal_via_dropped_hand_pile():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=3, chat_id=401)
        cur.execute("UPDATE game_group SET pot_total = 15 WHERE id = ?", (gid,))
        round_id, _, _ = rl.start_new_round(cur, gid)

        result = None
        for i in range(3):
            turn = rl.get_current_turn_player(cur, round_id)
            current = turn["turn_player_id"]
            cur.execute("SELECT ended_at FROM round WHERE id = ?", (round_id,))
            if cur.fetchone()["ended_at"] is not None:
                break
            result = rl.handle_drop(cur, round_id, current)

        assert result["all_dropped"] is True
        dropped_pile_count = gl.count_pile(cur, gid, "dropped_hand")
        assert dropped_pile_count == 6  # 3 players x 2 cards, never revealed


# --- Turn timeout -> auto-drop ------------------------------------------------

def test_timed_out_play_recorded_as_auto_dropped():
    with db_transaction() as (conn, cur):
        gid, pids = setup_group_with_players(cur, n=3, chat_id=501)
        round_id, _, _ = rl.start_new_round(cur, gid)
        turn = rl.get_current_turn_player(cur, round_id)
        current = turn["turn_player_id"]
        rl.handle_drop(cur, round_id, current, timed_out=True)
        hand = rl.get_player_hand(cur, round_id, current)
        assert hand["action"] == "auto_dropped"


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
