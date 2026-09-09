"""
BANKERU - Telegram bot entry point.

Wires every command in village-card-game-bot-commands.md to the
logic layers (db.py, game_logic.py, group_logic.py, round_logic.py),
which are independently unit-tested in test_*.py.

Run with:  python3 bot.py
Requires:  VILLAGE_CARD_BOT_TOKEN env var set to a token from @BotFather.
"""

import asyncio
import logging
import os
import time
import uuid
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

import config
from db import init_db, db_transaction
import game_logic as gl
import group_logic as grp
import round_logic as rl

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("villagecardbot")

# Minimal Flask web server to satisfy Render's port binding requirement
web_app = Flask(__name__)

@web_app.route("/")
def health_check():
    return "BANKERU Bot is running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Player helpers
# ---------------------------------------------------------------------------

def get_or_create_player(cur, telegram_user) -> str:
    cur.execute(
        "SELECT id FROM player WHERE telegram_id = ?", (telegram_user.id,)
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    pid = _new_id()
    cur.execute(
        "INSERT INTO player (id, telegram_id, display_name, balance, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (pid, telegram_user.id, telegram_user.full_name, time.time()),
    )
    return pid


def is_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


def get_player_group(cur, player_id: str):
    """Finds the player's current active group membership, if any."""
    cur.execute(
        "SELECT gm.group_id, g.status, g.telegram_chat_id "
        "FROM group_member gm JOIN game_group g ON g.id = gm.group_id "
        "WHERE gm.player_id = ? AND gm.left_at IS NULL LIMIT 1",
        (player_id,),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Deposit Helper Function
# ---------------------------------------------------------------------------

async def process_deposit_request(user, reply_fn):
    """Core logic to record a deposit request and present copyable ID & instructions."""
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, user)
        req_id = _new_id()
        cur.execute(
            "INSERT INTO deposit_request (id, player_id, amount_claimed, "
            "status, created_at) VALUES (?, ?, 0, 'pending', ?)",
            (req_id, pid, time.time()),
        )
    
    msg = (
        f"{config.ADMIN_PAYMENT_INSTRUCTIONS}\n\n"
        f"📌 **Your Telegram ID:** `{user.id}`\n"
        f"*(Tap the ID above to copy it)*\n\n"
        "After sending payment, please post your receipt/screenshot alongside your "
        "Telegram ID to the admin so they can verify and credit your balance.\n\n"
        f"Your deposit request reference: {req_id[:8]}"
    )
    await reply_fn(msg, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Withdrawal Helper Function
# ---------------------------------------------------------------------------

async def process_withdrawal_amount(user, amount: int, reply_fn) -> bool:
    """Core withdrawal logic used by both command and interactive message inputs."""
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, user)
        cur.execute("SELECT balance FROM player WHERE id = ?", (pid,))
        balance = cur.fetchone()["balance"]
        if amount <= 0:
            await reply_fn("Amount must be positive. Please enter a valid number:")
            return False
        if amount > balance:
            await reply_fn(
                f"You can't withdraw more than your balance ({balance} points). Please enter a valid amount:"
            )
            return False
        cur.execute(
            "UPDATE player SET balance = balance - ? WHERE id = ?", (amount, pid)
        )
        cur.execute(
            "INSERT INTO \"transaction\" (id, player_id, type, amount, created_at) "
            "VALUES (?, ?, 'withdrawal', ?, ?)",
            (_new_id(), pid, -amount, time.time()),
        )
    await reply_fn(
        f"✅ Withdrawal of **{amount} points** requested!\n"
        "The admin will process your payout manually and contact you.",
        parse_mode=ParseMode.MARKDOWN
    )
    return True


# ---------------------------------------------------------------------------
# 1. Main Menu & Account Commands
# ---------------------------------------------------------------------------

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Play Game", callback_data="menu:playgame"),
            InlineKeyboardButton("📊 Table Status", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton("💳 Deposit", callback_data="menu:deposit"),
            InlineKeyboardButton("💸 Withdraw", callback_data="menu:withdraw"),
        ],
        [
            InlineKeyboardButton("💰 Check Balance", callback_data="menu:balance"),
            InlineKeyboardButton("🆔 My Telegram ID", callback_data="menu:myid"),
        ]
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        get_or_create_player(cur, update.effective_user)

    await update.message.reply_text(
        "Welcome to BANKERU! 🃏\n\n"
        "Select an option below to manage your account or play:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        cur.execute("SELECT balance FROM player WHERE id = ?", (pid,))
        balance = cur.fetchone()["balance"]
    await update.message.reply_text(f"💰 Your balance: **{balance} points**", parse_mode=ParseMode.MARKDOWN)

async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_deposit_request(update.effective_user, update.message.reply_text)


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        context.user_data["awaiting_withdraw_amount"] = True
        await update.message.reply_text("💸 Please enter the amount you would like to withdraw:")
        return
    
    amount = int(context.args[0])
    await process_withdrawal_amount(update.effective_user, amount, update.message.reply_text)


# ---------------------------------------------------------------------------
# Interactive Menu Button Handler
# ---------------------------------------------------------------------------

async def on_menu_button_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    user = query.from_user

    if choice == "playgame":
        await send_stake_tier_menu(user, query.message.reply_text)
    elif choice == "status":
        await send_table_status(user, query.message.reply_text)
    elif choice == "deposit":
        await process_deposit_request(user, query.message.reply_text)
    elif choice == "withdraw":
        context.user_data["awaiting_withdraw_amount"] = True
        await query.message.reply_text("💸 Please enter the amount you would like to withdraw:")
    elif choice == "balance":
        with db_transaction() as (conn, cur):
            pid = get_or_create_player(cur, user)
            cur.execute("SELECT balance FROM player WHERE id = ?", (pid,))
            balance = cur.fetchone()["balance"]
        await query.message.reply_text(f"💰 Your balance: **{balance} points**", parse_mode=ParseMode.MARKDOWN)
    elif choice == "myid":
        await query.message.reply_text(
            f"📌 **Your Telegram ID:** `{user.id}`\n*(Tap to copy)*",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_text_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles interactive text prompts for custom stakes and withdrawals."""
    user = update.effective_user
    text = update.message.text.strip()

    if context.user_data.get("awaiting_withdraw_amount"):
        if not text.isdigit():
            await update.message.reply_text("Please enter a valid numeric amount to withdraw:")
            return
        amount = int(text)
        success = await process_withdrawal_amount(user, amount, update.message.reply_text)
        if success:
            context.user_data.pop("awaiting_withdraw_amount", None)

    elif context.user_data.get("awaiting_custom_tier"):
        if not text.isdigit():
            await update.message.reply_text("Please enter a valid numeric stake amount:")
            return
        amount = int(text)
        context.user_data.pop("awaiting_custom_tier", None)
        await _do_join_tier(user, amount, is_custom=True,
                             chat_id=update.effective_chat.id, context=context,
                             reply_fn=update.message.reply_text)


# ---------------------------------------------------------------------------
# 2. Lobby & Group commands
# ---------------------------------------------------------------------------
async def send_stake_tier_menu(user, reply_fn):
    keyboard = [
        [
            InlineKeyboardButton("5 pts", callback_data="tier:5"),
            InlineKeyboardButton("10 pts", callback_data="tier:10"),
            InlineKeyboardButton("20 pts", callback_data="tier:20"),
        ],
        [
            InlineKeyboardButton("50 pts", callback_data="tier:50"),
            InlineKeyboardButton("100 pts", callback_data="tier:100"),
        ],
        [
            InlineKeyboardButton("Custom amount...", callback_data="tier:custom"),
        ],
    ]
    await reply_fn("Choose a stake tier to join:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_playgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stake_tier_menu(update.effective_user, update.message.reply_text)


async def on_tier_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "custom":
        context.user_data["awaiting_custom_tier"] = True
        await query.edit_message_text(
            "Type and send the custom stake amount you'd like to propose:\n"
            "(Waits for another player to match your custom stake)"
        )
        return

    amount = int(choice)
    await _do_join_tier(update.effective_user, amount, is_custom=False,
                         chat_id=update.effective_chat.id, context=context,
                         reply_fn=query.edit_message_text)


async def cmd_jointier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /jointier <amount>")
        return
    amount = int(context.args[0])
    is_custom = amount not in (5, 10, 20, 50, 100)
    await _do_join_tier(update.effective_user, amount, is_custom,
                         chat_id=update.effective_chat.id, context=context,
                         reply_fn=update.message.reply_text)
                         
async def _do_join_tier(telegram_user, amount, is_custom, chat_id, context, reply_fn):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, telegram_user)
        cur.execute("SELECT balance FROM player WHERE id = ?", (pid,))
        balance = cur.fetchone()["balance"]
        if balance < amount:
            await reply_fn(
                f"You need at least {amount} points to join this tier "
                f"(your balance: {balance}). Use /deposit to add points."
            )
            return

        existing = get_player_group(cur, pid)
        if existing:
            await reply_fn(
                "You're already in a group. Use /leavegroup first if you "
                "want to switch (only allowed between rounds)."
            )
            return

        tier_id = grp.get_or_create_stake_tier(cur, amount, is_custom)
        group_id = grp.find_open_group(cur, tier_id, chat_id)
        if group_id is None:
            group_id = grp.create_group(cur, tier_id, chat_id)
        grp.join_group(cur, group_id, pid)
        member_count = grp._count_active(cur, group_id)

    await reply_fn(
        f"Joined the {amount}-point table. Players seated: {member_count}/"
        f"{gl.MAX_GROUP_SIZE} (minimum {gl.MIN_GROUP_SIZE} to start)."
    )

    with db_transaction() as (conn, cur):
        ready = grp.group_is_ready_to_deal(cur, group_id)
        already_scheduled = context.bot_data.get(f"deal_scheduled:{group_id}", False)
    if ready and not already_scheduled:
        context.bot_data[f"deal_scheduled:{group_id}"] = True
        context.job_queue.run_once(
            _deal_job, when=gl.AUTO_START_WINDOW_SECONDS,
            data={"group_id": group_id, "chat_id": chat_id},
            name=f"deal:{group_id}",
        )
        await context.bot.send_message(
            chat_id,
            f"Minimum players reached — round starts automatically within "
            f"{gl.AUTO_START_WINDOW_SECONDS // 60} minutes, or sooner if the table fills up."
        )
    if member_count >= gl.MAX_GROUP_SIZE:
        await _deal_round(group_id, chat_id, context)


async def cmd_leavegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership:
            await update.message.reply_text("You're not currently in a group.")
            return
        group_id = membership["group_id"]
        if not grp.can_player_leave(cur, group_id):
            await update.message.reply_text(
                "You can't leave mid-round — please wait until the current "
                "round ends."
            )
            return
        grp.leave_group(cur, group_id, pid)
    await update.message.reply_text("You've left the table. Use /playgame to join another.")


async def send_table_status(user, reply_fn):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, user)
        membership = get_player_group(cur, pid)
        if not membership:
            await reply_fn("You're not in a group. Use /playgame to join one.")
            return
        group_id = membership["group_id"]
        cur.execute(
            "SELECT g.pot_total, g.status, g.current_round_id, st.amount "
            "FROM game_group g JOIN stake_tier st ON st.id = g.stake_tier_id "
            "WHERE g.id = ?",
            (group_id,),
        )
        g = cur.fetchone()
        members = grp.get_active_members_in_seat_order(cur, group_id)
        turn_name = None
        if g["current_round_id"]:
            turn = rl.get_current_turn_player(cur, g["current_round_id"])
            if turn and turn["turn_player_id"]:
                cur.execute(
                    "SELECT display_name FROM player WHERE id = ?",
                    (turn["turn_player_id"],),
                )
                row = cur.fetchone()
                turn_name = row["display_name"] if row else None

    text = (
        f" Stake: {g['amount']} points\n"
        f"👥 Players: {len(members)}/{gl.MAX_GROUP_SIZE}\n"
        f"💰 Pot: {g['pot_total']} points\n"
        f"📌 Status: {g['status']}"
    )
    if turn_name:
        text += f"\n🎯 Current turn: {turn_name}"
    await reply_fn(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_table_status(update.effective_user, update.message.reply_text)


# ---------------------------------------------------------------------------
# 3. In-round gameplay commands & Interactive Controls
# ---------------------------------------------------------------------------

def get_in_game_hud_keyboard():
    """Inline keyboard panel attached directly to game updates for 1-tap actions."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎴 Check Hand", callback_data="game:hand"),
            InlineKeyboardButton("💰 Check Pot", callback_data="game:pot"),
        ],
        [
            InlineKeyboardButton("🎯 Whose Turn?", callback_data="game:turn"),
        ]
    ])


