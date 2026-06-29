import logging
import time
import asyncio
import json
import io
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError
import database as db
from config import BOT_TOKEN, SUPER_ADMIN_ID

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

call_cooldowns: dict[int, float] = {}
BOT_START_TIME = datetime.now()


# ─────────────────────────── helpers ───────────────────────────

def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID


async def is_chat_owner(bot: Bot, chat_id: int, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status == "creator"
    except TelegramError:
        return False


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except TelegramError:
        return False


async def delete_message_later(bot: Bot, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    asyncio.create_task(delete_message_later(context.bot, chat_id, message_id, delay))


def format_mention(user_id: int, first_name: str, emoji: str) -> str:
    return f'<a href="tg://user?id={user_id}">{emoji}{first_name}</a>'


def fmt_date(iso_str: str | None) -> str:
    if not iso_str:
        return "невідомо"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return iso_str[:10]


# ─────────────────────────── /start ───────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "👋 Привіт! Я бот для скликання учасників групи.\n"
            "Додай мене до групового чату і використовуй команди там.\n\n"
            "📢 Основні команди:\n"
            "!заклик або /call — скликати всіх\n"
            "!заклик+ або /reg — увійти до заклику\n"
            "!заклик- або /unreg — вийти з заклику\n"
            "!смайлик або /emoji — змінити смайлик\n\n"
            "📊 Статистика:\n"
            "!моя стата або /mystats — особиста статистика\n"
            "!стата або /stats — топ-10 за сьогодні\n"
            "!вся стата або /stats_all — топ-10 за весь час"
        )


# ─────────────────────────── CALL ───────────────────────────

async def handle_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    chat_id = chat.id
    user_id = user.id

    settings = db.get_chat_settings(chat_id)
    if settings and settings.get("admins_only"):
        if not await is_admin(context.bot, chat_id, user_id):
            msg = await message.reply_text("🔒 Заклик доступний лише адміністраторам.")
            await schedule_delete(context, chat_id, msg.message_id, 5)
            return

    now = time.time()
    last = call_cooldowns.get(chat_id, 0)
    remaining = 60 - (now - last)
    if remaining > 0 and not is_super_admin(user_id):
        msg = await message.reply_text(f"⏳ Заклик можна надіслати через {int(remaining)} сек.")
        await schedule_delete(context, chat_id, msg.message_id, 5)
        return

    call_cooldowns[chat_id] = now

    text = message.text or ""
    comment = ""
    for trigger in ["!заклик", "!скликати", "/call"]:
        if text.lower().startswith(trigger):
            comment = text[len(trigger):].strip()
            break

    members = db.get_registered_members(chat_id)
    if not members:
        await message.reply_text(
            "😔 Немає зареєстрованих учасників.\n"
            "Виконай /regchat або !перепис населення щоб додати всіх."
        )
        return

    hidden_id = db.get_hidden_user(chat_id)
    mentions = []
    for m in members:
        uid, first_name, emoji, active = m
        if uid == hidden_id:
            continue
        if active:
            mentions.append(format_mention(uid, first_name, emoji))

    if not mentions:
        await message.reply_text("😔 Всі учасники наразі неактивні.")
        return

    header = "📢 <b>Заклик!</b>"
    if comment:
        header += f"\n💬 {comment}"
    full_text = f"{header}\n\n{' '.join(mentions)}"

    sent = await message.reply_text(full_text, parse_mode="HTML")

    delete_after = settings.get("delete_after", 0) if settings else 0
    if delete_after > 0:
        await schedule_delete(context, chat_id, sent.message_id, delete_after)


# ─────────────────────────── /fix_call ───────────────────────────

async def fix_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if not await is_chat_owner(context.bot, chat.id, user.id):
        msg = await message.reply_text("🔒 Доступно лише власнику чату.")
        await schedule_delete(context, chat.id, msg.message_id, 5)
        return

    existing = db.get_registered_members(chat.id)
    await message.reply_text(
        "🔄 Перевіряю заклик...\n"
        "Примітка: Telegram не дозволяє отримати повний список учасників через Bot API.\n"
        "Для повного перепису використовуй !перепис населення — "
        "учасники мають написати щось у чат після цієї команди.\n\n"
        f"✅ Зараз у закликі: {len(existing)} учасників."
    )


# ─────────────────────────── REG / UNREG ───────────────────────────

async def reg(update: Update, context: ContextTypes.DEFAULT_TYPE, all_chats: bool = False):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private" and not all_chats:
        await message.reply_text("Ця команда доступна лише в групах.")
        return

    if all_chats:
        count = db.set_active_all_chats(user.id, True)
        await message.reply_text(f"🤗 Ти повернувся до заклику в усіх {count} чатах!")
    else:
        db.ensure_member(chat.id, user.id, user.first_name)
        db.set_active(chat.id, user.id, True)
        emoji = db.get_emoji(chat.id, user.id) or "👤"
        await message.reply_text(f"✅ Ти знову в закликі! {emoji}")


async def unreg(update: Update, context: ContextTypes.DEFAULT_TYPE, all_chats: bool = False):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if all_chats:
        count = db.set_active_all_chats(user.id, False)
        await message.reply_text(f"😡 Ти вийшов із заклику в усіх {count} чатах!")
    else:
        db.set_active(chat.id, user.id, False)
        await message.reply_text("❌ Ти вийшов із заклику в цьому чаті.")


# ─────────────────────────── CHECK STATUS ───────────────────────────

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    target_user = user
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user

    member = db.get_member(chat.id, target_user.id)
    if not member:
        await message.reply_text(f"🤷 {target_user.first_name} не зареєстрований у закликі.")
        return

    _, first_name, emoji, active = member
    status = "✅ активний" if active else "❌ неактивний"
    await message.reply_text(
        f"{'Ти' if target_user.id == user.id else target_user.first_name}: {emoji} | {status}"
    )


# ─────────────────────────── SET EMOJI ───────────────────────────

async def set_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    text = message.text or ""
    new_emoji = None
    for trigger in ["!смайлик", "/emoji", "/set_emoji"]:
        if trigger in text:
            rest = text[text.index(trigger) + len(trigger):].strip()
            if rest:
                new_emoji = rest.split()[0]
            break

    if context.args and not new_emoji:
        new_emoji = context.args[0]

    if not new_emoji:
        await message.reply_text("😎 Вкажи новий смайлик після команди.\nПриклад: !смайлик 🔥")
        return

    db.ensure_member(chat.id, user.id, user.first_name)
    db.set_emoji(chat.id, user.id, new_emoji)
    await message.reply_text(f"😎 Твій смайлик змінено на {new_emoji}!")


# ─────────────────────────── REGCHAT ───────────────────────────

async def regchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if not await is_chat_owner(context.bot, chat.id, user.id):
        msg = await message.reply_text("🦅 Доступно лише власнику чату.")
        await schedule_delete(context, chat.id, msg.message_id, 5)
        return

    db.ensure_member(chat.id, user.id, user.first_name)
    db.set_active(chat.id, user.id, True)
    count = len(db.get_registered_members(chat.id))

    await message.reply_text(
        "🦅 <b>Перепис населення запущено!</b>\n\n"
        "⚠️ Telegram не дозволяє ботам отримати повний список учасників.\n"
        "Учасники будуть автоматично додані, коли напишуть будь-що у чат.\n\n"
        f"📊 Зараз у закликі: <b>{count}</b> учасників.",
        parse_mode="HTML"
    )


# ─────────────────────────── STATS ───────────────────────────

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await message.reply_text("Ця команда доступна лише в групах.")
        return

    s = db.get_user_stats(chat.id, user.id)

    text = (
        f"📊 <b>Статистика {user.first_name}</b>\n"
        f"- день: {s['day']}\n"
        f"- тиждень: {s['week']}\n"
        f"- місяць: {s['month']}\n"
        f"- рік: {s['year']}\n"
        f"- весь час: {s['all']}\n"
        f"📅 Перше повідомлення: {fmt_date(s['first'])}"
    )
    await message.reply_text(text, parse_mode="HTML")


async def stats_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top-10 for today."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type == "private":
        await message.reply_text("Ця команда доступна лише в групах.")
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        chat_title = chat_obj.title or "чат"
    except TelegramError:
        chat_title = "чат"

    top = db.get_today_top_users(chat.id, 10)
    today_total = db.get_today_messages(chat.id)

    if not top:
        await message.reply_text("📊 За сьогодні ще немає повідомлень.")
        return

    lines = []
    for i, (uid, fname, count) in enumerate(top, 1):
        lines.append(f"{i}) {fname} — {count} повідомлень")

    text = (
        f"📊 <b>Статистика «{chat_title}» за сьогодні</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<b>Кількість за сьогодні: {today_total}</b>"
    )
    await message.reply_text(text, parse_mode="HTML")


async def stats_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top-10 for the last 7 days."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type == "private":
        await message.reply_text("Ця команда доступна лише в групах.")
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        chat_title = chat_obj.title or "чат"
    except TelegramError:
        chat_title = "чат"

    top = db.get_week_top_users(chat.id, 10)
    week_total = db.get_week_messages(chat.id)

    if not top:
        await message.reply_text("📊 За цей тиждень ще немає повідомлень.")
        return

    lines = []
    for i, (uid, fname, count) in enumerate(top, 1):
        lines.append(f"{i}) {fname} — {count} повідомлень")

    text = (
        f"📊 <b>Статистика «{chat_title}» за тиждень</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<b>Кількість за тиждень: {week_total}</b>"
    )
    await message.reply_text(text, parse_mode="HTML")


async def stats_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top-10 all time."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type == "private":
        await message.reply_text("Ця команда доступна лише в групах.")
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        chat_title = chat_obj.title or "чат"
    except TelegramError:
        chat_title = "чат"

    top = db.get_top_users(chat.id, 10)
    total = db.get_total_messages(chat.id)

    if not top:
        await message.reply_text(
            "📊 Поки немає даних.\n"
            "Завантаж експорт чату через /import_chat (у приватний чат зі мною)."
        )
        return

    lines = []
    for i, (uid, fname, count) in enumerate(top, 1):
        lines.append(f"{i}) {fname} — {count} повідомлень")

    text = (
        f"📊 <b>Статистика «{chat_title}» за весь час</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<b>Загальна кількість: {total}</b>"
    )
    await message.reply_text(text, parse_mode="HTML")


# ─────────────────────────── IMPORT CHAT EXPORT ───────────────────────────

async def import_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /import_chat in private chat with a JSON file attached.
    Usage:
      1. In Telegram Desktop: Export chat history as JSON (without media).
      2. Send the result.json file to the bot in private chat.
      3. Reply to the file with: /import_chat <chat_id>
         OR forward a message from the target group first so the bot knows the chat_id.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    # Only in private chat
    if chat.type != "private":
        await message.reply_text("📥 Команду /import_chat потрібно використовувати в приватному чаті зі мною.")
        return

    # Must have a document attached or reply to one
    doc = None
    if message.document:
        doc = message.document
    elif message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document

    if not doc:
        await message.reply_text(
            "📥 <b>Як імпортувати статистику з Telegram Desktop:</b>\n\n"
            "1. Відкрий потрібний чат у Telegram Desktop\n"
            "2. Натисни ⋮ → «Експорт історії чату»\n"
            "3. Формат: <b>JSON</b>, без медіафайлів\n"
            "4. Відправ мені файл <code>result.json</code> у цей чат\n"
            "5. У підписі до файлу або у відповіді напиши:\n"
            "   <code>/import_chat -1001234567890</code>\n"
            "   (ID групи — можна дізнатись у @username_to_id_bot)\n\n"
            "⚡ Бот підтягне всіх учасників і їх повідомлення до бази.",
            parse_mode="HTML"
        )
        return

    # Get target chat_id from args or caption
    target_chat_id: int | None = None

    if context.args:
        try:
            target_chat_id = int(context.args[0])
        except (ValueError, IndexError):
            pass

    if target_chat_id is None and message.caption:
        parts = message.caption.split()
        for p in parts:
            try:
                target_chat_id = int(p)
                break
            except ValueError:
                pass

    if target_chat_id is None:
        await message.reply_text(
            "❌ Вкажи ID чату після команди.\n"
            "Приклад: надішли файл з підписом <code>/import_chat -1001234567890</code>",
            parse_mode="HTML"
        )
        return

    # Download and parse the file
    processing_msg = await message.reply_text("⏳ Завантажую та обробляю файл...")

    try:
        file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        export_data = json.loads(buf.read().decode("utf-8"))
    except Exception as e:
        await processing_msg.edit_text(f"❌ Помилка читання файлу: {e}")
        return

    if "messages" not in export_data:
        await processing_msg.edit_text(
            "❌ Невірний формат файлу. Переконайся, що це result.json з Telegram Desktop (формат JSON)."
        )
        return

    result = db.import_from_telegram_export(target_chat_id, export_data)

    await processing_msg.edit_text(
        f"✅ <b>Імпорт завершено!</b>\n\n"
        f"💬 Повідомлень імпортовано: <b>{result['messages']}</b>\n"
        f"👥 Унікальних користувачів: <b>{result['users']}</b>\n\n"
        f"Тепер у чаті доступна команда !вся стата",
        parse_mode="HTML"
    )


# ─────────────────────────── SETTINGS ───────────────────────────

async def call_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if not await is_chat_owner(context.bot, chat.id, user.id):
        msg = await message.reply_text("🔒 Доступно лише власнику чату.")
        await schedule_delete(context, chat.id, msg.message_id, 5)
        return

    if not context.args:
        settings = db.get_chat_settings(chat.id)
        current = settings.get("delete_after", 0) if settings else 0
        await message.reply_text(
            f"🧨 Поточне значення: {current} сек.\n"
            "Використання: /call_del <секунди> (0 — не видаляти)"
        )
        return

    try:
        seconds = int(context.args[0])
        if seconds < 0:
            raise ValueError
    except ValueError:
        await message.reply_text("❌ Вкажи ціле невід'ємне число секунд.")
        return

    db.set_chat_setting(chat.id, "delete_after", seconds)
    if seconds == 0:
        await message.reply_text("🧨 Автовидалення закликів вимкнено.")
    else:
        await message.reply_text(f"🧨 Заклики будуть видалятись через {seconds} сек.")


async def call_admins_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if not await is_chat_owner(context.bot, chat.id, user.id):
        msg = await message.reply_text("🔒 Доступно лише власнику чату.")
        await schedule_delete(context, chat.id, msg.message_id, 5)
        return

    settings = db.get_chat_settings(chat.id)
    current = settings.get("admins_only", False) if settings else False
    new_val = not current
    db.set_chat_setting(chat.id, "admins_only", new_val)

    if new_val:
        await message.reply_text("👮 Заклик тепер доступний лише адміністраторам.")
    else:
        await message.reply_text("👮 Заклик знову доступний всім учасникам.")


# ─────────────────────────── SUPER ADMIN ───────────────────────────

async def botsysstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_super_admin(user.id):
        return

    uptime = datetime.now() - BOT_START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    total_chats = db.count_chats()
    total_members = db.count_members()
    total_active = db.count_active_members()
    total_logged = db.count_total_logged_messages()

    stats_text = (
        f"🤖 <b>Системна статистика бота</b>\n\n"
        f"⏱ Аптайм: {hours}г {minutes}хв {seconds}с\n"
        f"🗓 Запущено: {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"📊 <b>База даних:</b>\n"
        f"💬 Чатів: {total_chats}\n"
        f"👥 Учасників (загалом): {total_members}\n"
        f"✅ Активних у закликах: {total_active}\n"
        f"❌ Неактивних: {total_members - total_active}\n"
        f"📝 Повідомлень у лозі: {total_logged}\n\n"
        f"🔥 Версія: 1.1.0"
    )
    await update.effective_message.reply_text(stats_text, parse_mode="HTML")


async def hide_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not is_super_admin(user.id):
        return

    chat_id = chat.id if chat.type != "private" else None
    current = db.get_hidden_user(chat_id)
    db.set_hidden_user(chat_id, None if current == user.id else user.id)

    sent = await message.reply_text("✅")
    asyncio.create_task(delete_message_later(context.bot, message.chat_id, sent.message_id, 1))
    asyncio.create_task(delete_message_later(context.bot, message.chat_id, message.message_id, 1))


# ─────────────────────────── TEXT ROUTER ───────────────────────────

CALL_TRIGGERS      = ["!заклик", "!скликати", "/call"]
UNREG_TRIGGERS     = ["!заклик-", "заклик-", "!call-", "!оф", "!піти по хліб", "!тиха година", "!я сплю", "/unreg"]
UNREG_ALL_TRIGGERS = ["!заклик--", "/unreg_all"]
REG_TRIGGERS       = ["!заклик+", "заклик+", "!call+", "!прокинувся", "!прокинулась", "!з хлібом", "/reg"]
REG_ALL_TRIGGERS   = ["!заклик++", "!call++", "/reg_all"]
CHECK_TRIGGERS     = ["!заклик?"]
EMOJI_TRIGGERS     = ["!смайлик", "/emoji", "/set_emoji"]
CENSUS_TRIGGERS    = ["!перепис населення", "/regchat"]
MYSTATS_TRIGGERS   = ["!моя стата", "/mystats"]
STATS_TRIGGERS     = ["!стата", "/stats"]
STATS_WEEK_TRIGGERS= ["!стата тиждень", "!тижнева стата", "/stats_week"]
STATS_ALL_TRIGGERS = ["!вся стата", "/stats_all"]
HIDE_TRIGGERS      = ["!не згадувати мене"]
SYS_STATS_TRIGGERS = ["!botsysstats"]


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    text_lower = text.lower()
    user = update.effective_user
    chat = update.effective_chat

    # Log every message for stats (groups only)
    if chat.type != "private" and user:
        db.ensure_member(chat.id, user.id, user.first_name)
        db.log_message(chat.id, user.id, user.first_name)

    if chat.type == "private":
        return

    # Super admin secret commands
    if is_super_admin(user.id):
        if any(text_lower.startswith(t.lower()) for t in HIDE_TRIGGERS):
            await hide_me(update, context)
            return
        if any(text_lower.startswith(t.lower()) for t in SYS_STATS_TRIGGERS):
            await botsysstats(update, context)
            return

    # Order matters: longer/more-specific triggers first
    if any(text_lower.startswith(t.lower()) for t in UNREG_ALL_TRIGGERS):
        await unreg(update, context, all_chats=True); return

    if any(text_lower.startswith(t.lower()) for t in REG_ALL_TRIGGERS):
        await reg(update, context, all_chats=True); return

    if any(text_lower.startswith(t.lower()) for t in UNREG_TRIGGERS):
        await unreg(update, context); return

    if any(text_lower.startswith(t.lower()) for t in REG_TRIGGERS):
        await reg(update, context); return

    if any(text_lower.startswith(t.lower()) for t in CHECK_TRIGGERS):
        await check_status(update, context); return

    if any(text_lower.startswith(t.lower()) for t in EMOJI_TRIGGERS):
        await set_emoji(update, context); return

    if any(text_lower.startswith(t.lower()) for t in CENSUS_TRIGGERS):
        await regchat(update, context); return

    if any(text_lower.startswith(t.lower()) for t in MYSTATS_TRIGGERS):
        await my_stats(update, context); return

    # !вся стата must come before !стата, !стата тиждень before !стата
    if any(text_lower.startswith(t.lower()) for t in STATS_ALL_TRIGGERS):
        await stats_all(update, context); return

    if any(text_lower.startswith(t.lower()) for t in STATS_WEEK_TRIGGERS):
        await stats_week(update, context); return

    if any(text_lower.startswith(t.lower()) for t in STATS_TRIGGERS):
        await stats_today(update, context); return

    # Call — guard against !заклик+/-/?
    for trigger in CALL_TRIGGERS:
        if text_lower.startswith(trigger.lower()):
            rest = text[len(trigger):]
            if rest and rest[0] in ("+", "-", "?"):
                break
            await handle_call(update, context)
            return


# ─────────────────────────── MAIN ───────────────────────────

def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Slash commands — groups
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("call", handle_call))
    app.add_handler(CommandHandler("fix_call", fix_call))
    app.add_handler(CommandHandler("reg", lambda u, c: reg(u, c, False)))
    app.add_handler(CommandHandler("reg_all", lambda u, c: reg(u, c, True)))
    app.add_handler(CommandHandler("unreg", lambda u, c: unreg(u, c, False)))
    app.add_handler(CommandHandler("unreg_all", lambda u, c: unreg(u, c, True)))
    app.add_handler(CommandHandler("emoji", set_emoji))
    app.add_handler(CommandHandler("set_emoji", set_emoji))
    app.add_handler(CommandHandler("regchat", regchat))
    app.add_handler(CommandHandler("call_del", call_del))
    app.add_handler(CommandHandler("call_admins_only", call_admins_only))

    # Stats commands
    app.add_handler(CommandHandler("mystats", my_stats))
    app.add_handler(CommandHandler("stats", stats_today))
    app.add_handler(CommandHandler("stats_week", stats_week))
    app.add_handler(CommandHandler("stats_all", stats_all))

    # Import — private chat only
    app.add_handler(CommandHandler("import_chat", import_chat))
    app.add_handler(MessageHandler(
        filters.Document.MimeType("application/json") & filters.ChatType.PRIVATE,
        import_chat
    ))

    # Text router — groups
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS,
        text_router
    ))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
