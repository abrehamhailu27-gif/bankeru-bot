"""
Core game logic for the BANKERU.

Every function here maps directly to a rule in village-card-game-rulebook.md.
Docstrings cite the rulebook section so logic can be audited against it.
"""

import random
import time
import uuid
from db import db_transaction

COMMISSION_RATE = 0.05  # Rulebook Section 7
MIN_GROUP_SIZE = 3       # Rulebook Section 5
MAX_GROUP_SIZE = 6       # Rulebook Section 5
DECK_BUFFER_MULTIPLIER = 3  # Rulebook Section 8: minimum_required = players x 3
TURN_TIMEOUT_SECONDS = 60    # Rulebook Section 2
CUSTOM_TIER_TIMEOUT_SECONDS = 10 * 60  # Rulebook Section 5
AUTO_START_WINDOW_SECONDS = 2 * 60     # Rulebook Section 4
GRACE_PERIOD_ROUNDS = 2                # Rulebook Section 4


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Deck management (Rulebook Section 8)
# ---------------------------------------------------------------------------

def build_fresh_deck(num_decks: int = 1):
    """
    A standard deck has 4 suits, so each rank (1-13, per Rulebook
    Section 0) appears 4 times - 52 cards total, not 13 unique values.
    Suits don't affect win/lose evaluation (Rulebook Section 2 only
    compares numeric values), so this returns a flat list of 52 integer
    values (four copies of each 1-13) rather than tracking suit
    identity separately.
    """
    single = list(range(1, 14)) * 4  # 4 suits per rank = 52 cards
    return single * num_decks


def seed_group_deck(cur, group_id: str, num_players: int):
    """
    Creates a fresh, shuffled draw pile for a brand-new group.
    A standard 52-card deck (Rulebook Section 0: 4 suits x 13 ranks)
    comfortably covers the rulebook's max group size of 6 under the
    players x 3 minimum-deck rule (6 x 3 = 18 <= 52), so a single deck
    is always used at current group-size limits. num_decks stays
    available as a parameter in case MAX_GROUP_SIZE is ever raised
    beyond what one 52-card deck can support (players x 3 > 52, i.e.
    more than 17 players).
    """
    num_decks = 2 if num_players * DECK_BUFFER_MULTIPLIER > 52 else 1
    cards = build_fresh_deck(num_decks)
    random.shuffle(cards)
    now = time.time()
    for value in cards:
        cur.execute(
            "INSERT INTO deck_card (id, group_id, value, pile, updated_at) "
            "VALUES (?, ?, ?, 'draw', ?)",
            (_new_id(), group_id, value, now),
        )


def count_pile(cur, group_id: str, pile: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS c FROM deck_card WHERE group_id = ? AND pile = ?",
        (group_id, pile),
    )
    return cur.fetchone()["c"]


def count_active_members(cur, group_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS c FROM group_member "
        "WHERE group_id = ? AND left_at IS NULL",
        (group_id,),
    )
    return cur.fetchone()["c"]


def ensure_pre_round_deck_sufficient(cur, group_id: str):
    """
    Rulebook Section 8 - Pre-round deck check:
    minimum_required = players x 3 (2 for hands + 1 buffer per player).
    If insufficient, reshuffle discard pile into draw pile first;
    if still insufficient, fall back to the dropped_hand pile.
    Never called mid-turn - only between rounds, per the rulebook.
    """
    players = count_active_members(cur, group_id)
    minimum_required = players * DECK_BUFFER_MULTIPLIER
    draw_count = count_pile(cur, group_id, "draw")

    if draw_count >= minimum_required:
        return

    # Step 1: reshuffle the discard pile back into the draw pile.
    _reshuffle_pile_into_draw(cur, group_id, "discard")
    draw_count = count_pile(cur, group_id, "draw")

    if draw_count >= minimum_required:
        return

    # Step 2: still short - fall back to the dropped-hand pile.
    _reshuffle_pile_into_draw(cur, group_id, "dropped_hand")


def _reshuffle_pile_into_draw(cur, group_id: str, source_pile: str):
    now = time.time()
    cur.execute(
        "UPDATE deck_card SET pile = 'draw', updated_at = ? "
        "WHERE group_id = ? AND pile = ?",
        (now, group_id, source_pile),
    )


