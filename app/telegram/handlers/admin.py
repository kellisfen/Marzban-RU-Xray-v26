import copy
import io
import math
import os
import random
import re
import string
from datetime import datetime

import qrcode
import sqlalchemy
from dateutil.relativedelta import relativedelta
from telebot import types
from telebot.apihelper import ApiTelegramException
from telebot.util import extract_arguments, user_link

from app import xray
from app.db import GetDB, crud
from app.models.proxy import ProxyTypes
from app.models.user import (
    UserCreate,
    UserModify,
    UserResponse,
    UserStatus,
    UserStatusModify
)
from app.models.user_template import UserTemplateResponse
from app.telegram import bot
from app.telegram.utils.custom_filters import cb_query_equals, cb_query_startswith
from app.telegram.utils.keyboard import BotKeyboard
from app.telegram.utils.shared import (
    get_number_at_end,
    get_template_info_text,
    get_user_info_text,
    status_translations,
    statuses,
    time_to_string
)
from app.utils.store import MemoryStorage
from app.utils.system import cpu_usage, memory_usage, readable_size, realtime_bandwidth
from config import TELEGRAM_DEFAULT_VLESS_FLOW, TELEGRAM_LOGGER_CHANNEL_ID

mem_store = MemoryStorage()


def get_system_info():
    mem = memory_usage()
    cpu = cpu_usage()
    with GetDB() as db:
        bandwidth = crud.get_system_usage(db)
        total_users = crud.get_users_count(db)
        active_users = crud.get_users_count(db, UserStatus.active)
        onhold_users = crud.get_users_count(db, UserStatus.on_hold)
    return """\
🎛 <b>Ядер CPU</b>: <code>{cpu_cores}</code>
🖥 <b>Загрузка CPU</b>: <code>{cpu_percent}%</code>
➖➖➖➖➖➖➖
📊 <b>Всего памяти</b>: <code>{total_memory}</code>
📈 <b>Используется</b>: <code>{used_memory}</code>
📉 <b>Свободно</b>: <code>{free_memory}</code>
➖➖➖➖➖➖➖
⬇️ <b>Трафик (Вход)</b>: <code>{down_bandwidth}</code>
⬆️ <b>Трафик (Исход)</b>: <code>{up_bandwidth}</code>
↕️ <b>Всего трафика</b>: <code>{total_bandwidth}</code>
➖➖➖➖➖➖➖
�� <b>Всего пользователей</b>: <code>{total_users}</code>
🟢 <b>Активных</b>: <code>{active_users}</code>
🟣 *В ожидании*: <code>{onhold_users}</code>
🔴 <b>Отключенных</b>: <code>{deactivate_users}</code>
➖➖➖➖➖➖➖
⏫ <b>Скорость отдачи</b>: <code>{up_speed}/s</code>
⏬ <b>Скорость загрузки</b>: <code>{down_speed}/s</code>
""".format(
        cpu_cores=cpu.cores,
        cpu_percent=cpu.percent,
        total_memory=readable_size(mem.total),
        used_memory=readable_size(mem.used),
        free_memory=readable_size(mem.free),
        total_bandwidth=readable_size(bandwidth.uplink + bandwidth.downlink),
        up_bandwidth=readable_size(bandwidth.uplink),
        down_bandwidth=readable_size(bandwidth.downlink),
        total_users=total_users,
        active_users=active_users,
        onhold_users=onhold_users,
        deactivate_users=total_users - (active_users + onhold_users),
        up_speed=readable_size(realtime_bandwidth().outgoing_bytes),
        down_speed=readable_size(realtime_bandwidth().incoming_bytes)
    )


def schedule_delete_message(chat_id, *message_ids: int) -> None:
    messages: list[int] = mem_store.get(f"{chat_id}:messages_to_delete", [])
    for mid in message_ids:
        messages.append(mid)
    mem_store.set(f"{chat_id}:messages_to_delete", messages)


def cleanup_messages(chat_id: int) -> None:
    messages: list[int] = mem_store.get(f"{chat_id}:messages_to_delete", [])
    for message_id in messages:
        try:
            bot.delete_message(chat_id, message_id)
        except ApiTelegramException:
            pass
    mem_store.set(f"{chat_id}:messages_to_delete", [])


@bot.message_handler(commands=['start', 'help'], is_admin=True)
def help_command(message: types.Message):
    cleanup_messages(message.chat.id)
    bot.clear_step_handler_by_chat_id(message.chat.id)
    return bot.reply_to(message, """
{user_link} Добро пожаловать в админ-панель Marzban.
Здесь вы можете управлять пользователями и прокси.
Для начала используйте кнопки ниже.
Также вы можете просматривать и изменять пользователей командой /user.
""".format(
        user_link=user_link(message.from_user)
    ), parse_mode="html", reply_markup=BotKeyboard.main_menu())


@bot.callback_query_handler(cb_query_equals('system'), is_admin=True)
def system_command(call: types.CallbackQuery):
    return bot.edit_message_text(
        get_system_info(),
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=BotKeyboard.main_menu()
    )


@bot.callback_query_handler(cb_query_equals('restart'), is_admin=True)
def restart_command(call: types.CallbackQuery):
    bot.edit_message_text(
        '⚠️ Вы уверены? Это перезапустит Xray core.',
        call.message.chat.id,
        call.message.message_id,
        reply_markup=BotKeyboard.confirm_action(action='restart')
    )


@bot.callback_query_handler(cb_query_startswith('delete:'), is_admin=True)
def delete_user_command(call: types.CallbackQuery):
    username = call.data.split(':')[1]
    bot.edit_message_text(
        f'⚠️ Вы уверены? Это удалит пользователя `{username}`.',
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(
            action='delete', username=username)
    )


@bot.callback_query_handler(cb_query_startswith("suspend:"), is_admin=True)
def suspend_user_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это приостановит работу пользователя `{username}`.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(
            action="suspend", username=username),
    )


@bot.callback_query_handler(cb_query_startswith("activate:"), is_admin=True)
def activate_user_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это активирует пользователя `{username}`.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(
            action="activate", username=username),
    )


@bot.callback_query_handler(cb_query_startswith("reset_usage:"), is_admin=True)
def reset_usage_user_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это СБРОСИТ статистику пользователя `{username}`.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(
            action="reset_usage", username=username),
    )


@bot.callback_query_handler(cb_query_equals('edit_all'), is_admin=True)
def edit_all_command(call: types.CallbackQuery):
    with GetDB() as db:
        total_users = crud.get_users_count(db)
        active_users = crud.get_users_count(db, UserStatus.active)
        disabled_users = crud.get_users_count(db, UserStatus.disabled)
        expired_users = crud.get_users_count(db, UserStatus.expired)
        limited_users = crud.get_users_count(db, UserStatus.limited)
        onhold_users = crud.get_users_count(db, UserStatus.on_hold)
        text = f"""
�� <b>Всего пользователей</b>: <code>{total_users}</code>
✅ *Активных*: <code>{active_users}</code>
❌ *Отключенных*: `{disabled_users}`
🕰 *Истекших*: `{expired_users}`
🪫 *С лимитом*: `{limited_users}`
🔌 *В ожидании*: <code>{onhold_users}</code>"""
    return bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.edit_all_menu()
    )


@bot.callback_query_handler(cb_query_equals('delete_expired'), is_admin=True)
def delete_expired_command(call: types.CallbackQuery):
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это УДАЛИТ всех истекших пользователей‼️",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(action="delete_expired"))


@bot.callback_query_handler(cb_query_equals('delete_limited'), is_admin=True)
def delete_limited_command(call: types.CallbackQuery):
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это УДАЛИТ всех пользователей с исчерпанным лимитом‼️",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(action="delete_limited"))


