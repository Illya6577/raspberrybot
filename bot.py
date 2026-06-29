import logging
import time
import asyncio
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ChatMemberHandler
)
from telegram.error import TelegramError
import database as db
from config import BOT_TOKEN, SUPER_ADMIN_ID

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Cooldown tracker: {chat_id: last_call_time}
call_cooldowns: dict[int, float] = {}

# Bot start time for stats
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
    """Delete a message after `delay` seconds."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    """Schedule deletion via asyncio task."""
    asyncio.create_task(delete_message_later(context.bot, chat_id, message_id, delay))


def format_mention(user_id: int, first_name: str, emoji: str) -> str:
    return f'<a href="tg://user?id={user_id}">{emoji}{first_name}</a>'


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
            "!смайлик або /emoji — змінити смайлик"
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

    # Check if admins-only mode is on
    settings = db.get_chat_settings(chat_id)
    if settings and settings.get("admins_only"):
        if not await is_admin(context.bot, chat_id, user_id):
            msg = await message.reply_text("🔒 Заклик доступний лише адміністраторам.")
            await schedule_delete(context, chat_id, msg.message_id, 5)
            return

    # Cooldown check (1 minute)
    now = time.time()
    last = call_cooldowns.get(chat_id, 0)
    remaining = 60 - (now - last)
    if remaining > 0 and not is_super_admin(user_id):
        msg = await message.reply_text(
            f"⏳ Заклик можна надіслати через {int(remaining)} сек."
        )
        await schedule_delete(context, chat_id, msg.message_id, 5)
        return

    call_cooldowns[chat_id] = now

    # Extract optional comment from command args
    text = message.text or ""
    comment = ""
    for trigger in ["!заклик", "!скликати", "/call"]:
        if text.lower().startswith(trigger):
            comment = text[len(trigger):].strip()
            break

    # Get registered users
    members = db.get_registered_members(chat_id)
    if not members:
        msg = await message.reply_text(
            "😔 Немає зареєстрованих учасників.\n"
            "Виконай /regchat або !перепис населення щоб додати всіх."
        )
        return

    # Build mention list — skip super admin if hidden
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

    header = f"📢 <b>Заклик!</b>"
    if comment:
        header += f"\n💬 {comment}"
    body = " ".join(mentions)
    full_text = f"{header}\n\n{body}"

    sent = await message.reply_text(full_text, parse_mode="HTML")

    # Auto-delete if configured
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

    # Only owner or super admin
    if not await is_chat_owner(context.bot, chat.id, user.id):
        msg = await message.reply_text("🔒 Доступно лише власнику чату.")
        await schedule_delete(context, chat.id, msg.message_id, 5)
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError as e:
        await message.reply_text(f"❌ Помилка: {e}")
        return

    existing = {m[0] for m in db.get_registered_members(chat.id)}
    added = 0

    # We can't iterate all members via Bot API (no method for that),
    # so fix_call re-registers anyone who used the bot but isn't in the list
    msg = await message.reply_text(
        "🔄 Перевіряю заклик...\n"
        "Примітка: Telegram не дозволяє отримати повний список учасників через Bot API.\n"
        "Для повного перепису використовуй !перепис населення (або /regchat) — "
        "але учасники мають написати щось у чат після цієї команди.\n\n"
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

    user_id = user.id
    first_name = user.first_name

    if all_chats:
        count = db.set_active_all_chats(user_id, True)
        await message.reply_text(f"🤗 Ти повернувся до заклику в усіх {count} чатах!")
    else:
        chat_id = chat.id
        db.ensure_member(chat_id, user_id, first_name)
        db.set_active(chat_id, user_id, True)
        emoji = db.get_emoji(chat_id, user_id) or "👤"
        await message.reply_text(f"✅ Ти знову в закликі! {emoji}")


async def unreg(update: Update, context: ContextTypes.DEFAULT_TYPE, all_chats: bool = False):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    user_id = user.id

    if all_chats:
        count = db.set_active_all_chats(user_id, False)
        await message.reply_text(f"😡 Ти вийшов із заклику в усіх {count} чатах!")
    else:
        chat_id = chat.id
        db.set_active(chat_id, user_id, False)
        await message.reply_text("❌ Ти вийшов із заклику в цьому чаті.")


# ─────────────────────────── CHECK STATUS ───────────────────────────

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    chat_id = chat.id

    # If reply — check the replied-to user
    target_user = user
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user

    target_id = target_user.id
    member = db.get_member(chat_id, target_id)

    if not member:
        await message.reply_text(
            f"🤷 {target_user.first_name} не зареєстрований у закликі."
        )
        return

    _, first_name, emoji, active = member
    status = "✅ активний" if active else "❌ неактивний"
    await message.reply_text(
        f"{'Ти' if target_id == user.id else target_user.first_name}: {emoji} | {status}"
    )


# ─────────────────────────── SET EMOJI ───────────────────────────

async def set_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    args = context.args
    text = message.text or ""

    # Try to extract emoji from text after command/trigger
    new_emoji = None
    for trigger in ["!смайлик", "/emoji", "/set_emoji"]:
        if trigger in text:
            rest = text[text.index(trigger) + len(trigger):].strip()
            if rest:
                new_emoji = rest.split()[0]
            break

    if args and not new_emoji:
        new_emoji = args[0]

    if not new_emoji:
        await message.reply_text(
            "😎 Вкажи новий смайлик після команди.\n"
            "Приклад: !смайлик 🔥"
        )
        return

    chat_id = chat.id
    db.ensure_member(chat_id, user.id, user.first_name)
    db.set_emoji(chat_id, user.id, new_emoji)
    await message.reply_text(f"😎 Твій смайлик змінено на {new_emoji}!")


# ─────────────────────────── REGCHAT (census) ───────────────────────────

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

    # Telegram Bot API doesn't support fetching all members directly.
    # We register the command caller and note limitation.
    db.ensure_member(chat.id, user.id, user.first_name)
    db.set_active(chat.id, user.id, True)

    count = len(db.get_registered_members(chat.id))
    await message.reply_text(
        "🦅 <b>Перепис населення запущено!</b>\n\n"
        "⚠️ Telegram не дозволяє ботам отримати повний список учасників.\n"
        "Учасники будуть автоматично додані до заклику, коли напишуть будь-що у чат.\n\n"
        f"📊 Зараз у закликі: <b>{count}</b> учасників.\n"
        "Можна також використати !заклик+ щоб приєднатись вручну.",
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


# ─────────────────────────── AUTO-REGISTER on message ───────────────────────────

async def auto_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-register users who send messages in groups."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not user or chat.type == "private":
        return

    db.ensure_member(chat.id, user.id, user.first_name)


# ─────────────────────────── SUPER ADMIN COMMANDS ───────────────────────────

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

    stats_text = (
        f"🤖 <b>Системна статистика бота</b>\n\n"
        f"⏱ Аптайм: {hours}г {minutes}хв {seconds}с\n"
        f"🗓 Запущено: {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"📊 <b>База даних:</b>\n"
        f"💬 Чатів: {total_chats}\n"
        f"👥 Учасників (загалом): {total_members}\n"
        f"✅ Активних у закликах: {total_active}\n"
        f"❌ Неактивних: {total_members - total_active}\n\n"
        f"🔥 Версія: 1.0.0"
    )

    await update.effective_message.reply_text(stats_text, parse_mode="HTML")


async def hide_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle hidden status for super admin — success message auto-deletes in 1s."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not is_super_admin(user.id):
        return

    chat_id = chat.id if chat.type != "private" else None

    current = db.get_hidden_user(chat_id)
    if current == user.id:
        db.set_hidden_user(chat_id, None)
        status = "✅"
    else:
        db.set_hidden_user(chat_id, user.id)
        status = "✅"

    sent = await message.reply_text(status)
    asyncio.create_task(delete_message_later(context.bot, message.chat_id, sent.message_id, 1))
    # Also delete the command message
    asyncio.create_task(delete_message_later(context.bot, message.chat_id, message.message_id, 1))


# ─────────────────────────── TEXT COMMAND ROUTER ───────────────────────────

CALL_TRIGGERS = ["!заклик", "!скликати", "/call"]
UNREG_TRIGGERS = ["!заклик-", "заклик-", "!call-", "!оф", "!піти по хліб", "!тиха година", "!я сплю", "/unreg"]
UNREG_ALL_TRIGGERS = ["!заклик--", "/unreg_all"]
REG_TRIGGERS = ["!заклик+", "заклик+", "!call+", "!прокинувся", "!прокинулась", "!з хлібом", "/reg"]
REG_ALL_TRIGGERS = ["!заклик++", "!call++", "/reg_all"]
CHECK_TRIGGERS = ["!заклик?"]
EMOJI_TRIGGERS = ["!смайлик", "/emoji", "/set_emoji"]
CENSUS_TRIGGERS = ["!перепис населення", "/regchat"]
HIDE_TRIGGERS = ["!не згадувати мене"]
STATS_TRIGGERS = ["!botsysstats"]


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    text_lower = text.lower()
    user = update.effective_user
    chat = update.effective_chat

    if chat.type == "private":
        return

    # Auto-register
    db.ensure_member(chat.id, user.id, user.first_name)

    # Super admin secret commands
    if is_super_admin(user.id):
        if any(text_lower.startswith(t.lower()) for t in HIDE_TRIGGERS):
            await hide_me(update, context)
            return
        if any(text_lower.startswith(t.lower()) for t in STATS_TRIGGERS):
            await botsysstats(update, context)
            return

    # UNREG ALL (must check before UNREG)
    if any(text_lower.startswith(t.lower()) for t in UNREG_ALL_TRIGGERS):
        await unreg(update, context, all_chats=True)
        return

    # REG ALL (must check before REG)
    if any(text_lower.startswith(t.lower()) for t in REG_ALL_TRIGGERS):
        await reg(update, context, all_chats=True)
        return

    # UNREG
    if any(text_lower.startswith(t.lower()) for t in UNREG_TRIGGERS):
        await unreg(update, context)
        return

    # REG
    if any(text_lower.startswith(t.lower()) for t in REG_TRIGGERS):
        await reg(update, context)
        return

    # CHECK
    if any(text_lower.startswith(t.lower()) for t in CHECK_TRIGGERS):
        await check_status(update, context)
        return

    # EMOJI
    if any(text_lower.startswith(t.lower()) for t in EMOJI_TRIGGERS):
        await set_emoji(update, context)
        return

    # CENSUS
    if any(text_lower.startswith(t.lower()) for t in CENSUS_TRIGGERS):
        await regchat(update, context)
        return

    # CALL (must be last to avoid prefix conflicts)
    for trigger in CALL_TRIGGERS:
        if text_lower.startswith(trigger.lower()):
            # Make sure it's not actually !заклик- or !заклик+
            rest = text[len(trigger):]
            if rest and rest[0] in ("+", "-", "?"):
                break
            await handle_call(update, context)
            return


# ─────────────────────────── MAIN ───────────────────────────

def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Slash commands
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

    # Text message router (handles ! commands)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        text_router
    ))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
