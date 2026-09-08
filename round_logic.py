"""
Round orchestration for the BANKERU.

Ties together game_logic.py (deck/hand/pot math) and group_logic.py
(matchmaking/turns) into the actual round lifecycle described in
Rulebook Sections 1, 2, 3, 4, and 8.
"""

import time
import uuid

from game_logic import (
    ensure_pre_round_deck_sufficient, deal_hand_cards, draw_top_card,
    return_hand_to_dropped_pile_by_id, release_hand_to_discard_by_id,
    evaluate_play, is_pair_auto_win, apply_win, apply_loss, validate_bet,
    TURN_TIMEOUT_SECONDS,
)
from group_logic import (
    group_is_ready_to_deal, get_active_members_in_seat_order,
    next_starting_player, next_turn_player, player_can_afford_round,
    record_missed_round, reset_missed_rounds, should_remove_player,
    leave_group,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def get_house_id(cur) -> str:
    cur.execute("SELECT id FROM player WHERE is_house = 1")
    return cur.fetchone()["id"]


def get_stake_amount(cur, group_id: str) -> int:
    cur.execute(
        "SELECT st.amount FROM stake_tier st "
        "JOIN game_group g ON g.stake_tier_id = st.id WHERE g.id = ?",
        (group_id,),
    )
    return cur.fetchone()["amount"]


# ---------------------------------------------------------------------------
# Starting a new round (Rulebook Sections 1, 4, 8)
# ---------------------------------------------------------------------------

def start_new_round(cur, group_id: str, previous_round_id: str = None):
    """
    Deals a fresh round: filters out players who can't afford it
    (Section 4 - disconnect for balance/not-ready), runs the pre-round
    deck check (Section 8), seeds a deck if this is the very first round
    for the group, deals 2 cards to every eligible player, sets the
    starting/turn player (rotated clockwise per Section 4), and returns
    the new round_id plus the list of disconnected player_ids for this
    round (so the caller can notify them).
    """
    from game_logic import seed_group_deck, count_pile

    members = get_active_members_in_seat_order(cur, group_id)
    if not group_is_ready_to_deal(cur, group_id):
        return None, [], "not_enough_players"

    # Seed the deck on first-ever round for this group.
    cur.execute(
        "SELECT COUNT(*) AS c FROM deck_card WHERE group_id = ?", (group_id,)
    )
    if cur.fetchone()["c"] == 0:
        seed_group_deck(cur, group_id, num_players=len(members))

    # Section 4: disconnect players who can't afford the ante this round.
    eligible, disconnected = [], []
    for pid in members:
        if player_can_afford_round(cur, pid, group_id):
            eligible.append(pid)
        else:
            disconnected.append(pid)
            record_missed_round(cur, pid)
            if should_remove_player(cur, pid):
                leave_group(cur, group_id, pid)

    if len(eligible) < 3:
        # Not enough eligible players even though group has >=3 seated -
        # can't deal this round. Caller should keep waiting.
        return None, disconnected, "not_enough_eligible"

    # Rulebook Section 1 (initial ante) & Section 6 (re-ante after a full
    # win): whenever the pot is at zero at the start of a round, every
    # eligible player pays the tier's starting ante into the pot before
    # cards are dealt. If the pot is non-zero (carried over from a
    # partial win/loss round), no new ante is collected.
    cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (group_id,))
    pot_total = cur.fetchone()["pot_total"]
    if pot_total == 0:
        ante_amount = get_stake_amount(cur, group_id)
        now_ts = time.time()
        for pid in eligible:
            cur.execute(
                "UPDATE player SET balance = balance - ? WHERE id = ?",
                (ante_amount, pid),
            )
            cur.execute(
                "INSERT INTO \"transaction\" (id, player_id, type, amount, created_at) "
                "VALUES (?, ?, 'ante', ?, ?)",
                (_new_id(), pid, -ante_amount, now_ts),
            )
        cur.execute(
            "UPDATE game_group SET pot_total = pot_total + ? WHERE id = ?",
            (ante_amount * len(eligible), group_id),
        )

    # Section 8: pre-round deck check, players x 3 minimum.
    ensure_pre_round_deck_sufficient(cur, group_id)

    # Defensive safety net: if the deck still can't meet the minimum even
    # after reshuffling every available pile, the group's deck capacity
    # itself is insufficient for this many players - fail cleanly here
    # rather than crash mid-deal or mid-round.
    from game_logic import count_pile, DECK_BUFFER_MULTIPLIER
    draw_available = count_pile(cur, group_id, "draw")
    if draw_available < len(eligible) * DECK_BUFFER_MULTIPLIER:
        return None, disconnected, "insufficient_deck_capacity"

    round_number = _next_round_number(cur, group_id)
    starting_player = next_starting_player(cur, group_id, previous_round_id)
    # If the rotated starter is ineligible this round, advance to the
    # next eligible player in seat order so the round can still start.
    if starting_player not in eligible:
        rotated = get_active_members_in_seat_order(cur, group_id)
        for pid in rotated:
            if pid in eligible:
                starting_player = pid
                break

    round_id = _new_id()
    now = time.time()
    cur.execute(
        "INSERT INTO round (id, group_id, round_number, starting_player_id, "
        "turn_player_id, turn_deadline, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (round_id, group_id, round_number, starting_player, starting_player,
         now + TURN_TIMEOUT_SECONDS, now),
    )
    cur.execute(
        "UPDATE game_group SET status = 'active', current_round_id = ? WHERE id = ?",
        (round_id, group_id),
    )

    for pid in eligible:
        card1, card1_id, card2, card2_id = deal_hand_cards(cur, group_id)
        cur.execute(
            "INSERT INTO hand (id, round_id, player_id, card1, card2, "
            "card1_deck_card_id, card2_deck_card_id, is_pair) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id(), round_id, pid, card1, card2, card1_id, card2_id,
             1 if card1 == card2 else 0),
        )
        reset_missed_rounds(cur, pid)

    return round_id, disconnected, None