@bot.callback_query_handler(cb_query_equals('add_data'), is_admin=True)
def add_data_command(call: types.CallbackQuery):
    msg = bot.edit_message_text(
        f"🔋 Введите количество ГБ для изменения лимита:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=BotKeyboard.inline_cancel_action())
    schedule_delete_message(call.message.chat.id, call.message.id)
    schedule_delete_message(call.message.chat.id, msg.id)
    return bot.register_next_step_handler(call.message, add_data_step)


def add_data_step(message):
    try:
        data_limit = float(message.text)
        if not data_limit:
            raise ValueError
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Лимит данных должен быть числом и не равен нулю.')
        schedule_delete_message(message.chat.id, wait_msg.message_id)
        return bot.register_next_step_handler(wait_msg, add_data_step)
    schedule_delete_message(message.chat.id, message.message_id)
    msg = bot.send_message(
        message.chat.id,
        f"⚠️ Вы уверены? Это изменит лимит данных ВСЕХ пользователей на <b>"
        f"{'+' if data_limit > 0 else '-'}{readable_size(abs(data_limit * 1024*1024*1024))}</b>",
        parse_mode="html",
        reply_markup=BotKeyboard.confirm_action('add_data', data_limit))
    cleanup_messages(message.chat.id)
    schedule_delete_message(message.chat.id, msg.id)


@bot.callback_query_handler(cb_query_equals('add_time'), is_admin=True)
def add_time_command(call: types.CallbackQuery):
    msg = bot.edit_message_text(
        f"📅 Введите количество дней для изменения срока действия:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=BotKeyboard.inline_cancel_action())
    schedule_delete_message(call.message.chat.id, call.message.id)
    schedule_delete_message(call.message.chat.id, msg.id)
    return bot.register_next_step_handler(call.message, add_time_step)


def add_time_step(message):
    try:
        days = int(message.text)
        if not days:
            raise ValueError
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Количество дней должно быть числом и не равно нулю.')
        schedule_delete_message(message.chat.id, wait_msg.message_id)
        return bot.register_next_step_handler(wait_msg, add_time_step)
    schedule_delete_message(message.chat.id, message.message_id)
    msg = bot.send_message(
        message.chat.id,
        f"⚠️ Вы уверены? Это изменит срок действия ВСЕХ пользователей на <b>{days} дней</b>",
        parse_mode="html",
        reply_markup=BotKeyboard.confirm_action('add_time', days))
    cleanup_messages(message.chat.id)
    schedule_delete_message(message.chat.id, msg.id)


@bot.callback_query_handler(cb_query_startswith("inbound"), is_admin=True)
def inbound_command(call: types.CallbackQuery):
    bot.edit_message_text(
        f"Выберите входящее подключение для *{call.data[8:].title()}* для всех пользователей",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.inbounds_menu(call.data, xray.config.inbounds_by_tag))


@bot.callback_query_handler(cb_query_startswith("confirm_inbound"), is_admin=True)
def delete_expired_confirm_command(call: types.CallbackQuery):
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это применит {call.data[16:].replace(':', ' ')} для ВСЕХ пользователей‼️",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(action=call.data[8:]))


@bot.callback_query_handler(cb_query_startswith("edit:"), is_admin=True)
def edit_command(call: types.CallbackQuery):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    username = call.data.split(":")[1]
    with GetDB() as db:
        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(
                call.id,
                '❌ Пользователь не найден.',
                show_alert=True
            )
        user = UserResponse.model_validate(db_user)
    mem_store.set(f'{call.message.chat.id}:username', username)
    mem_store.set(f'{call.message.chat.id}:data_limit', db_user.data_limit)

    # if status is on_hold set expire_date to an integer that is duration else set a datetime
    if db_user.status == UserStatus.on_hold:
        mem_store.set(f'{call.message.chat.id}:expire_date', db_user.on_hold_expire_duration)
        mem_store.set(f'{call.message.chat.id}:expire_on_hold_timeout', db_user.on_hold_timeout)
        expire_date = db_user.on_hold_expire_duration
    else:
        mem_store.set(f'{call.message.chat.id}:expire_date',
                      datetime.fromtimestamp(db_user.expire) if db_user.expire else None)
        expire_date = datetime.fromtimestamp(db_user.expire) if db_user.expire else None
    mem_store.set(
        f'{call.message.chat.id}:protocols',
        {protocol.value: inbounds for protocol, inbounds in db_user.inbounds.items()})
    bot.edit_message_text(
        f"📝 Редактирование пользователя `{username}`",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.select_protocols(
            user.inbounds,
            "edit",
            username=username,
            data_limit=db_user.data_limit,
            expire_date=expire_date,
            expire_on_hold_duration=expire_date if isinstance(expire_date, int) else None,
            expire_on_hold_timeout=mem_store.get(f'{call.message.chat.id}:expire_on_hold_timeout'),
        )
    )


@bot.callback_query_handler(cb_query_equals('help_edit'), is_admin=True)
def help_edit_command(call: types.CallbackQuery):
    bot.answer_callback_query(
        call.id,
        text="Нажмите кнопку (✏️ Изменить) для редактирования",
        show_alert=True
    )


@bot.callback_query_handler(cb_query_equals('cancel'), is_admin=True)
def cancel_command(call: types.CallbackQuery):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    return bot.edit_message_text(
        get_system_info(),
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=BotKeyboard.main_menu()
    )


@bot.callback_query_handler(cb_query_startswith('edit_user:'), is_admin=True)
def edit_user_command(call: types.CallbackQuery):
    _, username, action = call.data.split(":")
    schedule_delete_message(call.message.chat.id, call.message.id)
    cleanup_messages(call.message.chat.id)
    expire_date = mem_store.get(f"{call.message.chat.id}:expire_date")
    if action == "data":
        msg = bot.send_message(
            call.message.chat.id,
            '📶 Введите лимит трафика (ГБ):\n⚠️ Отправьте 0 для безлимита.',
            reply_markup=BotKeyboard.inline_cancel_action(f'user:{username}')
        )
        mem_store.set(f"{call.message.chat.id}:edit_msg_text", call.message.text)
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        bot.register_next_step_handler(
            call.message, edit_user_data_limit_step, username)
        schedule_delete_message(call.message.chat.id, msg.message_id)
    elif action == "expire":
        text = """\
📅 Введите дату истечения, как показано ниже:
`3d` для 3 дней
`2m` для 2 месяцев
или дату в формате (ГГГГ-ММ-ДД)
⚠️ Отправьте 0, чтобы срок не истекал никогда."""
        if isinstance(expire_date, int):
            text = """\
📅 Введите длительность (on-hold), как показано ниже:
`3d` для 3 дней
`2m` для 2 месяцев"""
        msg = bot.send_message(
            call.message.chat.id,
            text,
            parse_mode="markdown",
            reply_markup=BotKeyboard.inline_cancel_action(f'user:{username}'))
        mem_store.set(f"{call.message.chat.id}:edit_msg_text", call.message.text)
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        bot.register_next_step_handler(
            call.message, edit_user_expire_step, username=username)
        schedule_delete_message(call.message.chat.id, msg.message_id)
    elif action == 'expire_on_hold_timeout':
        text = """\
📅 Введите таймаут для режима ожидания (on-hold):
`3d` для 3 дней
`2m` для 2 месяцев
или дату в формате (ГГГГ-ММ-ДД)
⚠️ Отправьте 0, чтобы срок не истекал никогда."""
        msg = bot.send_message(
            call.message.chat.id,
            text,
            parse_mode="markdown",
            reply_markup=BotKeyboard.inline_cancel_action(f'user:{username}'))
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        bot.register_next_step_handler(call.message, edit_user_expire_on_hold_timeout_step, username=username)
        schedule_delete_message(call.message.chat.id, msg.message_id)


def edit_user_expire_on_hold_timeout_step(message: types.Message, username: str):
    try:
        now = datetime.now()
        today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)
        if re.match(r'^[0-9]{1,3}([MmDd])$', message.text):
            expire_on_hold_timeout = today
            number = int(re.findall(r'^[0-9]{1,3}', message.text)[0])
            symbol = re.findall('[MmDd]$', message.text)[0].upper()
            if symbol == 'M':
                expire_on_hold_timeout = today + relativedelta(months=number)
            elif symbol == 'D':
                expire_on_hold_timeout = today + relativedelta(days=number)
        elif not message.text.isnumeric():
            expire_on_hold_timeout = datetime.strptime(message.text, "%Y-%m-%d")
        elif int(message.text) == 0:
            expire_on_hold_timeout = None
        else:
            raise ValueError
        if expire_on_hold_timeout and expire_on_hold_timeout < today:
            wait_msg = bot.send_message(message.chat.id, '❌ Дата истечения должна быть позже сегодняшней.')
            schedule_delete_message(message.chat.id, wait_msg.message_id)
            return bot.register_next_step_handler(wait_msg, edit_user_expire_on_hold_timeout_step, username=username)
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Дата не соответствует ни одному из форматов.')
        schedule_delete_message(message.chat.id, wait_msg.message_id)
        return bot.register_next_step_handler(wait_msg, edit_user_expire_on_hold_timeout_step, username=username)

    mem_store.set(f'{message.chat.id}:expire_on_hold_timeout', expire_on_hold_timeout)
    expire_date = mem_store.get(f"{message.chat.id}:expire_date")
    schedule_delete_message(message.chat.id, message.message_id)
    bot.send_message(
        message.chat.id,
        f"📝 Редактирование пользователя: <code>{username}</code>",
        parse_mode="html",
        reply_markup=BotKeyboard.select_protocols(
            mem_store.get(f'{message.chat.id}:protocols'), "edit",
            username=username, data_limit=mem_store.get(f'{message.chat.id}:data_limit'),
            expire_on_hold_duration=expire_date if isinstance(expire_date, int) else None,
            expire_on_hold_timeout=mem_store.get(f'{message.chat.id}:expire_on_hold_timeout')
        )
    )
    cleanup_messages(message.chat.id)


def edit_user_data_limit_step(message: types.Message, username: str):
    try:
        if float(message.text) < 0:
            wait_msg = bot.send_message(message.chat.id, '❌ Лимит данных должен быть больше или равен 0.')
            schedule_delete_message(message.chat.id, wait_msg.message_id)
            return bot.register_next_step_handler(wait_msg, edit_user_data_limit_step, username=username)
        data_limit = float(message.text) * 1024 * 1024 * 1024
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Лимит данных должен быть числом.')
        schedule_delete_message(message.chat.id, wait_msg.message_id)
        return bot.register_next_step_handler(wait_msg, edit_user_data_limit_step, username=username)
    mem_store.set(f'{message.chat.id}:data_limit', data_limit)
    schedule_delete_message(message.chat.id, message.message_id)
    text = mem_store.get(f"{message.chat.id}:edit_msg_text")
    mem_store.delete(f"{message.chat.id}:edit_msg_text")
    bot.send_message(
        message.chat.id,
        text or f"📝 Редактирование пользователя <code>{username}</code>",
        parse_mode="html",
        reply_markup=BotKeyboard.select_protocols(
            mem_store.get(f'{message.chat.id}:protocols'), "edit",
            username=username, data_limit=data_limit, expire_date=mem_store.get(f'{message.chat.id}:expire_date')))
    cleanup_messages(message.chat.id)


def edit_user_expire_step(message: types.Message, username: str):
    last_expiry = mem_store.get(f'{message.chat.id}:expire_date')
    try:
        now = datetime.now()
        today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)
        if re.match(r'^[0-9]{1,3}([MmDd])$', message.text):
            expire_date = today
            number_pattern = r'^[0-9]{1,3}'
            number = int(re.findall(number_pattern, message.text)[0])
            symbol_pattern = r'[MmDd]$'
            symbol = re.findall(symbol_pattern, message.text)[0].upper()
            if symbol == 'M':
                expire_date = today + relativedelta(months=number)
                if isinstance(last_expiry, int):
                    expire_date = number * 24 * 60 * 60 * 30
            elif symbol == 'D':
                expire_date = today + relativedelta(days=number)
                if isinstance(last_expiry, int):
                    expire_date = number * 24 * 60 * 60
        elif not message.text.isnumeric() and not isinstance(last_expiry, int):
            expire_date = datetime.strptime(message.text, "%Y-%m-%d")
        elif int(message.text) == 0:
            expire_date = None
        else:
            raise ValueError
        if expire_date and isinstance(expire_date, datetime) and expire_date < today:
            wait_msg = bot.send_message(message.chat.id, '❌ Дата истечения должна быть позже сегодняшней.')
            schedule_delete_message(message.chat.id, wait_msg.message_id)
            return bot.register_next_step_handler(wait_msg, edit_user_expire_step, username=username)
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Дата не соответствует ни одному из форматов.')
        schedule_delete_message(message.chat.id, wait_msg.message_id)
        return bot.register_next_step_handler(wait_msg, edit_user_expire_step, username=username)

    mem_store.set(f'{message.chat.id}:expire_date', expire_date)
    schedule_delete_message(message.chat.id, message.message_id)
    text = mem_store.get(f"{message.chat.id}:edit_msg_text")
    mem_store.delete(f"{message.chat.id}:edit_msg_text")
    bot.send_message(
        message.chat.id,
        text or f"📝 Редактирование пользователя: <code>{username}</code>",
        parse_mode="html",
        reply_markup=BotKeyboard.select_protocols(
            mem_store.get(f'{message.chat.id}:protocols'), "edit",
            username=username, data_limit=mem_store.get(f'{message.chat.id}:data_limit'),
            expire_date=expire_date,
            expire_on_hold_duration=expire_date if isinstance(expire_date, int) else None,
            expire_on_hold_timeout=mem_store.get(f'{message.chat.id}:expire_on_hold_timeout')))
    cleanup_messages(message.chat.id)


