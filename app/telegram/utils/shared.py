import re
from datetime import datetime as dt

from dateutil.relativedelta import relativedelta

from app.models.user import User, UserResponse, UserStatus
from app.models.user_template import UserTemplate
from app.utils.system import readable_size

statuses = {
    UserStatus.active: "✅",
    UserStatus.expired: "🕰",
    UserStatus.limited: "🪫",
    UserStatus.disabled: "❌",
    UserStatus.on_hold: "🔌",
}


status_translations = {
    UserStatus.active: "Активен",
    UserStatus.expired: "Истек",
    UserStatus.limited: "Лимит исчерпан",
    UserStatus.disabled: "Отключен",
    UserStatus.on_hold: "В ожидании",
}


def time_to_string(time: dt):
    now = dt.now()
    if time < now:
        delta = now - time
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"около <code>{days}</code> дней назад"
        elif hours > 0:
            return f"около <code>{hours}</code> часов назад"
        elif minutes > 0:
            return f"около <code>{minutes}</code> минут назад"
        else:
            return "только что"
    else:
        delta = time - now
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"через <code>{days}</code> дней"
        elif hours > 0:
            return f"через <code>{hours}</code> часов"
        elif minutes > 0:
            return f"через <code>{minutes}</code> минут"
        else:
            return "очень скоро"


def get_user_info_text(db_user: User) -> str:
    user: UserResponse = UserResponse.model_validate(db_user)
    data_limit = readable_size(user.data_limit) if user.data_limit else "Безлимитный"
    used_traffic = readable_size(user.used_traffic) if user.used_traffic else "-"
    data_left = readable_size(user.data_limit - user.used_traffic) if user.data_limit else "-"
    on_hold_timeout = user.on_hold_timeout.strftime("%Y-%m-%d") if user.on_hold_timeout else "-"
    on_hold_duration = user.on_hold_expire_duration // (24*60*60) if user.on_hold_expire_duration else None
    expiry_date = dt.fromtimestamp(user.expire).date() if user.expire else "Никогда"
    time_left = time_to_string(dt.fromtimestamp(user.expire)) if user.expire else "-"
    online_at = time_to_string(user.online_at) if user.online_at else "-"
    sub_updated_at = time_to_string(user.sub_updated_at) if user.sub_updated_at else "-"
    if user.status == UserStatus.on_hold:
        expiry_text = f"⏰ <b>Длительность ожидания:</b> <code>{on_hold_duration} дней</code> (автозапуск <code>{
            on_hold_timeout}</code>)"
    else:
        expiry_text = f"📅 <b>Дата истечения:</b> <code>{expiry_date}</code> ({time_left})"
    return f"""\
{statuses[user.status]} <b>Статус:</b> <code>{status_translations.get(user.status, user.status.title())}</code>

🔤 <b>Имя пользователя:</b> <code>{user.username}</code>

🔋 <b>Лимит данных:</b> <code>{data_limit}</code>
📶 <b>Использовано:</b> <code>{used_traffic}</code> (<code>{data_left}</code> осталось)
{expiry_text}

🔌 <b>В сети:</b> {online_at}
🔄 <b>Подписка обновлена:</b> {sub_updated_at}
📱 <b>Последний агент подписки:</b> <blockquote>{user.sub_last_user_agent or "-"}</blockquote>

📝 <b>Заметка:</b> <blockquote expandable>{user.note or "пусто"}</blockquote>
👨‍💻 <b>Админ:</b> <code>{db_user.admin.username if db_user.admin else "-"}</code>
🚀 <b><a href="{user.subscription_url}">Подписка</a>:</b> <code>{user.subscription_url}</code>"""


def get_template_info_text(template: UserTemplate):
    protocols = ""
    for p, inbounds in template.inbounds.items():
        protocols += f"\n├─ <b>{p.upper()}</b>\n"
        protocols += "├───" + ", ".join([f"<code>{i}</code>" for i in inbounds])
    data_limit = readable_size(template.data_limit) if template.data_limit else "Безлимитный"
    expire = ((dt.now() + relativedelta(seconds=template.expire_duration))
              .strftime("%Y-%m-%d")) if template.expire_duration else "Никогда"
    text = f"""
📊 Инфо о шаблоне:
ID: <b>{template.id}</b>
Лимит данных: <b>{data_limit}</b>
Дата истечения: <b>{expire}</b>
Префикс имени: <b>{template.username_prefix if template.username_prefix else "-"}</b>
Суффикс имени: <b>{template.username_suffix if template.username_suffix else "-"}</b>
Протоколы: {protocols}"""
    return text


def get_number_at_end(username: str):
    n = re.search(r'(\d+)$', username)
    if n:
        return n.group(1)