def _next_round_number(cur, group_id: str) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(round_number), 0) AS m FROM round WHERE group_id = ?",
        (group_id,),
    )
    return cur.fetchone()["m"] + 1


def get_player_hand(cur, round_id: str, player_id: str):
    cur.execute(
        "SELECT * FROM hand WHERE round_id = ? AND player_id = ?",
        (round_id, player_id),
    )
    return cur.fetchone()


def get_current_turn_player(cur, round_id: str):
    cur.execute("SELECT turn_player_id, turn_deadline, group_id FROM round WHERE id = ?", (round_id,))
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Turn actions: play, drop, auto-drop on timeout (Rulebook Section 2)
# ---------------------------------------------------------------------------

def handle_pair_auto_win(cur, round_id: str, player_id: str):
    """
    Rulebook Section 3: a pair is an automatic win, claimed on the
    player's turn (not immediately when dealt). This still goes through
    the normal payout path - it just skips the card-reveal step.
    """
    hand = get_player_hand(cur, round_id, player_id)
    group_id = _group_id_for_round(cur, round_id)
    house_id = get_house_id(cur)

    cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (group_id,))
    pot_total = cur.fetchone()["pot_total"]
    # A pair auto-win claims the full pot, same as any full win.
    result = apply_win(cur, group_id, player_id, house_id, round_id, bet_amount=pot_total)

    now = time.time()
    cur.execute(
        "UPDATE hand SET action = 'played', bet_amount = ?, result = 'win', "
        "revealed_at = ? WHERE id = ?",
        (pot_total, now, hand["id"]),
    )
    _end_round(cur, round_id)
    return result