@bot.callback_query_handler(cb_query_startswith('users:'), is_admin=True)
def users_command(call: types.CallbackQuery):
    page = int(call.data.split(':')[1]) if len(call.data.split(':')) > 1 else 1
    with GetDB() as db:
        total_pages = math.ceil(crud.get_users_count(db) / 10)
        users = crud.get_users(db, offset=(page - 1) * 10, limit=10, sort=[crud.UsersSortingOptions["-created_at"]])
        text = """👥 Пользователи: (Стр {page}/{total_pages})
✅ Активен
❌ Отключен
🕰 Истек
🪫 С лимитом
🔌 В ожидании""".format(page=page, total_pages=total_pages)

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=BotKeyboard.user_list(
            users, page, total_pages=total_pages)
    )


@bot.callback_query_handler(cb_query_startswith('edit_note:'), is_admin=True)
def edit_note_command(call: types.CallbackQuery):
    username = call.data.split(':')[1]
    with GetDB() as db:
        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, '❌ Пользователь не найден.', show_alert=True)
    schedule_delete_message(call.message.chat.id, call.message.id)
    cleanup_messages(call.message.chat.id)
    msg = bot.send_message(
        call.message.chat.id,
        f'<b>📝 Текущая заметка:</b> <code>{db_user.note}</code>\n\nОтправьте новую заметку для <code>{username}</code>',
        parse_mode="HTML",
        reply_markup=BotKeyboard.inline_cancel_action(f'user:{username}'))
    mem_store.set(f'{call.message.chat.id}:username', username)
    schedule_delete_message(call.message.chat.id, msg.id)
    bot.register_next_step_handler(msg, edit_note_step)


def edit_note_step(message: types.Message):
    note = message.text or ''
    if len(note) > 500:
        wait_msg = bot.send_message(message.chat.id, '❌ Заметка не может быть длиннее 500 символов.')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, edit_note_step)
    with GetDB() as db:
        username = mem_store.get(f'{message.chat.id}:username')
        if not username:
            cleanup_messages(message.chat.id)
            bot.reply_to(message, '❌ Что-то пошло не так!\n Перезапустите бота /start')
        db_user = crud.get_user(db, username)
        last_note = db_user.note
        modify = UserModify(note=note)
        db_user = crud.update_user(db, db_user, modify)
        user = UserResponse.model_validate(db_user)
        bot.reply_to(
            message, get_user_info_text(db_user), parse_mode="html",
            reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))
        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
📝 <b>#Изменение_Заметки #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{user.username}</code>
<b>Предыдущая заметка:</b> <code>{last_note}</code>
<b>Новая заметка:</b> <code>{user.note}</code>
➖➖➖➖➖➖➖➖➖
<b>Автор:</b> <a href="tg://user?id={message.chat.id}">{message.from_user.full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass


@bot.callback_query_handler(cb_query_startswith('user:'), is_admin=True)
def user_command(call: types.CallbackQuery):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    username = call.data.split(':')[1]
    page = int(call.data.split(':')[2]) if len(call.data.split(':')) > 2 else 1
    with GetDB() as db:
        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, '❌ Пользователь не найден.', show_alert=True)
        user = UserResponse.model_validate(db_user)
        bot.edit_message_text(
            get_user_info_text(db_user),
            call.message.chat.id, call.message.message_id, parse_mode="HTML",
            reply_markup=BotKeyboard.user_menu({'username': user.username, 'status': user.status}, page=page))


@bot.callback_query_handler(cb_query_startswith("revoke_sub:"), is_admin=True)
def revoke_sub_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]
    bot.edit_message_text(
        f"⚠️ Вы уверены? Это *СБРОСИТ ссылку подписки* для `{username}`‼️",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="markdown",
        reply_markup=BotKeyboard.confirm_action(action=call.data))


@bot.callback_query_handler(cb_query_startswith("links:"), is_admin=True)
def links_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]

    with GetDB() as db:
        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, "Пользователь не найден!", show_alert=True)

        user = UserResponse.model_validate(db_user)

    text = f"<code>{user.subscription_url}</code>\n\n\n"
    for link in user.links:
        if len(text) > 4056:
            text += '\n\n<b>...</b>'
            break
        text += f'\n<code>{link}</code>'

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=BotKeyboard.show_links(username)
    )


@bot.callback_query_handler(cb_query_startswith("genqr:"), is_admin=True)
def genqr_command(call: types.CallbackQuery):
    qr_select = call.data.split(":")[1]
    username = call.data.split(":")[2]

    with GetDB() as db:
        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, "Пользователь не найден!", show_alert=True)

        user = UserResponse.model_validate(db_user)

        bot.answer_callback_query(call.id, "Генерация QR-кода...")

        if qr_select == 'configs':
            for link in user.links:
                f = io.BytesIO()
                qr = qrcode.QRCode(border=6)
                qr.add_data(link)
                qr.make_image().save(f)
                f.seek(0)
                bot.send_photo(
                    call.message.chat.id,
                    photo=f,
                    caption=f"<code>{link}</code>",
                    parse_mode="HTML"
                )
        else:
            data_limit = readable_size(user.data_limit) if user.data_limit else "Безлимитный"
            used_traffic = readable_size(user.used_traffic) if user.used_traffic else "-"
            data_left = readable_size(user.data_limit - user.used_traffic) if user.data_limit else "-"
            on_hold_timeout = user.on_hold_timeout.strftime("%Y-%m-%d") if user.on_hold_timeout else "-"
            on_hold_duration = user.on_hold_expire_duration // (24 * 60 * 60) if user.on_hold_expire_duration else None
            expiry_date = datetime.fromtimestamp(user.expire).date() if user.expire else "Никогда"
            time_left = time_to_string(datetime.fromtimestamp(user.expire)) if user.expire else "-"
            if user.status == UserStatus.on_hold:
                expiry_text = f"⏰ <b>Длительность ожидания:</b> <code>{on_hold_duration} дн.</code> (автостарт в <code>{
                    on_hold_timeout}</code>)"
            else:
                expiry_text = f"📅 <b>Дата истечения:</b> <code>{expiry_date}</code> ({time_left})"
            text = f"""\
{statuses[user.status]} <b>Статус:</b> <code>{statuses[user.status]}</code>

🔤 <b>Имя пользователя:</b> <code>{user.username}</code>

🔋 <b>Лимит данных:</b> <code>{data_limit}</code>
📶 <b>Использовано:</b> <code>{used_traffic}</code> (<code>{data_left}</code> осталось)
{expiry_text}
🚀 <b><a href="{user.subscription_url}">Подписка</a>:</b> <code>{user.subscription_url}</code>"""

            with io.BytesIO() as f:
                qr = qrcode.QRCode(border=6)
                qr.add_data(user.subscription_url)
                qr.make_image().save(f)
                f.seek(0)
                return bot.send_photo(
                    call.message.chat.id,
                    photo=f,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=BotKeyboard.subscription_page(user.subscription_url)
                )
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except ApiTelegramException:
        pass

    text = f"<code>{user.subscription_url}</code>\n\n\n"
    for link in user.links:
        if len(text) > 4056:
            text += '\n\n<b>...</b>'
            break
        text += f"<code>{link}</code>\n\n"

    bot.send_message(
        call.message.chat.id,
        text,
        "HTML",
        reply_markup=BotKeyboard.show_links(username)
    )


@bot.callback_query_handler(cb_query_startswith('template_charge:'), is_admin=True)
def template_charge_command(call: types.CallbackQuery):
    _, template_id, username = call.data.split(":")
    now = datetime.now()
    today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)
    with GetDB() as db:
        template = crud.get_user_template(db, template_id)
        if not template:
            return bot.answer_callback_query(call.id, "Шаблон не найден!", show_alert=True)
        template = UserTemplateResponse.model_validate(template)

        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, "Пользователь не найден!", show_alert=True)
        user = UserResponse.model_validate(db_user)
        if (user.data_limit and not user.expire) or (not user.data_limit and user.expire):
            expire = (datetime.fromtimestamp(db_user.expire) if db_user.expire else today)
            expire += relativedelta(seconds=template.expire_duration)
            db_user.expire = expire.timestamp()
            db_user.data_limit = (user.data_limit - user.used_traffic + template.data_limit
                                  ) if user.data_limit else template.data_limit
            db_user.status = UserStatus.active
            bot.edit_message_text(
                f"""\
‼️ <b>Если добавить лимит данных и время шаблона пользователю, получится следующее</b>:\n\n\
{get_user_info_text(db_user)}\n\n\
<b>Добавить лимит данных и время шаблона пользователю или сбросить к значениям по умолчанию шаблона</b>⁉️""",
                call.message.chat.id, call.message.message_id, parse_mode='html',
                reply_markup=BotKeyboard.charge_add_or_reset(
                    username=username, template_id=template_id))
        elif (not user.data_limit and not user.expire) or (user.used_traffic > user.data_limit) or (now > datetime.fromtimestamp(user.expire)):
            crud.reset_user_data_usage(db, db_user)
            expire_date = None
            if template.expire_duration:
                expire_date = today + relativedelta(seconds=template.expire_duration)
            modify = UserModify(
                status=UserStatusModify.active,
                expire=int(expire_date.timestamp()) if expire_date else 0,
                data_limit=template.data_limit,
            )
            db_user = crud.update_user(db, db_user, modify)
            xray.operations.add_user(db_user)
            bot.answer_callback_query(call.id, "🔋 Пользователь успешно пополнен!")
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode='html',
                reply_markup=BotKeyboard.user_menu(user_info={'status': 'active', 'username': user.username}))
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
🔋 <b>#Пополнен #Сброс #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Шаблон:</b> <code>{template.name}</code>
<b>Имя пользователя:</b> <code>{user.username}</code>
➖➖➖➖➖➖➖➖➖
<u><b>Предыдущий статус</b></u>
<b>├Лимит трафика:</b> <code>{readable_size(user.data_limit) if user.data_limit else "Никогда"}</code>
<b>├Дата истечения:</b> <code>\
{datetime.fromtimestamp(user.expire).strftime('%H:%M:%S %Y-%m-%d') if user.expire else "Никогда"}</code>
➖➖➖➖➖➖➖➖➖
<u><b>Новый статус</b></u>
<b>├Лимит трафика:</b> <code>{readable_size(db_user.data_limit) if db_user.data_limit else "Никогда"}</code>
<b>├Дата истечения:</b> <code>\
{datetime.fromtimestamp(db_user.expire).strftime('%H:%M:%S %Y-%m-%d') if db_user.expire else "Никогда"}</code>
➖➖➖➖➖➖➖➖➖
<b>Автор:</b> <a href="tg://user?id={call.from_user.id}">{call.from_user.full_name}</a>"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass
        else:
            expire = (datetime.fromtimestamp(db_user.expire) if db_user.expire else today)
            expire += relativedelta(seconds=template.expire_duration)
            db_user.expire = expire.timestamp()
            db_user.data_limit = (user.data_limit - user.used_traffic + template.data_limit
                                  ) if user.data_limit else template.data_limit
            db_user.status = UserStatus.active
            bot.edit_message_text(
                f"""\
‼️ <b>Если добавить лимит данных и время шаблона пользователю, получится следующее</b>:\n\n\
{get_user_info_text(db_user)}\n\n\
<b>Добавить лимит данных и время шаблона пользователю или сбросить к значениям по умолчанию шаблона</b>⁉️""",
                call.message.chat.id, call.message.message_id, parse_mode='html',
                reply_markup=BotKeyboard.charge_add_or_reset(
                    username=username, template_id=template_id))


