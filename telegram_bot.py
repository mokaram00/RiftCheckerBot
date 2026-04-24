import os
import json
import time
import aiohttp
import asyncio
import random
import threading
import requests
import telebot
import telebot.apihelper as tele_apihelper
from io import BytesIO
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from PIL import Image, ImageDraw, ImageFont
from epic_auth import EpicUser, EpicGenerator, EpicEndpoints
from user import RiftUser
from cosmetic import FortniteCosmetic
from commands import (
    command_start,
    command_help,
    command_set_game_path,
    command_login,
    command_bp,
    command_theme,
    command_badges,
    command_stats,
    send_theme_message,
    send_badges_message,
    handle_menu_callback,
    handle_bp_callback,
    available_themes,
    avaliable_badges,
)

import epic_auth
import cosmetic
import commands
import utils

# ``getUpdates`` long-poll can block up to ~50s; default ``requests`` read timeout (25) is too low → ReadTimeout.
tele_apihelper.READ_TIMEOUT = 90
tele_apihelper.CONNECT_TIMEOUT = 30

# your telegram bot's api token
TELEGRAM_API_TOKEN = "8624494193:AAF_BCp-yjfVo2V1pghWxYEZjCMbzCAoqAM"

# locker categories we render in the checker
telegram_bot = telebot.TeleBot(TELEGRAM_API_TOKEN)

# Command names: no leading slash; a–z, 0–9, _ only (Telegram Bot API).
telegram_bot.set_my_commands(
    [
        telebot.types.BotCommand("login", "Skincheck your Epic Games account"),
        telebot.types.BotCommand("bp", "Bypass login (exchange link)"),
        telebot.types.BotCommand("start", "Main menu & saved accounts"),
        telebot.types.BotCommand("help", "Command list"),
        telebot.types.BotCommand("setgamepath", "Fortnite install folder for .bat"),
    ]
)

auth_code = None
@telegram_bot.message_handler(commands=['start'])
def handle_start(message):
    command_start(telegram_bot, message)

@telegram_bot.message_handler(commands=['help'])
def handle_help(message):
    command_help(telegram_bot, message)

@telegram_bot.message_handler(commands=['setgamepath', 'setfortnitepath'])
def handle_set_game_path(message):
    command_set_game_path(telegram_bot, message)

@telegram_bot.message_handler(commands=['login'])
def handle_login(message):
    asyncio.run(command_login(telegram_bot, message))

@telegram_bot.message_handler(commands=["bp"])
def handle_bp(message):
    command_bp(telegram_bot, message)

@telegram_bot.message_handler(commands=["theme", "style"])
def handle_theme(message):
    asyncio.run(command_theme(telegram_bot, message))

@telegram_bot.message_handler(commands=['badges'])
def handle_badges(message):
    asyncio.run(command_badges(telegram_bot, message))
    
@telegram_bot.message_handler(commands=['stats'])
def handle_stats(message):
    asyncio.run(command_stats(telegram_bot, message))

@telegram_bot.callback_query_handler(func=lambda c: c.data == "deldoc")
def handle_delete_play_message(call):
    try:
        telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        telegram_bot.answer_callback_query(call.id)
    except Exception:
        pass


@telegram_bot.callback_query_handler(
    func=lambda c: c.data in ("sav", "home", "hlp", "gph")
    or (c.data and len(c.data) == 33 and c.data.startswith("M"))
    or (c.data and len(c.data) == 34 and c.data.startswith("X"))
    or (c.data and len(c.data) == 34 and c.data.startswith("C"))
    or (c.data and len(c.data) == 34 and c.data.startswith("F"))
)
def handle_saved_accounts_menu(call):
    handle_menu_callback(telegram_bot, call)

@telegram_bot.callback_query_handler(
    func=lambda c: bool(c.data) and (c.data.startswith("bp_"))
)
def handle_bp_menu(call):
    handle_bp_callback(telegram_bot, call)


@telegram_bot.callback_query_handler(func=lambda call: call.data.startswith("tnav_") or call.data.startswith("tsel_"))
def handle_theme_navigation(call):
    data = call.data
    user = RiftUser(call.from_user.id, call.from_user.username)
    user_data = user.load_data()

    if not user_data:
        telegram_bot.reply_to(call.message, "You haven't setup your user yet, please use /start before skinchecking!")
        return

    if data.startswith("tnav_"):
        new_index = int(data.split("_")[1])
        telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
        send_theme_message(telegram_bot, call.message.chat.id, new_index)

    elif data.startswith("tsel_"):
        selected_index = int(data.split("_")[1])
        selected = available_themes[selected_index]
        user_data["theme"] = selected["ID"]
        user.update_data()
        telegram_bot.send_message(call.message.chat.id, f"✅ Theme {selected['name']} selected.")

@telegram_bot.callback_query_handler(func=lambda call: call.data.startswith("badge_") or call.data.startswith("toggle_"))
def handle_badge_navigation(call):
    data = call.data
    user = RiftUser(call.from_user.id, call.from_user.username)
    user_data = user.load_data()

    if not user_data:
        telegram_bot.reply_to(call.message, "You haven't setup your user yet, please use /start before skinchecking!")
        return
    
    if data.startswith("badge_"):
        new_index = int(data.split("_")[1])
        telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
        send_badges_message(telegram_bot, call.message.chat.id, new_index, user_data)

    elif data.startswith("toggle_"):
        badge_index = int(data.split("_")[1])
        badge = avaliable_badges[badge_index]
        current_status = user_data.get(badge['data2'], False)
        user_data[badge['data2']] = not current_status

        user.update_data()
        telegram_bot.answer_callback_query(call.id, f"{badge['name']} is now {'Enabled' if not current_status else 'Disabled'}!")
        telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
        send_badges_message(telegram_bot, call.message.chat.id, badge_index, user_data)

print("bot starting...")
if __name__ == '__main__':
    while True:
        try:
            telegram_bot.infinity_polling(
                timeout=50,
                long_polling_timeout=50,
                skip_pending=True,
            )
        except KeyboardInterrupt:
            raise
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            print(f"Telegram API network timeout, retrying in 3s: {exc!r}")
            time.sleep(3)