def handle_play(cur, round_id: str, player_id: str, bet_amount: int, shuffle_first: bool):
    """
    Rulebook Section 2: player commits a bet, chooses drop-top-card or
    reshuffle-first, the card is revealed, hand evaluated, pot/balance
    updated, and the player's cards are revealed to the group.

    Rulebook Section 6 clarification: a FULL win (bet == pot) ends the
    round immediately and resets the pot - the next round deals fresh
    hands and everyone re-antes. A PARTIAL win or a LOSS does NOT end
    the round: "the game continues with the remaining pot" means the
    turn passes to the next player using the SAME dealt hands from this
    round, not a fresh deal. The round only ends once every seated
    player has acted (played or dropped) without a full win occurring.

    Returns a result dict or raises ValueError with a user-facing reason
    if the bet is invalid.
    """
    group_id = _group_id_for_round(cur, round_id)
    valid, reason = validate_bet(cur, group_id, player_id, bet_amount)
    if not valid:
        raise ValueError(reason)

    hand = get_player_hand(cur, round_id, player_id)
    revealed_card = draw_top_card(cur, group_id, shuffle_first=shuffle_first)
    outcome = evaluate_play(hand["card1"], hand["card2"], revealed_card)

    house_id = get_house_id(cur)
    if outcome == "win":
        result = apply_win(cur, group_id, player_id, house_id, round_id, bet_amount)
    else:
        apply_loss(cur, group_id, player_id, round_id, bet_amount)
        result = {"win_amount": 0, "player_payout": 0, "commission": 0,
                  "is_full_win": False, "new_pot_total": None}

    now = time.time()
    cur.execute(
        "UPDATE hand SET action = 'played', bet_amount = ?, used_reshuffle = ?, "
        "revealed_card = ?, result = ?, revealed_at = ? WHERE id = ?",
        (bet_amount, 1 if shuffle_first else 0, revealed_card, outcome, now, hand["id"]),
    )

    result["outcome"] = outcome
    result["revealed_card"] = revealed_card
    result["card1"] = hand["card1"]
    result["card2"] = hand["card2"]

    if outcome == "win" and result.get("is_full_win"):
        # Full win: round ends immediately, pot already reset by apply_win.
        _end_round(cur, round_id)
        result["round_ended"] = True
    elif _all_active_players_have_acted(cur, round_id, group_id):
        # Every seated player has now played or dropped this round.
        _end_round(cur, round_id)
        result["round_ended"] = True
    else:
        # Partial win or loss, and players remain who haven't acted yet -
        # continue the SAME round: advance turn to the next unacted player.
        _advance_turn_to_next_unacted(cur, round_id, group_id, player_id)
        result["round_ended"] = False

    return result


def handle_drop(cur, round_id: str, player_id: str, timed_out: bool = False):
    """
    Rulebook Section 2: drop is free and always allowed. The top card
    is NOT revealed - it carries over to the next player untouched.
    The player's own hand IS revealed publicly after dropping.

    Rulebook Section 8: if EVERY seated player drops (nobody plays at
    all this round), hands return to the dropped_hand pile and a fresh
    round is dealt immediately. If some players played and this player
    is simply the last to act, the round ends normally (see
    _all_active_players_have_acted) and the next round deals fresh
    hands as usual - the special dropped_hand-pile handling only
    applies to the all-drop case, since only then were the cards never
    revealed to anyone at all.
    """
    hand = get_player_hand(cur, round_id, player_id)
    group_id = _group_id_for_round(cur, round_id)
    now = time.time()
    action = "auto_dropped" if timed_out else "dropped"
    cur.execute(
        "UPDATE hand SET action = ?, revealed_at = ? WHERE id = ?",
        (action, now, hand["id"]),
    )

    all_dropped = _all_active_players_have_dropped(cur, round_id, group_id)
    if all_dropped:
        _handle_full_drop_redeal(cur, round_id, group_id)
        return {"card1": hand["card1"], "card2": hand["card2"],
                "all_dropped": True, "round_ended": True}

    if _all_active_players_have_acted(cur, round_id, group_id):
        _end_round(cur, round_id)
        return {"card1": hand["card1"], "card2": hand["card2"],
                "all_dropped": False, "round_ended": True}

    _advance_turn_to_next_unacted(cur, round_id, group_id, player_id)
    return {"card1": hand["card1"], "card2": hand["card2"],
            "all_dropped": False, "round_ended": False}


def _all_active_players_have_acted(cur, round_id: str, group_id: str) -> bool:
    """True once every player dealt into this round has played or dropped."""
    members = get_active_members_in_seat_order(cur, group_id)
    cur.execute(
        "SELECT player_id, action FROM hand WHERE round_id = ?", (round_id,)
    )
    hands_by_player = {row["player_id"]: row["action"] for row in cur.fetchall()}
    for pid in members:
        action = hands_by_player.get(pid)
        if action is None:
            continue  # wasn't dealt in (disconnected this round)
        if action == "pending":
            return False
    return True


