# BANKERU — Telegram Bot (MVP)

This is a working implementation of the game described in
`village-card-game-rulebook.md`, `village-card-game-bot-commands.md`,
and `village-card-game-data-model.md`.

## What's here

| File | Purpose |
|---|---|
| `db.py` | Database schema (SQLite) — 9 tables matching the data model doc |
| `game_logic.py` | Deck, hand evaluation, pot/commission math |
| `group_logic.py` | Matchmaking, stake tiers, turn rotation, grace period |
| `round_logic.py` | Ties it all together into the full round lifecycle |
| `bot.py` | Telegram command handlers — the actual bot |
| `config.py` | Bot token / admin IDs (via environment variables) |
| `test_game_logic.py`, `test_group_logic.py`, `test_round_logic.py` | Unit tests (47 total) |
| `test_e2e_simulation.py` | Plays full simulated games and checks money is never created/destroyed |

## Before you run it

**1. Get a bot token.** Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts. You'll get a token like `123456:ABC-DEF...`.

**2. Get your own Telegram numeric ID** (to set yourself as admin). Message [@userinfobot](https://t.me/userinfobot) — it replies with your ID.

**3. Install dependencies:**
```bash
pip install python-telegram-bot==21.6
```

**4. Set environment variables:**
```bash
export VILLAGE_CARD_BOT_TOKEN="123456:ABC-DEF..."
export VILLAGE_CARD_ADMIN_IDS="your_telegram_id_here"
export VILLAGE_CARD_PAYMENT_INSTRUCTIONS="Send Telebirr to 0912345678 (Your Name)"
# Optional - set once you've created the Payments & Support group:
export VILLAGE_CARD_SUPPORT_CHAT_ID="-100xxxxxxxxxx"
```

**5. Run it:**
```bash
python3 bot.py
```

The bot will start polling Telegram for messages. Add it to a group chat (for gameplay) and message it privately (for account commands).

## Running the tests first (recommended)

Before connecting a real token, verify everything still works in your environment:

```bash
python3 test_game_logic.py      # 21 tests - deck, hand evaluation, pot/commission math
python3 test_group_logic.py     # 17 tests - matchmaking, turns, grace period
python3 test_round_logic.py     # 9 tests  - full round orchestration
python3 test_e2e_simulation.py  # plays a full simulated game, checks balance integrity
```

All of these should print `PASS` for every test and exit with code 0.

## What's implemented vs. what's still manual

**Implemented and tested:**
- Full rulebook logic: ranks, win/lose evaluation, pairs, betting limits, ante collection, full vs. partial win, commission, deck reshuffle (including the two-pile discard/dropped-hand system), pre-round deck sufficiency check, group size limits, stake tiers (preset + custom with 10-min timeout), turn rotation, 60-second turn timer with auto-drop, grace period + seat replacement, disconnect handling.
- Every player and admin command from the bot commands spec.

**Still manual (by design, per your decisions):**
- Deposits/withdrawals: `/deposit` and `/withdraw` just create a request — you (admin) run `/credit` or `/payout` after verifying payment yourself, exactly as planned.
- The Payments & Support group is just a regular Telegram group you create — the bot doesn't manage it, it just points players to it.

## Known limitations at this MVP stage

- **SQLite, single process.** Fine for one admin running one bot instance. If you ever run multiple bot processes against the same database, you'd want to move to PostgreSQL and add the per-group locking discussed earlier (the data model was designed with this in mind).
- **No message-edit deduplication.** If Telegram delivers a duplicate update (rare, but possible), it's not explicitly de-duplicated. Low risk at small scale.
- **Two-deck threshold.** The rulebook says "two decks for a large player count" without a number. Based on the `players × 3` minimum-deck rule, this code uses two decks starting at 5 players (a single 13-card deck can only mathematically support up to 4). Worth confirming this matches your intent.

## Testing with real players safely

Recommended first run: create a private Telegram group with 3-4 trusted people, use small stakes, and watch a few full rounds play out before opening it wider. Run `/groups` and `/pending` periodically as admin to sanity-check state.
