from datetime import datetime as dt
from itertools import islice
from typing import Dict, List, Literal

from telebot import types  # noqa

from app import xray
from app.utils.system import readable_size


def chunk_dict(data: dict, size: int = 2):
    it = iter(data)
    for i in range(0, len(data), size):
        yield {k: data[k] for k in islice(it, size)}


class BotKeyboard:

    @staticmethod
    def main_menu():
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(text='🔁 Инфо о системе', callback_data='system'),
            types.InlineKeyboardButton(text='♻️ Перезапуск Xray', callback_data='restart'))
        keyboard.add(
            types.InlineKeyboardButton(text='👥 Пользователи', callback_data='users:1'),
            types.InlineKeyboardButton(text='✏️ Редактировать всех', callback_data='edit_all'))
        keyboard.add(
            types.InlineKeyboardButton(text='➕ Создать по шаблону', callback_data='template_add_user'))
        keyboard.add(
            types.InlineKeyboardButton(text='➕ Массовое создание', callback_data='template_add_bulk_user'))
        keyboard.add(
            types.InlineKeyboardButton(text='➕ Создать пользователя', callback_data='add_user'))
        keyboard.add(
            types.InlineKeyboardButton(text='➕ Массовое создание пользователей', callback_data='add_bulk_user'))
        return keyboard

    @staticmethod
    def edit_all_menu():
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(text='🗑 Удалить истекших', callback_data='delete_expired'),
            types.InlineKeyboardButton(text='🪫 Удалить с лимитом', callback_data='delete_limited'))
        keyboard.add(
            types.InlineKeyboardButton(text='🔋 Данные (➕|➖)', callback_data='add_data'),
            types.InlineKeyboardButton(text='📅 Время (➕|➖)', callback_data='add_time'))
        keyboard.add(
            types.InlineKeyboardButton(text='➕ Добавить протокол', callback_data='inbound_add'),
            types.InlineKeyboardButton(text='➖ Удалить протокол', callback_data='inbound_remove'))
        keyboard.add(types.InlineKeyboardButton(text='🔙 Назад', callback_data='cancel'))
        return keyboard

    @staticmethod
    def inbounds_menu(action, inbounds):
        keyboard = types.InlineKeyboardMarkup()
        for inbound in inbounds:
            keyboard.add(types.InlineKeyboardButton(text=inbound, callback_data=f'confirm_{action}:{inbound}'))
        keyboard.add(types.InlineKeyboardButton(text='🔙 Назад', callback_data='cancel'))
        return keyboard

    @staticmethod
    def templates_menu(templates: Dict[str, int], username: str = None):
        keyboard = types.InlineKeyboardMarkup()

        for chunk in chunk_dict(templates):
            row = []
            for name, _id in chunk.items():
                row.append(
                    types.InlineKeyboardButton(
                        text=name,
                        callback_data=f'template_charge:{_id}:{username}' if username else f"template_add_user:{_id}"))
            keyboard.add(*row)

        keyboard.add(
            types.InlineKeyboardButton(
                text='🔙 Назад',
                callback_data=f'user:{username}' if username else 'cancel'))
        return keyboard

    @staticmethod
    def random_username(template_id: str = ''):
        keyboard = types.InlineKeyboardMarkup()

        keyboard.add(types.InlineKeyboardButton(
            text='🔡 Случайное имя',
            callback_data=f'random:{template_id}'))
        keyboard.add(types.InlineKeyboardButton(
            text='🔙 Отмена',
            callback_data='cancel'))
        return keyboard

    @staticmethod
    def user_menu(user_info, with_back: bool = True, page: int = 1):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                text='❌ Отключить' if user_info['status'] == 'active' else '✅ Активировать',
                callback_data=f"{'suspend' if user_info['status'] == 'active' else 'activate'}:{user_info['username']}"
            ),
            types.InlineKeyboardButton(
                text='🗑 Удалить',
                callback_data=f"delete:{user_info['username']}"
            ),
        )
        keyboard.add(
            types.InlineKeyboardButton(
                text='🚫 Сбросить подписку',
                callback_data=f"revoke_sub:{user_info['username']}"),
            types.InlineKeyboardButton(
                text='✏️ Ред.',
                callback_data=f"edit:{user_info['username']}"))
        keyboard.add(
            types.InlineKeyboardButton(
                text='📝 Изменить заметку',
                callback_data=f"edit_note:{user_info['username']}"),
            types.InlineKeyboardButton(
                text='📡 Ссылки',
                callback_data=f"links:{user_info['username']}"))
        keyboard.add(
            types.InlineKeyboardButton(
                text='🔁 Сбросить трафик',
                callback_data=f"reset_usage:{user_info['username']}"
            ),
            types.InlineKeyboardButton(
                text='🔋 Пополнить',
                callback_data=f"charge:{user_info['username']}"
            )
        )
        if with_back:
            keyboard.add(
                types.InlineKeyboardButton(
                    text='🔙 Назад',
                    callback_data=f'users:{page}'
                )
            )
        return keyboard

    @staticmethod
    def user_status_select():
        keyboard = types.InlineKeyboardMarkup()

        keyboard.add(
            types.InlineKeyboardButton(
                text="🟢 Активен",
                callback_data='status:active'
            ),
            types.InlineKeyboardButton(
                text="🟣 Ожидание",
                callback_data='status:onhold'
            )
        )
        keyboard.add(
            types.InlineKeyboardButton(
                text='🔙 Назад',
                callback_data='cancel'
            )
        )
        return keyboard

    @staticmethod
    def show_links(username: str):
        keyboard = types.InlineKeyboardMarkup()

        keyboard.add(
            types.InlineKeyboardButton(
                text="🖼 QR-код конфигов",
                callback_data=f'genqr:configs:{username}'
            ),
            types.InlineKeyboardButton(
                text="🚀 QR-код подписки",
                callback_data=f'genqr:sub:{username}'
            )
        )
        keyboard.add(
            types.InlineKeyboardButton(
                text='🔙 Назад',
                callback_data=f'user:{username}'
            )
        )
        return keyboard

    @staticmethod
    def subscription_page(sub_url: str):
        keyboard = types.InlineKeyboardMarkup()
        if sub_url[:4] == 'http':
            keyboard.add(types.InlineKeyboardButton(
                text='🚀 Страница подписки',
                url=sub_url))
        return keyboard

    @staticmethod
    def confirm_action(action: str, username: str = None):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                text='Да',
                callback_data=f"confirm:{action}:{username}"
            ),
            types.InlineKeyboardButton(
                text='Нет',
                callback_data=f"cancel"
            )
        )
        return keyboard

    @staticmethod
    def charge_add_or_reset(username: str, template_id: int):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                text='🔰 Добавить к текущему',
                callback_data=f"confirm:charge_add:{username}:{template_id}"
            ),
            types.InlineKeyboardButton(
                text='♻️ Сбросить',
                callback_data=f"confirm:charge_reset:{username}:{template_id}"
            ))
        keyboard.add(
            types.InlineKeyboardButton(
                text="Отмена",
                callback_data=f'user:{username}'
            )
        )
        return keyboard

    @staticmethod
    def inline_cancel_action(callback_data: str = "cancel"):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                text="🔙 Отмена",
                callback_data=callback_data
            )
        )
        return keyboard

    @staticmethod
    def user_list(users: list, page: int, total_pages: int):
        keyboard = types.InlineKeyboardMarkup()
        if len(users) >= 2:
            users = [p for p in users]
            users = [users[i:i + 2] for i in range(0, len(users), 2)]
        else:
            users = [users]
        for user in users:
            row = []
            for p in user:
                status = {
                    'active': '✅',
                    'expired': '🕰',
                    'limited': '📵',
                    'disabled': '❌',
                    'on_hold': '🔌'
                }
                row.append(types.InlineKeyboardButton(
                    text=f"{p.username} ({status[p.status]})",
                    callback_data=f'user:{p.username}:{page}'
                ))
            keyboard.row(*row)
        # if there is more than one page
        if total_pages > 1:
            if page > 1:
                keyboard.add(
                    types.InlineKeyboardButton(
                        text="⬅️ Предыдущая",
                        callback_data=f'users:{page - 1}'
                    )
                )
            if page < total_pages:
                keyboard.add(
                    types.InlineKeyboardButton(
                        text="➡️ Следующая",
                        callback_data=f'users:{page + 1}'
                    )
                )
        keyboard.add(
            types.InlineKeyboardButton(
                text='🔙 Назад',
                callback_data='cancel'
            )
        )
        return keyboard

    @staticmethod
    def select_protocols(
            selected_protocols: Dict[str, List[str]],
            action: Literal["edit", "create", "create_from_template"],
            username: str = None,
            data_limit: float = None,
            expire_date: dt = None,
            expire_on_hold_duration: int = None,
            expire_on_hold_timeout: dt = None
    ):
        keyboard = types.InlineKeyboardMarkup()

        if action == "edit":
            keyboard.add(types.InlineKeyboardButton(text="⚠️ Лимит данных:", callback_data=f"help_edit"))
            keyboard.add(
                types.InlineKeyboardButton(
                    text=f"{readable_size(data_limit) if data_limit else 'Безлимитный'}",
                    callback_data=f"help_edit"
                ),
                types.InlineKeyboardButton(text="✏️ Ред.", callback_data=f"edit_user:{username}:data"))
            if expire_on_hold_duration:
                keyboard.add(types.InlineKeyboardButton(text="⏳ Длительность:", callback_data=f"edit_user:{username}:expire"))
                keyboard.add(
                    types.InlineKeyboardButton(
                        text=f"{int(expire_on_hold_duration / 24 / 60 / 60)} дней",
                        callback_data=f"edit_user:{username}:expire"
                    ),
                    types.InlineKeyboardButton(text="✏️ Ред.", callback_data=f"edit_user:{username}:expire"))

                keyboard.add(
                    types.InlineKeyboardButton(
                        text="🌀 Авто-включение в:",
                        callback_data=f"edit_user:{username}:expire_on_hold_timeout"
                    )
                )
                keyboard.add(
                    types.InlineKeyboardButton(
                        text=f"{expire_on_hold_timeout.strftime('%Y-%m-%d') if expire_on_hold_timeout else 'Никогда'}",
                        callback_data=f"edit_user:{username}:expire_on_hold_timeout"),
                    types.InlineKeyboardButton(
                        text="✏️ Ред.",
                        callback_data=f"edit_user:{username}:expire_on_hold_timeout"
                    )
                )
            else:
                keyboard.add(types.InlineKeyboardButton(text="📅 Дата истечения:", callback_data=f"help_edit"))
                keyboard.add(
                    types.InlineKeyboardButton(
                        text=f"{expire_date.strftime('%Y-%m-%d') if expire_date else 'Никогда'}",
                        callback_data=f"help_edit"
                    ),
                    types.InlineKeyboardButton(text="✏️ Ред.", callback_data=f"edit_user:{username}:expire"))

        if action != 'create_from_template':
            for protocol, inbounds in xray.config.inbounds_by_protocol.items():
                keyboard.add(
                    types.InlineKeyboardButton(
                        text=f"🌐 {protocol.upper()} {'✅' if protocol in selected_protocols else '❌'}",
                        callback_data=f'select_protocol:{protocol}:{action}'
                    )
                )
                if protocol in selected_protocols:
                    for inbound in inbounds:
                        keyboard.add(
                            types.InlineKeyboardButton(
                                text=f"«{inbound['tag']}» {'✅' if inbound['tag'] in selected_protocols[protocol] else '❌'}",
                                callback_data=f'select_inbound:{inbound["tag"]}:{action}'
                            )
                        )

        keyboard.add(
            types.InlineKeyboardButton(
                text='Готово',
                callback_data='confirm:edit_user' if action == "edit" else 'confirm:add_user'
            )
        )
        keyboard.add(
            types.InlineKeyboardButton(
                text='Отмена',
                callback_data=f'user:{username}' if action == "edit" else 'cancel'
            )
        )

        return keyboard
