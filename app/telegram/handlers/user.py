from datetime import datetime
from app.db import GetDB, crud
from app.models.user import UserResponse
from app.telegram import bot
from telebot.custom_filters import ChatFilter
from telebot.util import extract_arguments

from app.utils.system import readable_size

bot.add_custom_filter(ChatFilter())


@bot.message_handler(commands=['usage'])
def usage_command(message):
    username = extract_arguments(message.text)
    if not username:
        return bot.reply_to(message, 'Использование: `/usage <имя_пользователя>`', parse_mode='MarkdownV2')

    with GetDB() as db:
        dbuser = crud.get_user(db, username)

        if not dbuser:
            return bot.reply_to(message, "Пользователь с таким именем не найден")
        user = UserResponse.model_validate(dbuser)

        statuses = {
            'active': '✅',
            'expired': '🕰',
            'limited': '📵',
            'disabled': '❌',
            'on_hold': '🔌'}

        status_translations = {
            'active': 'Активен',
            'expired': 'Истек',
            'limited': 'Лимит исчерпан',
            'disabled': 'Отключен',
            'on_hold': 'В ожидании'}

        text = f'''\
┌─{statuses[user.status]} <b>Статус:</b> <code>{status_translations.get(user.status, user.status.title())}</code>
│          └─<b>Имя пользователя:</b> <code>{user.username}</code>
│
├─🔋 <b>Лимит данных:</b> <code>{readable_size(user.data_limit) if user.data_limit else 'Безлимитный'}</code>
│          └─<b>Использовано:</b> <code>{readable_size(user.used_traffic) if user.used_traffic else "-"}</code>
│
└─📅 <b>Дата истечения:</b> <code>{datetime.fromtimestamp(user.expire).date() if user.expire else 'Никогда'}</code>
            └─<b>Осталось дней:</b> <code>{(datetime.fromtimestamp(user.expire or 0) - datetime.now()).days if user.expire else '-'}</code>'''

    return bot.reply_to(message, text, parse_mode='HTML')