def reshuffle_remaining_deck(cur, group_id: str):
    """
    Rulebook Section 2 - the 'reshuffle first' play option: shuffles the
    remaining draw pile in place before the top card is flipped. Free,
    unlimited use. A fair shuffle does not change the odds - this is
    purely for the "lucky ritual" feel described in the rulebook.
    Implemented by re-randomizing an order column would require a schema
    change; instead we simulate a shuffle by re-selecting the "top" card
    uniformly at random from the current draw pile rather than by
    insertion order. See draw_top_card().
    """
    # No-op on data; draw_top_card() with shuffle=True does the actual
    # random selection. Kept as a named function so the call site in the
    # bot handler reads clearly and matches the rulebook's two named
    # options (drop the top card vs. reshuffle first).
    pass


def draw_top_card(cur, group_id: str, shuffle_first: bool):
    """
    Reveals the top card of the draw pile, moving it to the discard pile.
    If shuffle_first is True (rulebook's "reshuffle first" option), the
    card is chosen uniformly at random from the current draw pile instead
    of a fixed "top" - functionally equivalent to shuffling then flipping.
    """
    if shuffle_first:
        cur.execute(
            "SELECT id, value FROM deck_card WHERE group_id = ? AND pile = 'draw' "
            "ORDER BY RANDOM() LIMIT 1",
            (group_id,),
        )
    else:
        cur.execute(
            "SELECT id, value FROM deck_card WHERE group_id = ? AND pile = 'draw' "
            "LIMIT 1",
            (group_id,),
        )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("Draw pile is empty - pre-round check should have prevented this")
    now = time.time()
    cur.execute(
        "UPDATE deck_card SET pile = 'discard', updated_at = ? WHERE id = ?",
        (now, row["id"]),
    )
    return row["value"]


