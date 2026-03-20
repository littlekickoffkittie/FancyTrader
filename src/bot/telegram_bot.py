import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, CallbackContext
)
from src.core.config_manager import load_config, save_config
from src.core.sim_engine import start_sim, stop_sim, is_running as sim_running
from src.core.live_engine import start_live, stop_live, is_running as live_running

load_dotenv()
logging.basicConfig(level=logging.INFO)

MINI_APP_URL = os.environ.get("MINI_APP_URL", "https://your-mini-app-url.com")


def _user_id(update: Update) -> str:
    """Derive a stable user ID from Telegram user ID."""
    return f"tg-{update.effective_user.id}"


def start(update: Update, context: CallbackContext) -> None:
    keyboard = [
        [InlineKeyboardButton("Open Dashboard", web_app={"url": MINI_APP_URL})],
        [
            InlineKeyboardButton("Start Sim", callback_data="sim_start"),
            InlineKeyboardButton("Stop Sim", callback_data="sim_stop")
        ],
        [
            InlineKeyboardButton("Dry Run", callback_data="live_dry"),
            InlineKeyboardButton("Status", callback_data="status")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        """FANCYBOT\nNon-custodial automated trading.\nYour keys. Your funds. Our edge.""",
        reply_markup=reply_markup
    )


def status(update: Update, context: CallbackContext) -> None:
    uid = _user_id(update)
    cfg = load_config(uid)
    sim = sim_running(uid)
    live = live_running(uid)
    msg = (
        f"""SIM:  {'RUNNING' if sim else 'STOPPED'}\n"""
        f"""LIVE: {'RUNNING' if live else 'STOPPED'}\n"""
        f"""Mode: {cfg.mode}\n"""
        f"""Score gate: {cfg.signal.score_gate}\n"""
        f"""Max positions: {cfg.risk.max_positions}\n"""
        f"""Leverage: {cfg.risk.leverage}x\n"""
        f"""Trade size: ${cfg.risk.trade_size_usdt}"""
    )
    update.message.reply_text(msg)


def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    uid = _user_id(update)

    if query.data == "sim_start":
        if sim_running(uid):
            query.edit_message_text("Sim already running.")
        else:
            start_sim(uid)
            query.edit_message_text("Sim started.")

    elif query.data == "sim_stop":
        if not sim_running(uid):
            query.edit_message_text("Sim not running.")
        else:
            stop_sim(uid)
            query.edit_message_text("Sim stopped.")

    elif query.data == "live_dry":
        if live_running(uid):
            query.edit_message_text("Live engine already running.")
        else:
            start_live(uid, dry_run=True)
            query.edit_message_text("Dry-run started. Watching for signals.")

    elif query.data == "status":
        cfg = load_config(uid)
        sim = sim_running(uid)
        live = live_running(uid)
        query.edit_message_text(
            f"""SIM: {'ON' if sim else 'OFF'} | LIVE: {'ON' if live else 'OFF'}\n"""
            f"""Gate: {cfg.signal.score_gate} | Lev: {cfg.risk.leverage}x | """
            f"""Size: ${cfg.risk.trade_size_usdt}"""
        )


def run_bot() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    updater = Updater(token)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()
    print("Bot running...")
    updater.idle()


if __name__ == "__main__":
    run_bot()