@bot.callback_query_handler(cb_query_startswith('charge:'), is_admin=True)
def charge_command(call: types.CallbackQuery):
    username = call.data.split(":")[1]
    with GetDB() as db:
        templates = crud.get_user_templates(db)
        if not templates:
            return bot.answer_callback_query(call.id, "У вас нет шаблонов пользователей!")

        db_user = crud.get_user(db, username)
        if not db_user:
            return bot.answer_callback_query(call.id, "Пользователь не найден!", show_alert=True)

    bot.edit_message_text(
        f"{call.message.html_text}\n\n🔢 Выберите <b>шаблон пользователя</b> для пополнения:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='html',
        reply_markup=BotKeyboard.templates_menu(
            {template.name: template.id for template in templates},
            username=username,
        )
    )


@bot.callback_query_handler(cb_query_equals('template_add_user'), is_admin=True)
@bot.callback_query_handler(cb_query_equals('template_add_bulk_user'), is_admin=True)
def add_user_from_template_command(call: types.CallbackQuery):
    with GetDB() as db:
        templates = crud.get_user_templates(db)
        if not templates:
            return bot.answer_callback_query(call.id, "У вас нет шаблонов пользователей!")

    if call.data == "template_add_bulk_user":
        mem_store.set(f"{call.message.chat.id}:is_bulk", True)
        mem_store.set(f"{call.message.chat.id}:is_bulk_from_template", True)
    else:
        mem_store.set(f"{call.message.chat.id}:is_bulk", False)
        mem_store.set(f"{call.message.chat.id}:is_bulk_from_template", False)

    bot.edit_message_text(
        "<b>Выберите шаблон для создания пользователя</b>:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='html',
        reply_markup=BotKeyboard.templates_menu({template.name: template.id for template in templates})
    )


@bot.callback_query_handler(cb_query_startswith('template_add_user:'), is_admin=True)
def add_user_from_template(call: types.CallbackQuery):
    template_id = int(call.data.split(":")[1])
    with GetDB() as db:
        template = crud.get_user_template(db, template_id)
        if not template:
            return bot.answer_callback_query(call.id, "Шаблон не найден!", show_alert=True)
        template = UserTemplateResponse.model_validate(template)

    text = get_template_info_text(template)
    if template.username_prefix:
        text += f"\n⚠️ Имя пользователя будет иметь префикс <code>{template.username_prefix}</code>"
    if template.username_suffix:
        text += f"\n⚠️ Имя пользователя будет иметь суффикс <code>{template.username_suffix}</code>"

    mem_store.set(f"{call.message.chat.id}:template_id", template.id)
    template_msg = bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )
    text = '👤 Введите имя пользователя:\n⚠️ Имя пользователя должно быть от 3 до 32 символов and contain a-z, A-Z, 0-9, and underscores in between.'
    msg = bot.send_message(
        call.message.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=BotKeyboard.random_username(template_id=template.id)
    )
    schedule_delete_message(call.message.chat.id, template_msg.message_id, msg.id)
    bot.register_next_step_handler(template_msg, add_user_from_template_username_step)


@bot.callback_query_handler(cb_query_startswith('random'), is_admin=True)
def random_username(call: types.CallbackQuery):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    template_id = int(call.data.split(":")[1] or 0)
    mem_store.delete(f'{call.message.chat.id}:template_id')

    username = ''.join([random.choice(string.ascii_letters)] +
                       random.choices(string.ascii_letters + string.digits, k=7))

    schedule_delete_message(call.message.chat.id, call.message.id)
    cleanup_messages(call.message.chat.id)
    if mem_store.get(f"{call.message.chat.id}:is_bulk", False) and not mem_store.get(f"{call.message.chat.id}:is_bulk_from_template", False):
        msg = bot.send_message(call.message.chat.id,
                               'Сколько пользователей вы хотите создать?',
                               reply_markup=BotKeyboard.inline_cancel_action())
        schedule_delete_message(call.message.chat.id, msg.id)
        return bot.register_next_step_handler(msg, add_user_bulk_number_step, username=username)

    if not template_id:
        msg = bot.send_message(call.message.chat.id,
                               '⬆️ Введите лимит трафика (ГБ):\n⚠️ Отправьте 0 для безлимита.',
                               reply_markup=BotKeyboard.inline_cancel_action())
        schedule_delete_message(call.message.chat.id, msg.id)
        return bot.register_next_step_handler(call.message, add_user_data_limit_step, username=username)

    with GetDB() as db:
        template = crud.get_user_template(db, template_id)
        if template.username_prefix:
            username = template.username_prefix + username
        if template.username_suffix:
            username += template.username_suffix

        template = UserTemplateResponse.model_validate(template)
    mem_store.set(f"{call.message.chat.id}:username", username)
    mem_store.set(f"{call.message.chat.id}:data_limit", template.data_limit)
    mem_store.set(f"{call.message.chat.id}:protocols", template.inbounds)
    now = datetime.now()
    today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)
    expire_date = None
    if template.expire_duration:
        expire_date = today + relativedelta(seconds=template.expire_duration)
    mem_store.set(f"{call.message.chat.id}:expire_date", expire_date)

    text = f"📝 Создание пользователя <code>{username}</code>\n" + get_template_info_text(template)

    mem_store.set(f"{call.message.chat.id}:template_info_text", text)

    if mem_store.get(f"{call.message.chat.id}:is_bulk", False):
        msg = bot.send_message(call.message.chat.id,
                               'Сколько пользователей вы хотите создать?',
                               reply_markup=BotKeyboard.inline_cancel_action())
        schedule_delete_message(call.message.chat.id, msg.id)
        return bot.register_next_step_handler(msg, add_user_bulk_number_step, username=username)
    else:
        if expire_date:
            msg = bot.send_message(
                call.message.chat.id,
                '⚡ Выберите статус пользователя:\nВ ожидании: Срок действия начинается после первого подключения\nАктивен: Срок действия начинается сейчас',
                reply_markup=BotKeyboard.user_status_select())
            schedule_delete_message(call.message.chat.id, msg.id)
        else:
            mem_store.set(f"{call.message.chat.id}:template_info_text", None)
            mem_store.set(f"{call.message.chat.id}:user_status", UserStatus.active)
            bot.send_message(
                call.message.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=BotKeyboard.select_protocols(
                    template.inbounds,
                    "create_from_template",
                    username=username,
                    data_limit=template.data_limit,
                    expire_date=expire_date,))


def add_user_from_template_username_step(message: types.Message):
    template_id = mem_store.get(f"{message.chat.id}:template_id")
    if template_id is None:
        return bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

    if not message.text:
        wait_msg = bot.send_message(message.chat.id, '❌ Имя пользователя не может быть пустым.')
        schedule_delete_message(message.chat.id, wait_msg.message_id, message.message_id)
        return bot.register_next_step_handler(wait_msg, add_user_from_template_username_step)

    with GetDB() as db:
        username = message.text

        template = crud.get_user_template(db, template_id)
        if template.username_prefix:
            username = template.username_prefix + username
        if template.username_suffix:
            username += template.username_suffix

        match = re.match(r"^(?=\w{3,32}\b)[a-zA-Z0-9-_@.]+(?:_[a-zA-Z0-9-_@.]+)*$", username)
        if not match:
            wait_msg = bot.send_message(
                message.chat.id,
                '❌ Имя пользователя должно быть от 3 до 32 символов и содержать a-z, A-Z, 0-9 и подчеркивания.')
            schedule_delete_message(message.chat.id, wait_msg.message_id, message.message_id)
            return bot.register_next_step_handler(wait_msg, add_user_from_template_username_step)

        if len(username) < 3:
            wait_msg = bot.send_message(
                message.chat.id,
                f"❌ Имя пользователя слишком короткое (минимум 3 символа): <code>{username}</code>",
                parse_mode="HTML")
            schedule_delete_message(message.chat.id, wait_msg.message_id, message.message_id)
            return bot.register_next_step_handler(wait_msg, add_user_from_template_username_step)
        elif len(username) > 32:
            wait_msg = bot.send_message(
                message.chat.id,
                f"❌ Имя пользователя слишком длинное (максимум 32 символа): <code>{username}</code>",
                parse_mode="HTML")
            schedule_delete_message(message.chat.id, wait_msg.message_id, message.message_id)
            return bot.register_next_step_handler(wait_msg, add_user_from_template_username_step)

        if crud.get_user(db, username):
            wait_msg = bot.send_message(message.chat.id, '❌ Имя пользователя уже существует.')
            schedule_delete_message(message.chat.id, wait_msg.message_id, message.message_id)
            return bot.register_next_step_handler(wait_msg, add_user_from_template_username_step)
        template = UserTemplateResponse.model_validate(template)
    mem_store.set(f"{message.chat.id}:username", username)
    mem_store.set(f"{message.chat.id}:data_limit", template.data_limit)
    mem_store.set(f"{message.chat.id}:protocols", template.inbounds)
    now = datetime.now()
    today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)
    expire_date = None
    if template.expire_duration:
        expire_date = today + relativedelta(seconds=template.expire_duration)
    mem_store.set(f"{message.chat.id}:expire_date", expire_date)

    text = f"📝 Создание пользователя <code>{username}</code>\n" + get_template_info_text(template)

    mem_store.set(f"{message.chat.id}:template_info_text", text)

    if mem_store.get(f"{message.chat.id}:is_bulk", False):
        msg = bot.send_message(message.chat.id,
                               'Сколько пользователей вы хотите создать?',
                               reply_markup=BotKeyboard.inline_cancel_action())
        schedule_delete_message(message.chat.id, msg.id)
        return bot.register_next_step_handler(msg, add_user_bulk_number_step, username=username)
    else:
        if expire_date:
            msg = bot.send_message(
                message.chat.id,
                '⚡ Выберите статус пользователя:\nВ ожидании: Срок действия начинается после первого подключения\nАктивен: Срок действия начинается сейчас',
                reply_markup=BotKeyboard.user_status_select())
            schedule_delete_message(message.chat.id, msg.id)
        else:
            mem_store.set(f"{message.chat.id}:template_info_text", None)
            mem_store.set(f"{message.chat.id}:user_status", UserStatus.active)
            bot.send_message(
                message.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=BotKeyboard.select_protocols(
                    template.inbounds,
                    "create_from_template",
                    username=username,
                    data_limit=template.data_limit,
                    expire_date=expire_date,))


