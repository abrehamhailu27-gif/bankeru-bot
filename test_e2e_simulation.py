"""
End-to-end simulation: plays several real rounds through the actual
logic layers (db, game_logic, group_logic, round_logic) - the same
code bot.py calls - without touching the Telegram network layer.
Verifies balances stay consistent and no rule is violated across a
realistic multi-round session.
"""

import os
import time
import uuid

import db as db_module
db_module.DB_PATH = "test_e2e.db"
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


def total_player_balance(cur):
    cur.execute("SELECT COALESCE(SUM(balance), 0) AS t FROM player")
    return cur.fetchone()["t"]


def total_pot_balance(cur):
    """Money currently escrowed in group pots - not lost, just not yet paid out."""
    cur.execute("SELECT COALESCE(SUM(pot_total), 0) AS t FROM game_group")
    return cur.fetchone()["t"]


def total_system_balance(cur):
    """
    The true conservation invariant: player balances + house balance +
    everything sitting in pots (escrowed, not yet won by anyone) must
    always equal the original total, since no money enters or leaves
    the system during gameplay (only /deposit and /withdraw do that,
    which aren't exercised in this simulation).
    """
    return total_player_balance(cur) + total_pot_balance(cur)


def run_simulation():
    with db_transaction() as (conn, cur):
        tier_id = grp.get_or_create_stake_tier(cur, 5, is_custom=False)
        gid = grp.create_group(cur, tier_id, telegram_chat_id=999)

        names = ["Abebe", "Kebede", "Almaz", "Tigist"]
        pids = []
        for n in names:
            pid = make_player(cur, n, balance=50)
            grp.join_group(cur, gid, pid)
            pids.append(pid)

        starting_total = total_system_balance(cur)
        print(f"Starting total balance (players + house + pots): {starting_total}")

        rounds_played = 0
        prev_round_id = None
        max_rounds = 30  # safety cap against infinite loop bugs

        while rounds_played < max_rounds:
            round_id, disconnected, err = rl.start_new_round(cur, gid, prev_round_id)
            if err is not None:
                print(f"Round could not start: {err}")
                break

            rounds_played += 1
            print(f"\n--- Round {rounds_played} (id={round_id[:8]}) ---")
            cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (gid,))
            print(f"Pot at start: {cur.fetchone()['pot_total']}")

            actions_this_round = 0
            while True:
                turn = rl.get_current_turn_player(cur, round_id)
                cur.execute("SELECT ended_at FROM round WHERE id = ?", (round_id,))
                if cur.fetchone()["ended_at"] is not None:
                    break
                current = turn["turn_player_id"]
                cur.execute("SELECT display_name FROM player WHERE id = ?", (current,))
                name = cur.fetchone()["display_name"]

                hand = rl.get_player_hand(cur, round_id, current)
                actions_this_round += 1
                assert actions_this_round <= len(pids) + 1, "turn loop did not terminate!"

                if hand["is_pair"]:
                    result = rl.handle_pair_auto_win(cur, round_id, current)
                    print(f"  {name} claims PAIR auto-win: {result}")
                    continue

                cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (gid,))
                pot = cur.fetchone()["pot_total"]
                cur.execute("SELECT balance FROM player WHERE id = ?", (current,))
                balance = cur.fetchone()["balance"]

                # Simple strategy: alternate play/drop, bet a small safe amount.
                if actions_this_round % 2 == 0 and pot > 0 and balance > 0:
                    bet = min(2, pot, balance)
                    try:
                        result = rl.handle_play(cur, round_id, current, bet, shuffle_first=(actions_this_round % 3 == 0))
                        print(f"  {name} plays bet={bet} hand={hand['card1']}-{hand['card2']} "
                              f"revealed={result['revealed_card']} -> {result['outcome']} "
                              f"(round_ended={result.get('round_ended')})")
                    except ValueError as e:
                        print(f"  {name} bet rejected ({e}), dropping instead")
                        result = rl.handle_drop(cur, round_id, current)
                        print(f"  {name} drops (all_dropped={result['all_dropped']}, "
                              f"round_ended={result.get('round_ended')})")
                else:
                    result = rl.handle_drop(cur, round_id, current)
                    print(f"  {name} drops (all_dropped={result['all_dropped']}, "
                          f"round_ended={result.get('round_ended')})")

            prev_round_id = round_id

            # Integrity check after every round: total balance across all
            # players + house + escrowed pots must never drift (money
            # only ever moves between these three places, never created
            # or destroyed).
            current_total = total_system_balance(cur)
            if current_total != starting_total:
                print(f"!!! BALANCE INTEGRITY VIOLATION: {current_total} != {starting_total} "
                      f"(players={total_player_balance(cur)}, pots={total_pot_balance(cur)})")
                return False

        print(f"\n--- Simulation complete: {rounds_played} rounds played ---")
        final_total = total_system_balance(cur)
        print(f"Final total balance: {final_total} (started at {starting_total})")

        cur.execute("SELECT display_name, balance FROM player WHERE is_house = 0")
        for row in cur.fetchall():
            print(f"  {row['display_name']}: {row['balance']}")
        cur.execute("SELECT balance FROM player WHERE is_house = 1")
        print(f"  HOUSE: {cur.fetchone()['balance']}")

        assert final_total == starting_total, "Money was created or destroyed!"
        print(f"  (pot(s) still holding: {total_pot_balance(cur)})")
        print("\nBALANCE INTEGRITY: PASS (no money created or destroyed across all rounds)")
        return True


if __name__ == "__main__":
    ok = run_simulation()
    if os.path.exists(db_module.DB_PATH):
        os.remove(db_module.DB_PATH)
    exit(0 if ok else 1)