async def on_game_action_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    choice = query.data.split(":", 1)[1]
    user = query.from_user

    if choice == "hand":
        with db_transaction() as (conn, cur):
            pid = get_or_create_player(cur, user)
            membership = get_player_group(cur, pid)
            if not membership or membership["status"] != "active":
                await query.answer("You're not currently in an active round.", show_alert=True)
                return
            cur.execute(
                "SELECT current_round_id FROM game_group WHERE id = ?",
                (membership["group_id"],),
            )
            round_id = cur.fetchone()["current_round_id"]
            hand = rl.get_player_hand(cur, round_id, pid)
            if not hand:
                await query.answer("You weren't dealt into this round.", show_alert=True)
                return

        pair_note = " — PAIR! Automatic win." if hand["is_pair"] else ""
        await query.answer(f"Cards: {hand['card1']} and {hand['card2']}{pair_note}", show_alert=True)

    elif choice == "pot":
        with db_transaction() as (conn, cur):
            pid = get_or_create_player(cur, user)
            membership = get_player_group(cur, pid)
            if not membership:
                await query.answer("You're not in a group.", show_alert=True)
                return
            cur.execute(
                "SELECT pot_total FROM game_group WHERE id = ?", (membership["group_id"],)
            )
            pot = cur.fetchone()["pot_total"]
        await query.answer(f"Current pot: {pot} points", show_alert=True)

    elif choice == "turn":
        with db_transaction() as (conn, cur):
            pid = get_or_create_player(cur, user)
            membership = get_player_group(cur, pid)
            if not membership or membership["status"] != "active":
                await query.answer("No active round in your group.", show_alert=True)
                return
            cur.execute(
                "SELECT current_round_id FROM game_group WHERE id = ?",
                (membership["group_id"],),
            )
            round_id = cur.fetchone()["current_round_id"]
            turn = rl.get_current_turn_player(cur, round_id)
            cur.execute(
                "SELECT display_name FROM player WHERE id = ?", (turn["turn_player_id"],)
            )
            name = cur.fetchone()["display_name"]
            seconds_left = max(0, int(turn["turn_deadline"] - time.time()))
        await query.answer(f"{name}'s turn ({seconds_left}s left)", show_alert=True)