def _advance_turn_to_next_unacted(cur, round_id: str, group_id: str, current_player_id: str):
    """
    Advances the turn to the next seated player (clockwise) who has
    NOT yet acted (played or dropped) in this round. Skips players who
    already acted earlier in this same round.
    """
    members = get_active_members_in_seat_order(cur, group_id)
    cur.execute(
        "SELECT player_id, action FROM hand WHERE round_id = ?", (round_id,)
    )
    hands_by_player = {row["player_id"]: row["action"] for row in cur.fetchall()}

    if current_player_id not in members:
        start_idx = 0
    else:
        start_idx = members.index(current_player_id)

    for offset in range(1, len(members) + 1):
        candidate = members[(start_idx + offset) % len(members)]
        action = hands_by_player.get(candidate)
        if action == "pending":
            now = time.time()
            cur.execute(
                "UPDATE round SET turn_player_id = ?, turn_deadline = ? WHERE id = ?",
                (candidate, now + TURN_TIMEOUT_SECONDS, round_id),
            )
            return candidate
    return None  # everyone has acted (caller should have already ended the round)


def _all_active_players_have_dropped(cur, round_id: str, group_id: str) -> bool:
    members = get_active_members_in_seat_order(cur, group_id)
    cur.execute(
        "SELECT player_id, action FROM hand WHERE round_id = ?", (round_id,)
    )
    hands_by_player = {row["player_id"]: row["action"] for row in cur.fetchall()}
    for pid in members:
        action = hands_by_player.get(pid)
        if action is None:
            continue  # wasn't dealt in (disconnected this round)
        if action not in ("dropped", "auto_dropped"):
            return False
    return True


def _handle_full_drop_redeal(cur, round_id: str, group_id: str):
    """
    Rulebook Section 8: if all players drop, everyone's hands return to
    the dropped_hand pile (not discard) and a fresh round is dealt.
    """
    cur.execute(
        "SELECT card1_deck_card_id, card2_deck_card_id FROM hand WHERE round_id = ?",
        (round_id,),
    )
    for row in cur.fetchall():
        return_hand_to_dropped_pile_by_id(
            cur, row["card1_deck_card_id"], row["card2_deck_card_id"]
        )

    _end_round(cur, round_id, ended_without_result=True, release_hands=False)
    # Caller (bot handler) is responsible for calling start_new_round()
    # again after this returns, since that also needs to notify players.


def _advance_turn(cur, round_id: str, group_id: str, current_player_id: str):
    next_pid = next_turn_player(cur, group_id, current_player_id)
    now = time.time()
    cur.execute(
        "UPDATE round SET turn_player_id = ?, turn_deadline = ? WHERE id = ?",
        (next_pid, now + TURN_TIMEOUT_SECONDS, round_id),
    )


def _group_id_for_round(cur, round_id: str) -> str:
    cur.execute("SELECT group_id FROM round WHERE id = ?", (round_id,))
    return cur.fetchone()["group_id"]


def _end_round(cur, round_id: str, ended_without_result: bool = False, release_hands: bool = True):
    """
    A round ends when either a full win occurs, or every seated player
    has acted (played or dropped) without one. release_hands controls
    whether this call also returns every dealt hand's two cards to the
    discard pile - true for a normal end (cards were already revealed
    per the reveal-after-action rule, so they're recyclable via the
    standard discard-pile reshuffle path). It's false when called from
    the full-drop path, since that path already moved the cards to the
    dropped_hand pile itself (they were never revealed at all).
    """
    if release_hands:
        cur.execute(
            "SELECT card1_deck_card_id, card2_deck_card_id FROM hand WHERE round_id = ?",
            (round_id,),
        )
        for row in cur.fetchall():
            release_hand_to_discard_by_id(
                cur, row["card1_deck_card_id"], row["card2_deck_card_id"]
            )

    now = time.time()
    cur.execute("UPDATE round SET ended_at = ? WHERE id = ?", (now, round_id))
    group_id = _group_id_for_round(cur, round_id)
    cur.execute(
        "UPDATE game_group SET status = 'waiting', current_round_id = NULL WHERE id = ?",
        (group_id,),
    )