@bot.callback_query_handler(cb_query_equals('add_bulk_user'), is_admin=True)
@bot.callback_query_handler(cb_query_equals('add_user'), is_admin=True)
def add_user_command(call: types.CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except ApiTelegramException:  # noqa
        pass

    if call.data == "add_bulk_user":
        mem_store.set(f"{call.message.chat.id}:is_bulk", True)
    else:
        mem_store.set(f"{call.message.chat.id}:is_bulk", False)

    mem_store.set(f"{call.message.chat.id}:is_bulk_from_template", False)

    username_msg = bot.send_message(
        call.message.chat.id,
        '👤 Введите имя пользователя:\n⚠️Имя пользователя должно быть от 3 до 32 символов и содержать a-z, A-Z 0-9 и подчеркивания.',
        reply_markup=BotKeyboard.random_username())
    schedule_delete_message(call.message.chat.id, username_msg.id)
    bot.register_next_step_handler(username_msg, add_user_username_step)


def add_user_username_step(message: types.Message):
    username = message.text
    if not username:
        wait_msg = bot.send_message(message.chat.id, '❌ Имя пользователя не может быть пустым.')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, add_user_username_step)
    if not re.match(r"^(?=\w{3,32}\b)[a-zA-Z0-9-_@.]+(?:_[a-zA-Z0-9-_@.]+)*$", username):
        wait_msg = bot.send_message(
            message.chat.id,
            '❌ Имя пользователя должно быть от 3 до 32 символов и содержать a-z, A-Z, 0-9 и подчеркивания.')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, add_user_username_step)
    with GetDB() as db:
        if crud.get_user(db, username):
            wait_msg = bot.send_message(message.chat.id, '❌ Имя пользователя уже существует.')
            schedule_delete_message(message.chat.id, wait_msg.id)
            schedule_delete_message(message.chat.id, message.id)
            return bot.register_next_step_handler(wait_msg, add_user_username_step)
    schedule_delete_message(message.chat.id, message.id)
    cleanup_messages(message.chat.id)
    if mem_store.get(f"{message.chat.id}:is_bulk", False):
        msg = bot.send_message(message.chat.id,
                               'Сколько пользователей вы хотите создать?',
                               reply_markup=BotKeyboard.inline_cancel_action())
        schedule_delete_message(message.chat.id, msg.id)
        return bot.register_next_step_handler(msg, add_user_bulk_number_step, username=username)
    msg = bot.send_message(message.chat.id,
                           '⬆️ Введите лимит трафика (ГБ):\n⚠️ Отправьте 0 для безлимита.',
                           reply_markup=BotKeyboard.inline_cancel_action())
    schedule_delete_message(message.chat.id, msg.id)
    bot.register_next_step_handler(msg, add_user_data_limit_step, username=username)


def add_user_bulk_number_step(message: types.Message, username: str):
    try:
        if int(message.text) < 1:
            wait_msg = bot.send_message(message.chat.id, '❌ Количество должно быть 1 или больше.')
            schedule_delete_message(message.chat.id, wait_msg.id)
            schedule_delete_message(message.chat.id, message.id)
            return bot.register_next_step_handler(wait_msg, add_user_bulk_number_step, username=username)
        mem_store.set(f'{message.chat.id}:number', int(message.text))
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Количество должно быть числом.')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, add_user_bulk_number_step, username=username)

    schedule_delete_message(message.chat.id, message.id)
    cleanup_messages(message.chat.id)
    if mem_store.get(f"{message.chat.id}:is_bulk_from_template", False):
        expire_date = mem_store.get(f'{message.chat.id}:expire_date')
        if expire_date:
            msg = bot.send_message(
                message.chat.id,
                '⚡ Выберите статус пользователя:\nВ ожидании: Срок действия начинается после первого подключения\nАктивен: Срок действия начинается сейчас',
                reply_markup=BotKeyboard.user_status_select())
            schedule_delete_message(message.chat.id, msg.id)
            return
        else:
            text = mem_store.get(f"{message.chat.id}:template_info_text")
            mem_store.set(f"{message.chat.id}:template_info_text", None)
            inbounds = mem_store.get(f"{message.chat.id}:protocols")
            mem_store.set(f'{message.chat.id}:user_status', UserStatus.active)
            data_limit = mem_store.get(f'{message.chat.id}:data_limit')
            return bot.send_message(
                message.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=BotKeyboard.select_protocols(
                    inbounds,
                    "create_from_template",
                    username=username,
                    data_limit=data_limit,
                    expire_date=expire_date,))

    msg = bot.send_message(message.chat.id,
                           '⬆️ Введите лимит трафика (ГБ):\n⚠️ Отправьте 0 для безлимита.',
                           reply_markup=BotKeyboard.inline_cancel_action())
    schedule_delete_message(message.chat.id, msg.id)
    bot.register_next_step_handler(msg, add_user_data_limit_step, username=username)


def add_user_data_limit_step(message: types.Message, username: str):
    try:
        if float(message.text) < 0:
            wait_msg = bot.send_message(message.chat.id, '❌ Лимит данных должен быть 0 или больше.')
            schedule_delete_message(message.chat.id, wait_msg.id)
            schedule_delete_message(message.chat.id, message.id)
            return bot.register_next_step_handler(wait_msg, add_user_data_limit_step, username=username)
        data_limit = float(message.text) * 1024 * 1024 * 1024
    except ValueError:
        wait_msg = bot.send_message(message.chat.id, '❌ Лимит данных должен быть числом.')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, add_user_data_limit_step, username=username)

    schedule_delete_message(message.chat.id, message.id)
    cleanup_messages(message.chat.id)
    msg = bot.send_message(
        message.chat.id,
        '⚡ Выберите статус пользователя:\nВ ожидании: Срок действия начинается после первого подключения\nАктивен: Срок действия начинается сейчас',
        reply_markup=BotKeyboard.user_status_select())
    schedule_delete_message(message.chat.id, msg.id)

    mem_store.set(f'{message.chat.id}:data_limit', data_limit)
    mem_store.set(f'{message.chat.id}:username', username)


@bot.callback_query_handler(cb_query_startswith('status:'), is_admin=True)
def add_user_status_step(call: types.CallbackQuery):
    user_status = call.data.split(':')[1]
    username = mem_store.get(f'{call.message.chat.id}:username')
    data_limit = mem_store.get(f'{call.message.chat.id}:data_limit')

    if user_status not in ['active', 'onhold']:
        return bot.answer_callback_query(call.id, '❌ Некорректный статус. Пожалуйста, выберите Активен или В ожидании.')

    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.delete_message(call.message.chat.id, call.message.message_id)

    if text := mem_store.get(f"{call.message.chat.id}:template_info_text"):
        mem_store.set(f"{call.message.chat.id}:template_info_text", None)
        inbounds = mem_store.get(f"{call.message.chat.id}:protocols")
        expire_date = mem_store.get(f'{call.message.chat.id}:expire_date')
        mem_store.set(f'{call.message.chat.id}:user_status', user_status)
        if user_status == "onhold":
            mem_store.set(f'{call.message.chat.id}:onhold_timeout', None)
        return bot.send_message(
            call.message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=BotKeyboard.select_protocols(
                inbounds,
                "create_from_template",
                username=username,
                data_limit=data_limit,
                expire_date=expire_date,))

    if user_status == 'onhold':
        expiry_message = '⬆️ Введите количество дней (длительность):\nМожно использовать формат: 30d или 1m'
    else:
        expiry_message = '⬆️ Введите дату истечения (ГГГГ-ММ-ДД):\nИли используйте формат: 30d или 1m\n⚠️ Отправьте 0 для бессрочного.'

    msg = bot.send_message(
        call.message.chat.id,
        expiry_message,
        reply_markup=BotKeyboard.inline_cancel_action())
    schedule_delete_message(call.message.chat.id, msg.id)
    bot.register_next_step_handler(msg, add_user_expire_step, username=username,
                                   data_limit=data_limit, user_status=user_status)


def add_user_expire_step(message: types.Message, username: str, data_limit: int, user_status: str):
    try:
        now = datetime.now()
        today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)

        if re.match(r'^[0-9]{1,3}([MmDd])$', message.text):
            number_pattern = r'^[0-9]{1,3}'
            number = int(re.findall(number_pattern, message.text)[0])
            symbol_pattern = r'([MmDd])$'
            symbol = re.findall(symbol_pattern, message.text)[0].upper()

            if user_status == 'onhold':
                if symbol == 'M':
                    expire_date = number * 30
                else:
                    expire_date = number
            else:  # active
                if symbol == 'M':
                    expire_date = today + relativedelta(months=number)
                else:
                    expire_date = today + relativedelta(days=number)
        elif message.text == '0':
            if user_status == 'onhold':
                raise ValueError("Количество дней обязательно для пользователя в режиме ожидания.")
            expire_date = None
        elif user_status == 'active':
            expire_date = datetime.strptime(message.text, "%Y-%m-%d")
            if expire_date < today:
                raise ValueError("Дата истечения должна быть больше сегодняшней.")
        else:
            raise ValueError("Неверный ввод для статуса ожидания.")
    except ValueError as e:
        error_message = str(e) if str(e) != "Неверный ввод для статуса ожидания." else "Неверный ввод. Попробуйте еще раз."
        wait_msg = bot.send_message(message.chat.id, f'❌ {error_message}')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(
            wait_msg, add_user_expire_step, username=username, data_limit=data_limit, user_status=user_status)

    mem_store.set(f'{message.chat.id}:username', username)
    mem_store.set(f'{message.chat.id}:data_limit', data_limit)
    mem_store.set(f'{message.chat.id}:user_status', user_status)
    mem_store.set(f'{message.chat.id}:expire_date', expire_date)

    schedule_delete_message(message.chat.id, message.id)
    cleanup_messages(message.chat.id)
    if user_status == "onhold":
        timeout_message = '⬆️ Введите таймаут (ГГГГ-ММ-ДД)\nИли используйте формат: ^[0-9]{1,3}(M|D) :\n⚠️ Введите 0 для бессрочного.'
        msg = bot.send_message(
            message.chat.id,
            timeout_message,
            reply_markup=BotKeyboard.inline_cancel_action()
        )
        schedule_delete_message(message.chat.id, msg.id)
        return bot.register_next_step_handler(msg, add_on_hold_timeout)

    bot.send_message(
        message.chat.id, 'Выберите протоколы:\nИмя пользователя: {}\nЛимит данных: {}\nСтатус: {}\nДата истечения: {}'.format(
            mem_store.get(f'{message.chat.id}:username'),
            readable_size(mem_store.get(f'{message.chat.id}:data_limit'))
            if mem_store.get(f'{message.chat.id}:data_limit') else "Безлимитно", 
            "В ожидании" if mem_store.get(f'{message.chat.id}:user_status') == "onhold" else "Активен",
            mem_store.get(f'{message.chat.id}:expire_date').strftime("%Y-%m-%d")
            if isinstance(mem_store.get(f'{message.chat.id}:expire_date'),
                          datetime) else f"{mem_store.get(f'{message.chat.id}:expire_date')} дн."
            if mem_store.get(f'{message.chat.id}:expire_date') else 'Никогда'),
        reply_markup=BotKeyboard.select_protocols(
            mem_store.get(f'{message.chat.id}:protocols', {}), action="create"))