async def cmd_hand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please DM me /hand to keep your cards private.")
        return
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership or not membership["status"] == "active":
            await update.message.reply_text("You're not currently in an active round.")
            return
        cur.execute(
            "SELECT current_round_id FROM game_group WHERE id = ?",
            (membership["group_id"],),
        )
        round_id = cur.fetchone()["current_round_id"]
        hand = rl.get_player_hand(cur, round_id, pid)
        if not hand:
            await update.message.reply_text("You weren't dealt into this round.")
            return
    pair_note = " — that's a PAIR! Automatic win when your turn comes. 🎉" if hand["is_pair"] else ""
    await update.message.reply_text(
        f"Your cards: {hand['card1']} and {hand['card2']}{pair_note}"
    )


async def cmd_pot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership:
            await update.message.reply_text("You're not in a group.")
            return
        cur.execute(
            "SELECT pot_total FROM game_group WHERE id = ?", (membership["group_id"],)
        )
        pot = cur.fetchone()["pot_total"]
    await update.message.reply_text(f"Current pot: {pot} points")


async def cmd_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership or membership["status"] != "active":
            await update.message.reply_text("No round is currently active in your group.")
            return
        cur.execute(
            "SELECT current_round_id FROM game_group WHERE id = ?",
            (membership["group_id"],),
        )
        round_id = cur.fetchone()["current_round_id"]
        turn = rl.get_current_turn_player(cur, round_id)
        cur.execute(
            "SELECT display_name FROM player WHERE id = ?", (turn["turn_player_id"],)
        )
        name = cur.fetchone()["display_name"]
        seconds_left = max(0, int(turn["turn_deadline"] - time.time()))
    await update.message.reply_text(f"{name}'s turn — {seconds_left}s left to act")


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /play <bet amount>")
        return
    bet_amount = int(context.args[0])

    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership or membership["status"] != "active":
            await update.message.reply_text("No active round in your group.")
            return
        group_id = membership["group_id"]
        cur.execute(
            "SELECT current_round_id FROM game_group WHERE id = ?", (group_id,)
        )
        round_id = cur.fetchone()["current_round_id"]
        turn = rl.get_current_turn_player(cur, round_id)
        if turn["turn_player_id"] != pid:
            await update.message.reply_text("It's not your turn.")
            return

        hand = rl.get_player_hand(cur, round_id, pid)
        if hand["is_pair"]:
            result = rl.handle_pair_auto_win(cur, round_id, pid)
            chat_id = membership["telegram_chat_id"]
        else:
            valid, reason = gl.validate_bet(cur, group_id, pid, bet_amount)
            if not valid:
                await update.message.reply_text(reason)
                return
            context.user_data["pending_bet"] = {
                "round_id": round_id, "bet_amount": bet_amount,
                "group_id": group_id, "chat_id": membership["telegram_chat_id"],
            }
            keyboard = [[
                InlineKeyboardButton("Drop the top card", callback_data="reveal:drop_top"),
                InlineKeyboardButton("Reshuffle first", callback_data="reveal:reshuffle"),
            ]]
            await update.message.reply_text(
                f"Betting {bet_amount} points. How should the card be revealed?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

    await context.bot.send_message(
        chat_id,
        f"🎉 {update.effective_user.full_name} had a PAIR ({hand['card1']}-"
        f"{hand['card2']}) and auto-wins {result['win_amount']} points! "
        f"(Payout: {result['player_payout']}, commission: {result['commission']})\n"
        f"Pot is now {result['new_pot_total']}."
    )
    if result["is_full_win"]:
        await _maybe_start_next_round(group_id, chat_id, context)


async def on_reveal_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shuffle_first = query.data.split(":", 1)[1] == "reshuffle"

    pending = context.user_data.get("pending_bet")
    if not pending:
        await query.edit_message_text("This bet has expired.")
        return

    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        try:
            result = rl.handle_play(
                cur, pending["round_id"], pid, pending["bet_amount"], shuffle_first
            )
        except ValueError as e:
            await query.edit_message_text(str(e))
            return

    context.user_data.pop("pending_bet", None)
    method = "reshuffled the deck then flipped" if shuffle_first else "flipped the top card"
    outcome_text = "WON 🎉" if result["outcome"] == "win" else "lost"
    await query.edit_message_text(
        f"{update.effective_user.full_name} bet {pending['bet_amount']} "
        f"({method}: {result['revealed_card']}) — hand was "
        f"{result['card1']}-{result['card2']} — {outcome_text}!"
    )
    if result["outcome"] == "win":
        await context.bot.send_message(
            pending["chat_id"],
            f"Payout: {result['player_payout']}, commission: {result['commission']}. "
            f"Pot is now {result['new_pot_total'] if result['new_pot_total'] is not None else '?'}."
        )

    if result.get("round_ended"):
        await _maybe_start_next_round(pending["group_id"], pending["chat_id"], context)
    else:
        await _announce_next_turn(pending["round_id"], pending["chat_id"], context)


async def cmd_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        pid = get_or_create_player(cur, update.effective_user)
        membership = get_player_group(cur, pid)
        if not membership or membership["status"] != "active":
            await update.message.reply_text("No active round in your group.")
            return
        group_id = membership["group_id"]
        cur.execute(
            "SELECT current_round_id FROM game_group WHERE id = ?", (group_id,)
        )
        round_id = cur.fetchone()["current_round_id"]
        turn = rl.get_current_turn_player(cur, round_id)
        if turn["turn_player_id"] != pid:
            await update.message.reply_text("It's not your turn.")
            return
        result = rl.handle_drop(cur, round_id, pid)
        chat_id = membership["telegram_chat_id"]

    await context.bot.send_message(
        chat_id,
        f"{update.effective_user.full_name} dropped "
        f"(hand was {result['card1']}-{result['card2']})."
    )
    if result["all_dropped"]:
        await context.bot.send_message(
            chat_id, "Everyone dropped — dealing a fresh round..."
        )
        await _deal_round(group_id, chat_id, context)
    elif result["round_ended"]:
        await _maybe_start_next_round(group_id, chat_id, context)
    else:
        await _announce_next_turn(round_id, chat_id, context)


# ---------------------------------------------------------------------------
# Round dealing, turn announcements, and timers
# ---------------------------------------------------------------------------

async def _deal_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    context.bot_data.pop(f"deal_scheduled:{data['group_id']}", None)
    with db_transaction() as (conn, cur):
        ready = grp.group_is_ready_to_deal(cur, data["group_id"])
    if ready:
        await _deal_round(data["group_id"], data["chat_id"], context)


async def _deal_round(group_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        cur.execute(
            "SELECT current_round_id FROM game_group WHERE id = ?", (group_id,)
        )
        prev_round = cur.fetchone()
        prev_round_id = None
        cur.execute(
            "SELECT id FROM round WHERE group_id = ? AND ended_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (group_id,),
        )
        row = cur.fetchone()
        if row:
            prev_round_id = row["id"]

        round_id, disconnected, err = rl.start_new_round(cur, group_id, prev_round_id)

        disconnected_names = []
        for pid in disconnected:
            cur.execute("SELECT display_name FROM player WHERE id = ?", (pid,))
            r = cur.fetchone()
            if r:
                disconnected_names.append(r["display_name"])

    if err == "not_enough_players" or err == "not_enough_eligible":
        await context.bot.send_message(
            chat_id, "Not enough eligible players to start a round yet. Waiting..."
        )
        return

    if disconnected_names:
        await context.bot.send_message(
            chat_id,
            "Skipped this round (insufficient balance / not ready): "
            + ", ".join(disconnected_names)
        )

    await context.bot.send_message(chat_id, "🎴 New round dealt! Cards sent privately.")

    with db_transaction() as (conn, cur):
        cur.execute("SELECT player_id FROM hand WHERE round_id = ?", (round_id,))
        dealt_players = [r["player_id"] for r in cur.fetchall()]
        for pid in dealt_players:
            hand = rl.get_player_hand(cur, round_id, pid)
            cur.execute("SELECT telegram_id FROM player WHERE id = ?", (pid,))
            tg_id = cur.fetchone()["telegram_id"]
            pair_note = " — PAIR! Automatic win when it's your turn. 🎉" if hand["is_pair"] else ""
            try:
                await context.bot.send_message(
                    tg_id,
                    f"Your cards: {hand['card1']} and {hand['card2']}{pair_note}",
                    reply_markup=get_in_game_hud_keyboard()
                )
            except Exception as e:
                logger.warning("Could not DM player %s: %s", tg_id, e)

    await _announce_next_turn(round_id, chat_id, context)


async def _announce_next_turn(round_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        turn = rl.get_current_turn_player(cur, round_id)
        cur.execute(
            "SELECT display_name FROM player WHERE id = ?", (turn["turn_player_id"],)
        )
        name = cur.fetchone()["display_name"]
        cur.execute("SELECT pot_total FROM game_group WHERE id = ?", (turn["group_id"],))
        pot = cur.fetchone()["pot_total"]

    await context.bot.send_message(
        chat_id,
        f"💰 Pot: {pot} points.\n🎯 **{name}**'s turn — use /play <bet> or /drop\n"
        f"⏱️ Time remaining: {gl.TURN_TIMEOUT_SECONDS}s",
        reply_markup=get_in_game_hud_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    context.job_queue.run_once(
        _turn_timeout_job, when=gl.TURN_TIMEOUT_SECONDS,
        data={"round_id": round_id, "chat_id": chat_id,
              "expected_player_id": turn["turn_player_id"]},
        name=f"turn_timeout:{round_id}:{turn['turn_player_id']}",
    )


async def _turn_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    with db_transaction() as (conn, cur):
        turn = rl.get_current_turn_player(cur, data["round_id"])
        cur.execute("SELECT ended_at FROM round WHERE id = ?", (data["round_id"],))
        round_row = cur.fetchone()
        if round_row["ended_at"] is not None:
            return
        if turn["turn_player_id"] != data["expected_player_id"]:
            return
        result = rl.handle_drop(cur, data["round_id"], data["expected_player_id"], timed_out=True)
        cur.execute(
            "SELECT display_name FROM player WHERE id = ?",
            (data["expected_player_id"],),
        )
        name = cur.fetchone()["display_name"]
        group_id = turn["group_id"]

    await context.bot.send_message(
        data["chat_id"], f"⏱️ {name} ran out of time — auto-dropped."
    )
    if result["all_dropped"]:
        await context.bot.send_message(data["chat_id"], "Everyone dropped — dealing a fresh round...")
        await _deal_round(group_id, data["chat_id"], context)
    elif result["round_ended"]:
        await _maybe_start_next_round(group_id, data["chat_id"], context)
    else:
        await _announce_next_turn(data["round_id"], data["chat_id"], context)


async def _maybe_start_next_round(group_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    with db_transaction() as (conn, cur):
        ready = grp.group_is_ready_to_deal(cur, group_id)
    if ready:
        await _deal_round(group_id, chat_id, context)
    else:
        await context.bot.send_message(
            chat_id, "Round ended. Waiting for enough players for the next round."
        )


# ---------------------------------------------------------------------------
# 5. Admin-only commands
# ---------------------------------------------------------------------------

async def cmd_checkuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /checkuser <telegram_id>")
        return

    target_tg_id = int(context.args[0])

    with db_transaction() as (conn, cur):
        cur.execute(
            "SELECT id, display_name, balance FROM player WHERE telegram_id = ?",
            (target_tg_id,)
        )
        player = cur.fetchone()

        if not player:
            await update.message.reply_text(f"❌ No player found with Telegram ID: `{target_tg_id}`", parse_mode=ParseMode.MARKDOWN)
            return

        pid = player["id"]

        membership = get_player_group(cur, pid)
        group_info = "Not currently seated at any table"
        if membership:
            cur.execute(
                "SELECT g.id, g.status, g.pot_total, st.amount "
                "FROM game_group g JOIN stake_tier st ON st.id = g.stake_tier_id "
                "WHERE g.id = ?",
                (membership["group_id"],)
            )
            g = cur.fetchone()
            if g:
                group_info = f"Table ID: `{g['id'][:8]}` | Stake: {g['amount']} pts | Pot: {g['pot_total']} pts | Status: {g['status']}"

        cur.execute(
            "SELECT COUNT(*) as count FROM deposit_request WHERE player_id = ? AND status = 'pending'",
            (pid,)
        )
        pending_deposits = cur.fetchone()["count"]

    msg = (
        f"👤 **Player Details**\n"
        f"• **Name:** {player['display_name']}\n"
        f"• **Telegram ID:** `{target_tg_id}`\n"
        f"• **Coins/Balance:** {player['balance']} points\n"
        f"• **Pending Deposits:** {pending_deposits}\n"
        f"• **Table Status:** {group_info}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /credit <telegram_id> <amount>")
        return
    target_tg_id, amount = int(context.args[0]), int(context.args[1])
    with db_transaction() as (conn, cur):
        cur.execute("SELECT id FROM player WHERE telegram_id = ?", (target_tg_id,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("Unknown player.")
            return
        pid = row["id"]
        admin_pid = get_or_create_player(cur, update.effective_user)
        cur.execute("UPDATE player SET balance = balance + ? WHERE id = ?", (amount, pid))
        cur.execute(
            "INSERT INTO \"transaction\" (id, player_id, type, amount, admin_id, created_at) "
            "VALUES (?, ?, 'deposit', ?, ?, ?)",
            (_new_id(), pid, amount, admin_pid, time.time()),
        )
        cur.execute(
            "UPDATE deposit_request SET status = 'resolved', resolved_at = ? "
            "WHERE player_id = ? AND status = 'pending'",
            (time.time(), pid),
        )
    await update.message.reply_text(f"Credited {amount} points to {target_tg_id}.")


async def cmd_payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /payout <telegram_id> <amount>")
        return
    target_tg_id, amount = int(context.args[0]), int(context.args[1])
    with db_transaction() as (conn, cur):
        cur.execute("SELECT id FROM player WHERE telegram_id = ?", (target_tg_id,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("Unknown player.")
            return
        pid = row["id"]
        admin_pid = get_or_create_player(cur, update.effective_user)
        cur.execute(
            "INSERT INTO \"transaction\" (id, player_id, type, amount, admin_id, note, created_at) "
            "VALUES (?, ?, 'withdrawal', ?, ?, 'manual payout confirmed', ?)",
            (_new_id(), pid, -amount, admin_pid, time.time()),
        )
    await update.message.reply_text(f"Marked payout of {amount} to {target_tg_id} as paid.")


async def cmd_commission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    with db_transaction() as (conn, cur):
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM \"transaction\" WHERE type = 'commission'"
        )
        total = cur.fetchone()["total"]
    await update.message.reply_text(f"Total commission earned: {total} points")


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    with db_transaction() as (conn, cur):
        cur.execute(
            "SELECT g.id, g.status, g.pot_total, st.amount FROM game_group g "
            "JOIN stake_tier st ON st.id = g.stake_tier_id WHERE g.status != 'closed'"
        )
        rows = cur.fetchall()
        lines = []
        for r in rows:
            count = grp._count_active(cur, r["id"])
            lines.append(
                f"{r['id'][:8]} | stake {r['amount']} | {count} players | "
                f"pot {r['pot_total']} | {r['status']}"
            )
    await update.message.reply_text("\n".join(lines) or "No active groups.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    with db_transaction() as (conn, cur):
        cur.execute(
            "SELECT dr.id, p.telegram_id, p.display_name, dr.amount_claimed, dr.created_at "
            "FROM deposit_request dr JOIN player p ON p.id = dr.player_id "
            "WHERE dr.status = 'pending' ORDER BY dr.created_at ASC"
        )
        rows = cur.fetchall()
        lines = [
            f"{r['display_name']} ({r['telegram_id']}) — claimed {r['amount_claimed']} "
            f"— ref {r['id'][:8]}"
            for r in rows
        ]
    await update.message.reply_text("\n".join(lines) or "No pending deposit requests.")


async def cmd_forceremove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /forceremove <telegram_id>")
        return
    target_tg_id = int(context.args[0])
    with db_transaction() as (conn, cur):
        cur.execute("SELECT id FROM player WHERE telegram_id = ?", (target_tg_id,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("Unknown player.")
            return
        pid = row["id"]
        membership = get_player_group(cur, pid)
        if not membership:
            await update.message.reply_text("Player isn't in a group.")
            return
        grp.leave_group(cur, membership["group_id"], pid)
    await update.message.reply_text(f"Removed {target_tg_id} from their group.")


# ---------------------------------------------------------------------------
# Wiring it all together
# ---------------------------------------------------------------------------

def build_application() -> Application:
    init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))

    app.add_handler(CommandHandler("playgame", cmd_playgame))
    app.add_handler(CommandHandler("jointier", cmd_jointier))
    app.add_handler(CommandHandler("leavegroup", cmd_leavegroup))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(CommandHandler("hand", cmd_hand))
    app.add_handler(CommandHandler("pot", cmd_pot))
    app.add_handler(CommandHandler("turn", cmd_turn))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("drop", cmd_drop))

    app.add_handler(CommandHandler("checkuser", cmd_checkuser))
    app.add_handler(CommandHandler("credit", cmd_credit))
    app.add_handler(CommandHandler("payout", cmd_payout))
    app.add_handler(CommandHandler("commission", cmd_commission))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("forceremove", cmd_forceremove))

    app.add_handler(CallbackQueryHandler(on_menu_button_selected, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_tier_selected, pattern=r"^tier:"))
    app.add_handler(CallbackQueryHandler(on_reveal_choice, pattern=r"^reveal:"))
    app.add_handler(CallbackQueryHandler(on_game_action_selected, pattern=r"^game:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_inputs))

    return app


def main():
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Set VILLAGE_CARD_BOT_TOKEN before running. See README.md."
        )
    
    # Start the Flask web server in a background thread so it opens the port for Render
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()
    logger.info("Background web server started for Render health checks.")

    app = build_application()
    logger.info("BANKERU bot starting...")
    app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    main()