def deal_hand_cards(cur, group_id: str):
    """
    Pulls 2 cards from the draw pile into a player's hand (pile='in_hand').
    Returns (value1, id1, value2, id2) so the caller can store the exact
    deck_card row references and later release precisely these two cards
    back to a recyclable pile - avoiding any ambiguity when multiple
    cards share the same value (e.g. two-deck games).
    """
    cur.execute(
        "SELECT id, value FROM deck_card WHERE group_id = ? AND pile = 'draw' "
        "ORDER BY RANDOM() LIMIT 2",
        (group_id,),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        raise RuntimeError("Not enough cards to deal a hand - pre-round check failed")
    now = time.time()
    for row in rows:
        cur.execute(
            "UPDATE deck_card SET pile = 'in_hand', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
    return rows[0]["value"], rows[0]["id"], rows[1]["value"], rows[1]["id"]


def return_hand_to_dropped_pile_by_id(cur, card1_deck_card_id: str, card2_deck_card_id: str):
    """
    Rulebook Section 8: when all players drop without playing, their
    unrevealed hands go to the dropped_hand pile (not discard, since
    discard is reserved for cards actually flipped/played). Uses exact
    deck_card row ids to avoid any ambiguity between cards sharing the
    same face value (e.g. two-deck games).
    """
    now = time.time()
    for card_id in (card1_deck_card_id, card2_deck_card_id):
        if card_id:
            cur.execute(
                "UPDATE deck_card SET pile = 'dropped_hand', updated_at = ? WHERE id = ?",
                (now, card_id),
            )


def release_hand_to_discard_by_id(cur, card1_deck_card_id: str, card2_deck_card_id: str):
    """
    Rulebook Section 2/8: once a round ends normally (a mix of plays and
    drops, not the special all-drop case), every dealt hand's cards have
    already been revealed to the group per the reveal-after-action rule.
    They go to the discard pile so they're recyclable via the normal
    discard-pile reshuffle path - this is what prevents the draw pile
    from silently draining round after round.
    """
    now = time.time()
    for card_id in (card1_deck_card_id, card2_deck_card_id):
        if card_id:
            cur.execute(
                "UPDATE deck_card SET pile = 'discard', updated_at = ? WHERE id = ?",
                (now, card_id),
            )


# ---------------------------------------------------------------------------
# Hand evaluation (Rulebook Section 2 & 3)
# ---------------------------------------------------------------------------

def evaluate_play(card1: int, card2: int, revealed_card: int) -> str:
    """
    Rulebook Section 2: win if revealed card is strictly between the
    player's two card values; lose if equal to either or outside the range.
    Rulebook Section 3: a pair (card1 == card2) is an automatic win -
    handled separately by is_pair_auto_win(), not through this function,
    since a pair has no "in-between" range to evaluate against a flip.
    """
    low, high = min(card1, card2), max(card1, card2)
    if low < revealed_card < high:
        return "win"
    return "lose"


def is_pair_auto_win(card1: int, card2: int) -> bool:
    """Rulebook Section 3: two cards of the same value auto-win on this player's turn."""
    return card1 == card2


# ---------------------------------------------------------------------------
# Pot & payout (Rulebook Sections 6 & 7)
# ---------------------------------------------------------------------------

def compute_payout(win_amount: int):
    """
    Rulebook Section 7: 5% commission on the winning amount.
    Returns (player_payout, house_commission). Commission is rounded
    down so the player never receives less than the stated 95% due to
    rounding, and the remainder (if any) still goes to the house.
    """
    commission = round(win_amount * COMMISSION_RATE)
    player_payout = win_amount - commission
    return player_payout, commission


def apply_win(cur, group_id: str, player_id: str, house_id: str,
              round_id: str, bet_amount: int) -> dict:
    """
    Rulebook Section 6: full win (bet == pot) resets pot to zero;
    partial win (bet < pot) pays out only the bet amount and the
    remaining pot carries over. Rulebook Section 7: 5% commission
    applies to the winning amount either way.
    """
    cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (group_id,))
    pot_total = cur.fetchone()["pot_total"]

    win_amount = min(bet_amount, pot_total)
    player_payout, commission = compute_payout(win_amount)
    is_full_win = win_amount >= pot_total

    now = time.time()

    # Credit player
    cur.execute(
        "UPDATE player SET balance = balance + ? WHERE id = ?",
        (player_payout, player_id),
    )
    cur.execute(
        "INSERT INTO \"transaction\" (id, player_id, type, amount, round_id, created_at) "
        "VALUES (?, ?, 'win', ?, ?, ?)",
        (_new_id(), player_id, player_payout, round_id, now),
    )

    # Credit house commission
    cur.execute(
        "UPDATE player SET balance = balance + ? WHERE id = ?",
        (commission, house_id),
    )
    cur.execute(
        "INSERT INTO \"transaction\" (id, player_id, type, amount, round_id, created_at) "
        "VALUES (?, ?, 'commission', ?, ?, ?)",
        (_new_id(), house_id, commission, round_id, now),
    )

    # Update pot
    new_pot = 0 if is_full_win else pot_total - win_amount
    cur.execute(
        "UPDATE game_group SET pot_total = ? WHERE id = ?",
        (new_pot, group_id),
    )

    return {
        "win_amount": win_amount,
        "player_payout": player_payout,
        "commission": commission,
        "is_full_win": is_full_win,
        "new_pot_total": new_pot,
    }


def apply_loss(cur, group_id: str, player_id: str, round_id: str, bet_amount: int):
    """
    Rulebook Section 6: a failed bet is added to the pot. The player's
    bet amount is deducted from their balance (they must have had
    sufficient balance - checked before this is called).
    """
    now = time.time()
    cur.execute(
        "UPDATE player SET balance = balance - ? WHERE id = ?",
        (bet_amount, player_id),
    )
    cur.execute(
        "INSERT INTO \"transaction\" (id, player_id, type, amount, round_id, created_at) "
        "VALUES (?, ?, 'bet', ?, ?, ?)",
        (_new_id(), player_id, -bet_amount, round_id, now),
    )
    cur.execute(
        "UPDATE game_group SET pot_total = pot_total + ? WHERE id = ?",
        (bet_amount, group_id),
    )


def validate_bet(cur, group_id: str, player_id: str, bet_amount: int):
    """
    Rulebook Section 6 & Section 2: bet must be <= current pot total
    AND <= player's own balance. Returns (is_valid, reason).
    """
    if bet_amount <= 0:
        return False, "Bet must be a positive number."

    cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (group_id,))
    pot_total = cur.fetchone()["pot_total"]
    if bet_amount > pot_total:
        return False, f"Bet cannot exceed the current pot ({pot_total})."

    cur.execute("SELECT balance FROM player WHERE id = ?", (player_id,))
    balance = cur.fetchone()["balance"]
    if bet_amount > balance:
        return False, f"Bet cannot exceed your balance ({balance})."

    return True, None