def add_on_hold_timeout(message: types.Message):
    try:
        now = datetime.now()
        today = datetime(year=now.year, month=now.month, day=now.day, hour=23, minute=59, second=59)

        if re.match(r'^[0-9]{1,3}([MmDd])$', message.text):
            number_pattern = r'^[0-9]{1,3}'
            number = int(re.findall(number_pattern, message.text)[0])
            symbol_pattern = r'([MmDd])$'
            symbol = re.findall(symbol_pattern, message.text)[0].upper()
            if symbol == 'M':
                onhold_timeout = today + relativedelta(months=number)
            else:
                onhold_timeout = today + relativedelta(days=number)
        elif message.text == '0':
            onhold_timeout = None
        else:
            onhold_timeout = datetime.strptime(message.text, "%Y-%m-%d")
            if onhold_timeout < today:
                raise ValueError("Дата истечения таймаута должна быть в будущем.")
    except ValueError as e:
        error_message = str(e)
        if "strptime" in error_message:
            error_message = "Неверный формат даты. Используйте ГГГГ-ММ-ДД или формат 30d/1m."
        wait_msg = bot.send_message(message.chat.id, f'❌ {error_message}')
        schedule_delete_message(message.chat.id, wait_msg.id)
        schedule_delete_message(message.chat.id, message.id)
        return bot.register_next_step_handler(wait_msg, add_on_hold_timeout)

    mem_store.set(f'{message.chat.id}:onhold_timeout', onhold_timeout)

    schedule_delete_message(message.chat.id, message.id)
    cleanup_messages(message.chat.id)

    bot.send_message(
        message.chat.id, 'Выберите протоколы:\nИмя пользователя: {}\nЛимит данных: {}\nСтатус: {}\nДата истечения: {}'.format(
            mem_store.get(f'{message.chat.id}:username'),
            readable_size(mem_store.get(f'{message.chat.id}:data_limit'))
            if mem_store.get(f'{message.chat.id}:data_limit') else "Безлимитно", 
            "В ожидании" if mem_store.get(f'{message.chat.id}:user_status') == "onhold" else "Активен",
            mem_store.get(f'{message.chat.id}:expire_date').strftime("%Y-%m-%d")
            if isinstance(mem_store.get(f'{message.chat.id}:expire_date'),
                          datetime) else f"{mem_store.get(f'{message.chat.id}:expire_date')} дн."
            if mem_store.get(f'{message.chat.id}:expire_date') else 'Никогда'),
        reply_markup=BotKeyboard.select_protocols(
            mem_store.get(f'{message.chat.id}:protocols', {}), action="create"))


@bot.callback_query_handler(cb_query_startswith('select_inbound:'), is_admin=True)
def select_inbounds(call: types.CallbackQuery):
    if not (username := mem_store.get(f'{call.message.chat.id}:username')):
        return bot.answer_callback_query(call.id, '❌ Пользователь не выбран.', show_alert=True)
    protocols: dict[str, list[str]] = mem_store.get(f'{call.message.chat.id}:protocols', {})
    _, inbound, action = call.data.split(':')
    for protocol, inbounds in xray.config.inbounds_by_protocol.items():
        for i in inbounds:
            if i['tag'] != inbound:
                continue
            if not inbound in protocols[protocol]:
                protocols[protocol].append(inbound)
            else:
                protocols[protocol].remove(inbound)
            if len(protocols[protocol]) < 1:
                del protocols[protocol]

    mem_store.set(f'{call.message.chat.id}:protocols', protocols)

    if action in ["edit", "create_from_template"]:
        return bot.edit_message_text(
            call.message.text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=BotKeyboard.select_protocols(
                protocols,
                "edit",
                username=username,
                data_limit=mem_store.get(f"{call.message.chat.id}:data_limit"),
                expire_date=mem_store.get(f"{call.message.chat.id}:expire_date"))
        )
    bot.edit_message_text(
        call.message.text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=BotKeyboard.select_protocols(protocols, "create")
    )


@bot.callback_query_handler(cb_query_startswith('select_protocol:'), is_admin=True)
def select_protocols(call: types.CallbackQuery):
    if not (username := mem_store.get(f'{call.message.chat.id}:username')):
        return bot.answer_callback_query(call.id, '❌ Пользователь не выбран.', show_alert=True)
    protocols: dict[str, list[str]] = mem_store.get(f'{call.message.chat.id}:protocols', {})
    _, protocol, action = call.data.split(':')
    if protocol in protocols:
        del protocols[protocol]
    else:
        protocols.update(
            {protocol: [inbound['tag'] for inbound in xray.config.inbounds_by_protocol[protocol]]})
    mem_store.set(f'{call.message.chat.id}:protocols', protocols)

    if action in ["edit", "create_from_template"]:
        return bot.edit_message_text(
            call.message.text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=BotKeyboard.select_protocols(
                protocols,
                "edit",
                username=username,
                data_limit=mem_store.get(f"{call.message.chat.id}:data_limit"),
                expire_date=mem_store.get(f"{call.message.chat.id}:expire_date"))
        )
    bot.edit_message_text(
        call.message.text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=BotKeyboard.select_protocols(protocols, action="create")
    )


@bot.callback_query_handler(cb_query_startswith('confirm:'), is_admin=True)
def confirm_user_command(call: types.CallbackQuery):
    data = call.data.split(':')[1]
    chat_id = call.from_user.id
    full_name = call.from_user.full_name
    now = datetime.now()
    today = datetime(
        year=now.year,
        month=now.month,
        day=now.day,
        hour=23,
        minute=59,
        second=59)
    if data == 'delete':
        username = call.data.split(':')[2]
        with GetDB() as db:
            db_user = crud.get_user(db, username)
            crud.remove_user(db, db_user)
            xray.operations.remove_user(db_user)

        bot.edit_message_text(
            '✅ Пользователь удален.',
            call.message.chat.id,
            call.message.message_id,
            reply_markup=BotKeyboard.main_menu()
        )
        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
🗑 <b>#Удален #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{db_user.username}</code>
<b>Лимит трафика:</b> <code>{readable_size(db_user.data_limit) if db_user.data_limit else "Безлимитный"}</code>
<b>Дата истечения:</b> <code>\
{datetime.fromtimestamp(db_user.expire).strftime('%H:%M:%S %Y-%m-%d') if db_user.expire else "Никогда"}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass
    elif data == "suspend":
        username = call.data.split(":")[2]
        with GetDB() as db:
            db_user = crud.get_user(db, username)
            crud.update_user(db, db_user, UserModify(
                status=UserStatusModify.disabled))
            xray.operations.remove_user(db_user)
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=BotKeyboard.user_menu(user_info={'status': 'disabled', 'username': db_user.username}))
        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
❌ <b>#Отключен #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{username}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass
    elif data == "activate":
        username = call.data.split(":")[2]
        with GetDB() as db:
            db_user = crud.get_user(db, username)
            crud.update_user(db, db_user, UserModify(
                status=UserStatusModify.active))
            xray.operations.add_user(db_user)
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=BotKeyboard.user_menu(user_info={'status': 'active', 'username': db_user.username}))
        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
✅ <b>#Активирован #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{username}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass
    elif data == 'reset_usage':
        username = call.data.split(":")[2]
        with GetDB() as db:
            db_user = crud.get_user(db, username)
            crud.reset_user_data_usage(db, db_user)
            if db_user.status in [UserStatus.active, UserStatus.on_hold]:
                xray.operations.add_user(db_user)
            user = UserResponse.model_validate(db_user)
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))
        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
🔁 <b>#Сброс_трафика #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{username}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass
    elif data == 'restart':
        m = bot.edit_message_text(
            '🔄 Перезапуск XRay core...', call.message.chat.id, call.message.message_id)
        config = xray.config.include_db_users()
        xray.core.restart(config)
        for node_id, node in list(xray.nodes.items()):
            if node.connected:
                xray.operations.restart_node(node_id, config)
        bot.edit_message_text(
            '✅ XRay core успешно перезапущен.',
            m.chat.id, m.message_id,
            reply_markup=BotKeyboard.main_menu()
        )

    elif data in ['charge_add', 'charge_reset']:
        _, _, username, template_id = call.data.split(":")
        with GetDB() as db:
            template = crud.get_user_template(db, template_id)
            if not template:
                return bot.answer_callback_query(call.id, "Шаблон не найден!", show_alert=True)
            template = UserTemplateResponse.model_validate(template)

            db_user = crud.get_user(db, username)
            if not db_user:
                return bot.answer_callback_query(call.id, "Пользователь не найден!", show_alert=True)
            user = UserResponse.model_validate(db_user)

            inbounds = template.inbounds
            proxies = {p.type.value: p.settings for p in db_user.proxies}

            for protocol in xray.config.inbounds_by_protocol:
                if protocol in inbounds and protocol not in db_user.inbounds:
                    proxies.update({protocol: {}})
                elif protocol in db_user.inbounds and protocol not in inbounds:
                    del proxies[protocol]

            crud.reset_user_data_usage(db, db_user)
            if data == 'charge_reset':
                expire_date = None
                if template.expire_duration:
                    expire_date = today + relativedelta(seconds=template.expire_duration)
                modify = UserModify(
                    status=UserStatus.active,
                    expire=int(expire_date.timestamp()) if expire_date else 0,
                    data_limit=template.data_limit,
                )
            else:
                expire_date = None
                if template.expire_duration:
                    expire_date = (datetime.fromtimestamp(user.expire)
                                   if user.expire else today) + relativedelta(seconds=template.expire_duration)
                modify = UserModify(
                    status=UserStatus.active,
                    expire=int(expire_date.timestamp()) if expire_date else 0,
                    data_limit=(user.data_limit or 0) - user.used_traffic + template.data_limit,
                )
            db_user = crud.update_user(db, db_user, modify)
            xray.operations.add_user(db_user)
            bot.answer_callback_query(call.id, "🔋 Пользователь успешно пополнен!")
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode='html',
                reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
🔋 <b>#Пополнен #{'Сброс' if data.split('_')[1] == 'reset' else 'Добавление'} #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Шаблон:</b> <code>{template.name}</code>
<b>Имя пользователя:</b> <code>{user.username}</code>
➖➖➖➖➖➖➖➖➖
<u><b>Последний статус</b></u>
<b>├Лимит трафика:</b> <code>{readable_size(user.data_limit) if user.data_limit else "Безлимитный"}</code>
<b>├Дата истечения:</b> <code>\
{datetime.fromtimestamp(user.expire).strftime('%H:%M:%S %Y-%m-%d') if user.expire else "Никогда"}</code>
➖➖➖➖➖➖➖➖➖
<u><b>Новый статус</b></u>
<b>├Лимит трафика:</b> <code>{readable_size(db_user.data_limit) if db_user.data_limit else "Безлимитный"}</code>
<b>├Дата истечения:</b> <code>\
{datetime.fromtimestamp(db_user.expire).strftime('%H:%M:%S %Y-%m-%d') if db_user.expire else "Никогда"}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>\
"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass

    elif data == 'edit_user':
        if (username := mem_store.get(f'{call.message.chat.id}:username')) is None:
            try:
                bot.delete_message(call.message.chat.id,
                                   call.message.message_id)
            except Exception:
                pass
            return bot.send_message(
                call.message.chat.id,
                '❌ Обнаружена перезагрузка бота. Пожалуйста, начните заново.',
                reply_markup=BotKeyboard.main_menu()
            )

        if not mem_store.get(f'{call.message.chat.id}:protocols'):
            return bot.answer_callback_query(
                call.id,
                '❌ Не выбраны входящие подключения.',
                show_alert=True
            )

        inbounds: dict[str, list[str]] = {
            k: v for k, v in mem_store.get(f'{call.message.chat.id}:protocols').items() if v}

        with GetDB() as db:
            db_user = crud.get_user(db, username)
            if not db_user:
                return bot.answer_callback_query(call.id, text=f"Пользователь не найден!", show_alert=True)

            proxies = {p.type.value: p.settings for p in db_user.proxies}

            for protocol in xray.config.inbounds_by_protocol:
                if protocol in inbounds and protocol not in db_user.inbounds:
                    proxies.update({protocol: {'flow': TELEGRAM_DEFAULT_VLESS_FLOW} if
                                    TELEGRAM_DEFAULT_VLESS_FLOW and protocol == ProxyTypes.VLESS else {}})
                elif protocol in db_user.inbounds and protocol not in inbounds:
                    del proxies[protocol]

            data_limit = mem_store.get(f"{call.message.chat.id}:data_limit")
            expire_date = mem_store.get(f'{call.message.chat.id}:expire_date')
            if isinstance(expire_date, int):
                modify = UserModify(
                    on_hold_expire_duration=expire_date,
                    on_hold_timeout=mem_store.get(f'{call.message.chat.id}:expire_on_hold_timeout'),
                    data_limit=data_limit,
                    proxies=proxies,
                    inbounds=inbounds
                )
            else:
                modify = UserModify(
                    expire=int(expire_date.timestamp()) if expire_date else 0,
                    data_limit=data_limit,
                    proxies=proxies,
                    inbounds=inbounds
                )
            last_user = UserResponse.model_validate(db_user)
            db_user = crud.update_user(db, db_user, modify)

            user = UserResponse.model_validate(db_user)

            if user.status == UserStatus.active:
                xray.operations.update_user(db_user)

            bot.answer_callback_query(call.id, "✅ Пользователь успешно обновлен.")
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=BotKeyboard.user_menu({'username': db_user.username, 'status': db_user.status}))
        if TELEGRAM_LOGGER_CHANNEL_ID:
            tag = f'\n➖➖➖➖➖➖➖➖➖ \n<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>'
            if last_user.data_limit != user.data_limit:
                text = f"""\
📶 <b>#Изменение_Трафика #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{user.username}</code>
<b>Прошлый лимит трафика:</b> <code>{readable_size(last_user.data_limit) if last_user.data_limit else "Безлимитный"}</code>
<b>Новый лимит трафика:</b> <code>{readable_size(user.data_limit) if user.data_limit else "Безлимитный"}</code>{tag}"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass
            if last_user.expire != user.expire:
                text = f"""\
📅 <b>#Изменение_Срока #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{user.username}</code>
<b>Прошлая дата истечения:</b> <code>\
{datetime.fromtimestamp(last_user.expire).strftime('%H:%M:%S %Y-%m-%d') if last_user.expire else "Никогда"}</code>
<b>Новая дата истечения:</b> <code>\
{datetime.fromtimestamp(user.expire).strftime('%H:%M:%S %Y-%m-%d') if user.expire else "Никогда"}</code>{tag}"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass
            if list(last_user.inbounds.values())[0] != list(user.inbounds.values())[0]:
                text = f"""\
⚙️ <b>#Изменение_Протоколов #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{user.username}</code>
<b>Прошлые прокси:</b> <code>{", ".join(list(last_user.inbounds.values())[0])}</code>
<b>Новые прокси:</b> <code>{", ".join(list(user.inbounds.values())[0])}</code>{tag}"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass

    elif data == 'add_user':
        if mem_store.get(f'{call.message.chat.id}:username') is None:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            return bot.send_message(
                call.message.chat.id,
                '❌ Обнаружена перезагрузка бота. Пожалуйста, начните заново.',
                reply_markup=BotKeyboard.main_menu()
            )

        if not mem_store.get(f'{call.message.chat.id}:protocols'):
            return bot.answer_callback_query(
                call.id,
                '❌ Не выбраны входящие подключения.',
                show_alert=True
            )

        inbounds: dict[str, list[str]] = {
            k: v for k, v in mem_store.get(f'{call.message.chat.id}:protocols').items() if v}
        original_proxies = {p: ({'flow': TELEGRAM_DEFAULT_VLESS_FLOW} if
                                TELEGRAM_DEFAULT_VLESS_FLOW and p == ProxyTypes.VLESS else {}) for p in inbounds}

        user_status = mem_store.get(f'{call.message.chat.id}:user_status')
        number = mem_store.get(f'{call.message.chat.id}:number', 1)
        if not mem_store.get(f"{call.message.chat.id}:is_bulk", False):
            number = 1

        for i in range(number):
            proxies = copy.deepcopy(original_proxies)
            username: str = mem_store.get(f'{call.message.chat.id}:username')
            if mem_store.get(f"{call.message.chat.id}:is_bulk", False):
                if n := get_number_at_end(username):
                    username = username.replace(n, str(int(n)+i))
                else:
                    username += str(i+1) if i > 0 else ""
            if user_status == 'onhold':
                expire_days = mem_store.get(f'{call.message.chat.id}:expire_date')
                onhold_timeout = mem_store.get(f'{call.message.chat.id}:onhold_timeout')
                if isinstance(expire_days, datetime):
                    expire_days = (expire_days - datetime.now()).days
                new_user = UserCreate(
                    username=username,
                    status='on_hold',
                    on_hold_expire_duration=int(expire_days) * 24 * 60 * 60,
                    on_hold_timeout=onhold_timeout,
                    data_limit=mem_store.get(f'{call.message.chat.id}:data_limit')
                    if mem_store.get(f'{call.message.chat.id}:data_limit') else None,
                    proxies=proxies,
                    inbounds=inbounds)
            else:
                new_user = UserCreate(
                    username=username,
                    status='active',
                    expire=int(mem_store.get(f'{call.message.chat.id}:expire_date').timestamp())
                    if mem_store.get(f'{call.message.chat.id}:expire_date') else None,
                    data_limit=mem_store.get(f'{call.message.chat.id}:data_limit')
                    if mem_store.get(f'{call.message.chat.id}:data_limit') else None,
                    proxies=proxies,
                    inbounds=inbounds)
            for proxy_type in new_user.proxies:
                if not xray.config.inbounds_by_protocol.get(proxy_type):
                    return bot.answer_callback_query(
                        call.id,
                        f'❌ Протокол {proxy_type} отключен на вашем сервере',
                        show_alert=True
                    )
            try:
                with GetDB() as db:
                    db_user = crud.create_user(db, new_user)
                    proxies = db_user.proxies
                    user = UserResponse.model_validate(db_user)
                    xray.operations.add_user(db_user)
                    if mem_store.get(f"{call.message.chat.id}:is_bulk", False):
                        schedule_delete_message(call.message.chat.id, call.message.id)
                        cleanup_messages(call.message.chat.id)
                        bot.send_message(
                            call.message.chat.id,
                            get_user_info_text(db_user),
                            parse_mode="HTML",
                            reply_markup=BotKeyboard.user_menu(
                                user_info={'status': user.status, 'username': user.username})
                        )
                    else:
                        bot.edit_message_text(
                            get_user_info_text(db_user),
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode="HTML",
                            reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))
            except sqlalchemy.exc.IntegrityError:
                db.rollback()
                return bot.answer_callback_query(
                    call.id,
                    '❌ Имя пользователя уже существует.',
                    show_alert=True
                )
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
🆕 <b>#Создан #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{user.username}</code>
<b>Статус:</b> <code>{'Активен' if user_status == 'active' else 'В ожидании'}</code>
<b>Лимит трафика:</b> <code>{readable_size(user.data_limit) if user.data_limit else "Безлимитный"}</code>
"""
                if user_status == 'onhold':
                    text += f"""\
<b>Длительность (on-hold):</b> <code>{new_user.on_hold_expire_duration // (24*60*60)} дней</code>
<b>Таймаут (on-hold):</b> <code>{new_user.on_hold_timeout.strftime("%H:%M:%S %Y-%m-%d") if new_user.on_hold_timeout else "-"}</code>"""
                else:
                    text += f"""<b>Дата истечения:</b> \
<code>{datetime.fromtimestamp(user.expire).strftime("%H:%M:%S %Y-%m-%d") if user.expire else "Никогда"}</code>\n"""
                text += f"""
<b>Протоколы:</b> <code>{"" if not proxies else ", ".join([proxy.type for proxy in proxies])}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass

    elif data in ['delete_expired', 'delete_limited']:
        bot.edit_message_text(
            '⏳ <b>Выполняется...</b>',
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML")
        with GetDB() as db:
            depleted_users = crud.get_users(
                db, status=[UserStatus.limited if data == 'delete_limited' else UserStatus.expired])
            file_name = f'{data[8:]}_users_{int(now.timestamp()*1000)}.txt'
            with open(file_name, 'w') as f:
                f.write('ИМЯ_ПОЛЬЗОВАТЕЛЯ\tИСТЕЧЕНИЕ\tИСПОЛЬЗОВАНИЕ/ЛИМИТ\tСТАТУС\n')
                deleted = 0
                for user in depleted_users:
                    try:
                        crud.remove_user(db, user)
                        xray.operations.remove_user(user)
                        deleted += 1
                        f.write(
                            f'{user.username}\
\t{datetime.fromtimestamp(user.expire) if user.expire else "никогда"}\
\t{readable_size(user.used_traffic) if user.used_traffic else 0}\
/{readable_size(user.data_limit) if user.data_limit else "Безлимитно"}\
\t{status_translations.get(user.status, user.status)}\n')
                    except sqlalchemy.exc.IntegrityError:
                        db.rollback()
            bot.edit_message_text(
                f'✅ <code>{deleted}</code>/<code>{len(depleted_users)}</code> <b>Пользователей удалено</b>',
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=BotKeyboard.main_menu())
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
🗑 <b>#Удаление #{'Истекших' if data[7:] == 'expired' else 'Лимитированных'} #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Количество:</b> <code>{deleted}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
                try:
                    bot.send_document(TELEGRAM_LOGGER_CHANNEL_ID, open(
                        file_name, 'rb'), caption=text, parse_mode='HTML')
                    os.remove(file_name)
                except ApiTelegramException:
                    pass
    elif data == 'add_data':
        schedule_delete_message(
            call.message.chat.id,
            bot.send_message(chat_id, '⏳ <b>Выполняется...</b>', 'HTML').id)
        data_limit = float(call.data.split(":")[2]) * 1024 * 1024 * 1024
        with GetDB() as db:
            users = crud.get_users(db)
            counter = 0
            file_name = f'new_data_limit_users_{int(now.timestamp()*1000)}.txt'
            with open(file_name, 'w') as f:
                f.write('ИМЯ_ПОЛЬЗОВАТЕЛЯ\tИСТЕЧЕНИЕ\tИСПОЛЬЗОВАНИЕ/ЛИМИТ\tСТАТУС\n')
                for user in users:
                    try:
                        if user.data_limit and user.status not in [UserStatus.limited, UserStatus.expired]:
                            user = crud.update_user(db, user, UserModify(data_limit=(user.data_limit + data_limit)))
                            counter += 1
                            f.write(
                                f'{user.username}\
\t{datetime.fromtimestamp(user.expire) if user.expire else "никогда"}\
\t{readable_size(user.used_traffic) if user.used_traffic else 0}\
/{readable_size(user.data_limit) if user.data_limit else "Безлимитно"}\
\t{status_translations.get(user.status, user.status)}\n')
                    except sqlalchemy.exc.IntegrityError:
                        db.rollback()
            cleanup_messages(chat_id)
            bot.send_message(
                chat_id,
                f'✅ <b>{counter}/{len(users)} Пользователям</b> изменен лимит на <code>{"+" if data_limit >
                                                                                       0 else "-"}{readable_size(abs(data_limit))}</code>',
                'HTML',
                reply_markup=BotKeyboard.main_menu())
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
📶 <b>#Изменение_Трафика #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Значение:</b> <code>{"+" if data_limit > 0 else "-"}{readable_size(abs(data_limit))}</code>
<b>Количество:</b> <code>{counter}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
                try:
                    bot.send_document(TELEGRAM_LOGGER_CHANNEL_ID, open(
                        file_name, 'rb'), caption=text, parse_mode='HTML')
                    os.remove(file_name)
                except ApiTelegramException:
                    pass

    elif data == 'add_time':
        schedule_delete_message(
            call.message.chat.id,
            bot.send_message(chat_id, '⏳ <b>Выполняется...</b>', 'HTML').id)
        days = int(call.data.split(":")[2])
        with GetDB() as db:
            users = crud.get_users(db)
            counter = 0
            file_name = f'new_expiry_users_{int(now.timestamp()*1000)}.txt'
            with open(file_name, 'w') as f:
                f.write('ИМЯ_ПОЛЬЗОВАТЕЛЯ\tИСТЕЧЕНИЕ\tИСПОЛЬЗОВАНИЕ/ЛИМИТ\tСТАТУС\n')
                for user in users:
                    try:
                        if user.expire and user.status not in [UserStatus.limited, UserStatus.expired]:
                            user = crud.update_user(
                                db, user,
                                UserModify(
                                    expire=int(
                                        (datetime.fromtimestamp(user.expire) + relativedelta(days=days)).timestamp())))
                            counter += 1
                            f.write(
                                f'{user.username}\
\t{datetime.fromtimestamp(user.expire) if user.expire else "никогда"}\
\t{readable_size(user.used_traffic) if user.used_traffic else 0}\
/{readable_size(user.data_limit) if user.data_limit else "Безлимитно"}\
\t{status_translations.get(user.status, user.status)}\n')
                    except sqlalchemy.exc.IntegrityError:
                        db.rollback()
            cleanup_messages(chat_id)
            bot.send_message(
                chat_id,
                f'✅ <b>{counter}/{len(users)} Пользователям</b> изменен срок действия на {days} дн.',
                'HTML',
                reply_markup=BotKeyboard.main_menu())
            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
📅 <b>#Изменение_Срока #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Значение:</b> <code>{days} дней</code>
<b>Количество:</b> <code>{counter}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
                try:
                    bot.send_document(TELEGRAM_LOGGER_CHANNEL_ID, open(
                        file_name, 'rb'), caption=text, parse_mode='HTML')
                    os.remove(file_name)
                except ApiTelegramException:
                    pass
    elif data in ['inbound_add', 'inbound_remove']:
        bot.edit_message_text(
            '⏳ <b>Выполняется...</b>',
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML")
        inbound = call.data.split(":")[2]
        with GetDB() as db:
            users = crud.get_users(db)
            unsuccessful = 0
            for user in users:
                inbound_tags = [j for i in user.inbounds for j in user.inbounds[i]]
                protocol = xray.config.inbounds_by_tag[inbound]['protocol']
                new_inbounds = user.inbounds
                if data == 'inbound_add':
                    if inbound not in inbound_tags:
                        if protocol in list(new_inbounds.keys()):
                            new_inbounds[protocol].append(inbound)
                        else:
                            new_inbounds[protocol] = [inbound]
                elif data == 'inbound_remove':
                    if inbound in inbound_tags:
                        if len(new_inbounds[protocol]) == 1:
                            del new_inbounds[protocol]
                        else:
                            new_inbounds[protocol].remove(inbound)
                if (data == 'inbound_remove' and inbound in inbound_tags)\
                        or (data == 'inbound_add' and inbound not in inbound_tags):
                    proxies = {p.type.value: p.settings for p in user.proxies}
                    for protocol in xray.config.inbounds_by_protocol:
                        if protocol in new_inbounds and protocol not in user.inbounds:
                            proxies.update({protocol: {'flow': TELEGRAM_DEFAULT_VLESS_FLOW} if
                                            TELEGRAM_DEFAULT_VLESS_FLOW and protocol == ProxyTypes.VLESS else {}})
                        elif protocol in user.inbounds and protocol not in new_inbounds:
                            del proxies[protocol]
                    try:
                        user = crud.update_user(db, user, UserModify(inbounds=new_inbounds, proxies=proxies))
                        if user.status == UserStatus.active:
                            xray.operations.update_user(user)
                    except:
                        db.rollback()
                        unsuccessful += 1

            bot.edit_message_text(
                f'✅ Протокол <code>{inbound}</code> успешно ' + ('добавлен' if data == 'inbound_add' else 'удален') +
                (f'\n Неудачно: <code>{unsuccessful}</code>' if unsuccessful else ''),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=BotKeyboard.main_menu())

            if TELEGRAM_LOGGER_CHANNEL_ID:
                text = f"""\
✏️ <b>#Изменение_Протокола #{"Добавление" if data == 'inbound_add' else "Удаление"} #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Протокол:</b> <code>{inbound}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
                try:
                    bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
                except ApiTelegramException:
                    pass

    elif data == 'revoke_sub':
        username = call.data.split(":")[2]
        with GetDB() as db:
            db_user = crud.get_user(db, username)
            if not db_user:
                return bot.answer_callback_query(call.id, text=f"Пользователь не найден!", show_alert=True)
            db_user = crud.revoke_user_sub(db, db_user)
            user = UserResponse.model_validate(db_user)
            bot.answer_callback_query(call.id, "✅ Подписка успешно сброшена!")
            bot.edit_message_text(
                get_user_info_text(db_user),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))

        if TELEGRAM_LOGGER_CHANNEL_ID:
            text = f"""\
🚫 <b>#Сброс_подписки #Из_Бота</b>
➖➖➖➖➖➖➖➖➖
<b>Имя пользователя:</b> <code>{username}</code>
➖➖➖➖➖➖➖➖➖
<b>От:</b> <a href="tg://user?id={chat_id}">{full_name}</a>"""
            try:
                bot.send_message(TELEGRAM_LOGGER_CHANNEL_ID, text, 'HTML')
            except ApiTelegramException:
                pass


@bot.message_handler(commands=['user'], is_admin=True)
def search_user(message: types.Message):
    args = extract_arguments(message.text)
    if not args:
        return bot.reply_to(
            message,
            "❌ Вы должны указать имена пользователей\n\n"
            "<b>Использование:</b> <code>/user username1 username2</code>",
            parse_mode="HTML"
        )

    usernames = args.split()

    with GetDB() as db:
        for username in usernames:
            db_user = crud.get_user(db, username)
            if not db_user:
                bot.reply_to(message, f'❌ Пользователь «{username}» не найден.')
                continue
            user = UserResponse.model_validate(db_user)
            bot.reply_to(
                message,
                get_user_info_text(db_user),
                parse_mode="html",
                reply_markup=BotKeyboard.user_menu(user_info={'status': user.status, 'username': user.username}))
