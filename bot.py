import asyncio
import logging
import random
import re
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile, BusinessConnection, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType

from config import BOT_TOKEN
# Optional AI settings: the existing config.py may only define BOT_TOKEN.
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "qwen3.5:4b")
try:
    AI_DELAY_SECONDS = max(1, int(os.getenv("AI_DELAY_SECONDS", "120")))
except ValueError:
    AI_DELAY_SECONDS = 120
import database as db
from keyboards import main_menu, chat_actions, unmute_keyboard, ai_keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()
# Tasks are intentionally in-memory: after a restart no delayed answer is sent unexpectedly.
pending_ai: dict[tuple[int, str, int], asyncio.Task] = {}
# At most one AI reply per private chat until the owner sends a message there.
ai_replied_chats: set[tuple[int, str, int]] = set()
# Controlled test only: limit repeats to avoid flooding chats or triggering Telegram limits.
pending_spam: dict[tuple[int, str, int], asyncio.Task] = {}


def safe_text(value, limit=2500):
    return escape(str(value or "")[:limit])


def warning_keyboard(owner_id: int, chat_id: int, sender_id: int):
    """The button is displayed in the warning message; only the business account owner may use it."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛡️ Remove Warning",
            callback_data=f"warnclear:{owner_id}:{chat_id}:{sender_id}",
        )
    ]])


def profile_link(user):
    if not user:
        return "Невідомий користувач"
    if getattr(user, "username", None):
        username = escape(user.username)
        return f'<a href="https://t.me/{username}">@{username}</a>'
    name = escape(getattr(user, "full_name", None) or "Користувач")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def get_media(message: Message):
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return None, None


async def send_archived_media(bot: Bot, owner_id: int, media_type, file_id, caption=""):
    if not file_id:
        return False
    caption = (caption or "")[:900]
    try:
        methods = {
            "photo": bot.send_photo, "video": bot.send_video,
            "animation": bot.send_animation, "document": bot.send_document,
            "audio": bot.send_audio, "voice": bot.send_voice,
            "video_note": bot.send_video_note,
        }
        method = methods.get(media_type)
        if not method:
            return False
        if media_type == "video_note":
            # Telegram video notes (round videos) do not support captions.
            await method(chat_id=owner_id, video_note=file_id)
        else:
            await method(chat_id=owner_id, **{media_type: file_id}, caption=caption)
        return True
    except Exception:
        logging.exception("Не вдалося надіслати архівне медіа")
        return False


async def try_delete_business_message(bot: Bot, connection_id: str, message_id: int):
    try:
        await bot.delete_business_messages(business_connection_id=connection_id, message_ids=[message_id])
        return True
    except Exception:
        logging.exception("Telegram не дозволив видалити Business-повідомлення")
        return False


async def notify_deleted(bot: Bot, owner_id: int, connection_id: str, chat_id: int,
                         title, sender, body, created_at, media_type=None,
                         media_file_id=None, blocked=False):
    text = (
        "🗑 <b>Видалене повідомлення</b>\n"
        f"💬 Чат: {safe_text(title or 'Чат', 180)}\n"
        f"👤 Від: {sender or 'Невідомий користувач'}\n"
        f"🕒 Отримано: {safe_text(created_at, 80)}\n\n"
        f"{safe_text(body, 2200)}"
    )
    try:
        if media_type and media_file_id:
            ok = await send_archived_media(bot, owner_id, media_type, media_file_id,
                                           f"Архів eSIM • {title or 'Чат'}\nВід: {sender or 'невідомо'}")
            if not ok:
                text += "\n\n⚠️ Медіафайл не вдалося переслати; запис збережено."
        await bot.send_message(owner_id, text, reply_markup=chat_actions(connection_id, chat_id, blocked))
        logging.info(
            "ESIM_DIAG NOTIFY_DELETED SENT: owner=%s connection=%s chat=%s",
            owner_id, connection_id, chat_id
        )
    except Exception:
        logging.exception(
            "ESIM_DIAG NOTIFY_DELETED FAILED: owner=%s connection=%s chat=%s",
            owner_id, connection_id, chat_id
        )


async def call_ai(owner_id: int, incoming_text: str) -> str:
    # Always have a safe fallback so a slow/unavailable local model does not
    # silently result in no reply. A custom fallback from /auto takes priority.
    fallback = (await db.get_setting(owner_id, "auto_reply") or "").strip()
    if not fallback:
        fallback = "Привіт! Я трохи зайнятий, відповім трохи пізніше."
    headers = {"Content-Type": "application/json"}
    if AI_API_KEY:
        headers["Authorization"] = f"Bearer {AI_API_KEY}"
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "Ти короткий, ввічливий помічник власника Telegram Business. Проаналізуй повідомлення співрозмовника й дай одну коротку, чітку відповідь мовою співрозмовника, максимум 1-2 речення. Не вигадуй факти, ціни, обіцянки чи особисті обставини. Якщо потрібне рішення власника, скажи, що він трохи зайнятий і відповість пізніше. Наприкінці відповіді природно додай: «Я трохи зайнятий, відповім трохи пізніше.»"},
            {"role": "user", "content": incoming_text[:3000]},
        ],
        "max_tokens": 140,
        "temperature": 0.4,
    }
    try:
        # Local models may take a while to load or generate the first answer.
        timeout = aiohttp.ClientTimeout(total=150, connect=10, sock_connect=10, sock_read=140)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{AI_BASE_URL}/chat/completions", headers=headers, json=payload) as response:
                if response.status >= 400:
                    logging.warning("AI endpoint returned HTTP %s", response.status)
                    return fallback
                data = await response.json()
        answer = data["choices"][0]["message"]["content"].strip()
        return answer[:1200] if answer else fallback
    except Exception:
        logging.exception("AI-відповідь не вдалася")
        return fallback


async def delayed_ai_reply(bot: Bot, owner_id: int, connection_id: str, chat_id: int, incoming_text: str):
    key = (owner_id, connection_id, chat_id)
    started_at = asyncio.get_running_loop().time()
    try:
        # Start AI analysis immediately. The 2-minute delay is measured from
        # message arrival, not from when the model finishes generating.
        answer = await call_ai(owner_id, incoming_text)
        if not answer:
            return
        remaining = AI_DELAY_SECONDS - (asyncio.get_running_loop().time() - started_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        # Re-check switch and mute status right before sending.
        if (await db.get_setting(owner_id, "ai_enabled") or "0") != "1":
            return
        connection = await db.get_connection(connection_id)
        if not connection or connection[0] != owner_id or not connection[1]:
            return
        if await db.is_muted(owner_id, connection_id, chat_id):
            return
        # Mark before sending to prevent a second response if another update races in.
        if key in ai_replied_chats:
            return
        ai_replied_chats.add(key)
        await bot.send_message(chat_id=chat_id, text=answer, business_connection_id=connection_id)
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Помилка відкладеної AI-відповіді")
    finally:
        if pending_ai.get(key) is asyncio.current_task():
            pending_ai.pop(key, None)


def cancel_pending(owner_id: int, connection_id: str, chat_id: int):
    task = pending_ai.pop((owner_id, connection_id, chat_id), None)
    if task and not task.done():
        task.cancel()


@dp.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return
    await db.register_user(message.from_user.id)
    caption = (
        "👋 <b>eSIM</b> — помічник Telegram Business\n\n"
        "Підключи @eSimcorporationbot у Telegram → Налаштування → Telegram Business → Чат-боти.\n\n"
        "Кожен користувач керує лише своїми підключеннями та архівом. Бот працює тільки з повідомленнями, доступними через Telegram Business API."
    )
    image_path = Path(__file__).with_name("esim_start.png")
    if image_path.exists():
        await message.answer_photo(FSInputFile(image_path), caption=caption, reply_markup=main_menu())
    else:
        await message.answer(caption + "\n\nℹ️ Щоб показати стартове зображення, поклади файл <code>esim_start.png</code> поруч із bot.py.", reply_markup=main_menu())


@dp.business_connection()
async def business_connection_update(connection: BusinessConnection):
    owner_id = connection.user.id
    rights = connection.rights
    can_reply = getattr(rights, "can_reply", None) if rights else None
    await db.save_connection(connection.id, owner_id, connection.is_enabled, can_reply)
    logging.info(
        "ESIM_DIAG business_connection: owner=%s enabled=%s can_reply=%s rights=%s",
        owner_id, connection.is_enabled, can_reply, rights
    )
    try:
        if connection.is_enabled:
            await db.register_user(owner_id)
    except Exception:
        logging.exception("Не вдалося зберегти власника Business-підключення")


@dp.message(Command("guide"))
async def guide_command(message: Message):
    path = Path(__file__).with_name("esim_connect_guide.png")
    caption = "📘 <b>Підключення eSIM</b>\n1. Telegram → Налаштування.\n2. Telegram Business → Чат-боти.\n3. Додай @eSimcorporationbot.\n4. Вибери потрібні чати та дозвіл на відповіді, якщо він доступний."
    if path.exists():
        await message.answer_photo(FSInputFile(path), caption=caption)
    else:
        await message.answer(caption)


@dp.message(Command("diag"))
async def diag_command(message: Message):
    """Show safe local diagnostic state for the current user's Business connections."""
    owner_id = message.from_user.id
    rows = await db.list_chats(owner_id, 50)
    connection_rows = []
    # Connection ids are local opaque identifiers; show only count and status, not the raw IDs.
    seen = set()
    for connection_id, chat_id, title, muted in rows:
        if connection_id not in seen:
            seen.add(connection_id)
            connection = await db.get_connection(connection_id)
            connection_rows.append((bool(connection), bool(connection and connection[1])))
    await message.answer(
        "🩺 <b>Діагностика eSIM</b>\\n"
        f"Збережених чатів: {len(rows)}\\n"
        f"Підключень у списку чатів: {len(seen)}\\n"
        f"Активних підключень: {sum(1 for exists, enabled in connection_rows if exists and enabled)}\\n\\n"
        "Тепер протестуй редагування/видалення і перевір термінал на рядки <code>ESIM_DIAG</code>. "
        "Якщо після дії немає відповідного рядка RECEIVED, Telegram не передав цю подію цьому запуску."
    )


@dp.message(Command("chats"))
async def chats_command(message: Message):
    owner_id = message.from_user.id
    rows = await db.list_chats(owner_id)
    if not rows:
        await message.answer("Поки немає чатів. Перевір, чи підключений eSIM до Telegram Business і чи дозволений доступ до потрібних чатів.")
        return
    for connection_id, chat_id, title, blocked in rows:
        await message.answer(f"💬 <b>{safe_text(title or 'Чат', 180)}</b>\nID: <code>{chat_id}</code>\nРежим: {'🚫 Заблокований в eSIM' if blocked else '🔔 Активний'}", reply_markup=chat_actions(connection_id, chat_id, bool(blocked)))


@dp.message(Command("deleted"))
async def deleted_command(message: Message, bot: Bot):
    owner_id = message.from_user.id
    rows = await db.latest_deleted(owner_id)
    logging.info(
        "ESIM_DIAG ARCHIVE_CHECK: user_id=%s db_file=%s rows=%s",
        owner_id, db.DB_PATH, len(rows)
    )
    if not rows:
        # Diagnostic: show whether the database has archive entries for any owner,
        # without exposing message contents or other users' data.
        try:
            import aiosqlite
            async with aiosqlite.connect(db.DB_PATH) as conn:
                total = await (await conn.execute("SELECT COUNT(*) FROM esim_deleted")).fetchone()
                owners = await (await conn.execute("SELECT COUNT(DISTINCT owner_id) FROM esim_deleted")).fetchone()
            logging.info(
                "ESIM_DIAG ARCHIVE_TOTAL: db_file=%s total_rows=%s distinct_owners=%s",
                db.DB_PATH, total[0] if total else 0, owners[0] if owners else 0
            )
        except Exception:
            logging.exception("ESIM_DIAG ARCHIVE_TOTAL FAILED")
        await message.answer("🗑 Архів поки порожній. Діагностика записана в термінал.")
        return
    for connection_id, chat_id, title, sender, body, deleted_at, media_type, media_file_id in rows:
        await message.answer(f"🗑 <b>Видалене повідомлення</b>\n💬 Чат: {safe_text(title or 'Чат', 180)}\n👤 Від: {sender or 'Невідомо'}\n🕒 {safe_text(deleted_at, 80)}\n\n{safe_text(body, 2200)}")
        if media_type and media_file_id:
            await send_archived_media(bot, message.from_user.id, media_type, media_file_id, f"Архів eSIM • {title or 'Чат'}")


@dp.message(Command("edits"))
async def edits_command(message: Message):
    rows = await db.latest_edits(message.from_user.id, 10)
    if not rows:
        await message.answer("✏️ Історія редагувань поки порожня. Старий текст можна зберегти лише якщо бот отримав початкове повідомлення до редагування.")
        return
    for title, sender, old_body, new_body, edited_at in rows:
        await message.answer(f"✏️ <b>Редагування</b>\n💬 {safe_text(title or 'Чат', 180)}\n👤 {sender or 'Невідомо'}\n🕒 {safe_text(edited_at, 80)}\n\n<b>Було:</b> {safe_text(old_body or '[без тексту]', 1000)}\n\n<b>Стало:</b> {safe_text(new_body or '[без тексту]', 1000)}")


@dp.message(Command("auto"))
async def auto_command(message: Message):
    owner_id = message.from_user.id
    value = (message.text or "").partition(" ")[2].strip()
    if not value:
        current = await db.get_setting(owner_id, "auto_reply") or ""
        enabled = (await db.get_setting(owner_id, "ai_enabled") or "0") == "1"
        await message.answer("🤖 <b>ШІ-автовідповідач</b>\n\n" + f"Стан: {'увімкнено' if enabled else 'вимкнено'}\n" + f"Резервний текст: {safe_text(current or 'не заданий', 800)}\n\n" + "<code>/ai on</code> — увімкнути\n<code>/ai off</code> — вимкнути\n<code>/auto текст</code> — резервна відповідь\n<code>/auto off</code> — очистити резервний текст", reply_markup=ai_keyboard(enabled))
    elif value.lower() == "off":
        await db.set_setting(owner_id, "auto_reply", "")
        await message.answer("Резервний текст очищено. Перемикач AI не змінено.")
    else:
        await db.set_setting(owner_id, "auto_reply", value[:1000])
        await message.answer("✅ Резервний текст збережено.")


@dp.message(Command("ai"))
async def ai_command(message: Message):
    owner_id = message.from_user.id
    value = (message.text or "").partition(" ")[2].strip().lower()
    if value not in ("on", "off"):
        enabled = (await db.get_setting(owner_id, "ai_enabled") or "0") == "1"
        await message.answer("ШІ-автовідповідач " + ("увімкнений." if enabled else "вимкнений."), reply_markup=ai_keyboard(enabled))
        return
    await db.set_setting(owner_id, "ai_enabled", "1" if value == "on" else "0")
    if value == "off":
        for key in list(pending_ai):
            if key[0] == owner_id:
                cancel_pending(*key)
        await message.answer("⏸ ШІ вимкнено. Усі очікувані відповіді цього користувача скасовано.", reply_markup=ai_keyboard(False))
    else:
        await message.answer("▶️ ШІ увімкнено. Якщо ти не відповіси протягом 2 хвилин, локальна ШІ-модель спробує сформувати одну коротку відповідь. Потрібно, щоб Ollama була запущена.", reply_markup=ai_keyboard(True))


@dp.callback_query(F.data == "ai:on")
async def ai_on_callback(call: CallbackQuery):
    await db.set_setting(call.from_user.id, "ai_enabled", "1")
    try:
        await call.answer("ШІ увімкнено")
    except Exception:
        logging.info("Callback ШІ-увімкнення вже застарів")
    await call.message.answer("▶️ ШІ увімкнено. Якщо ти відповіси сам, очікувана відповідь буде скасована.")


@dp.callback_query(F.data == "ai:off")
async def ai_off_callback(call: CallbackQuery):
    owner_id = call.from_user.id
    await db.set_setting(owner_id, "ai_enabled", "0")
    for key in list(pending_ai):
        if key[0] == owner_id:
            cancel_pending(*key)
    try:
        await call.answer("ШІ вимкнено")
    except Exception:
        logging.info("Callback ШІ-вимкнення вже застарів")
    await call.message.answer("⏸ ШІ вимкнено; усі очікувані відповіді скасовано.")


@dp.callback_query(F.data == "menu:chats")
async def menu_chats(call: CallbackQuery):
    rows = await db.list_chats(call.from_user.id)
    await call.answer()
    if not rows:
        await call.message.answer("Поки немає чатів. Спочатку підключи eSIM до Telegram Business.")
        return
    for connection_id, chat_id, title, blocked in rows:
        await call.message.answer(f"💬 <b>{safe_text(title or 'Чат', 180)}</b>\nID: <code>{chat_id}</code>\nРежим: {'🚫 Заблокований в eSIM' if blocked else '🔔 Активний'}", reply_markup=chat_actions(connection_id, chat_id, bool(blocked)))


@dp.callback_query(F.data == "menu:deleted")
async def menu_deleted(call: CallbackQuery, bot: Bot):
    await call.answer()
    rows = await db.latest_deleted(call.from_user.id, 10)
    if not rows:
        await call.message.answer("🗑 Архів поки порожній.")
        return
    for connection_id, chat_id, title, sender, body, deleted_at, media_type, media_file_id in rows:
        await call.message.answer(f"🗑 <b>Видалене повідомлення</b>\n💬 {safe_text(title or 'Чат', 180)}\n👤 {sender or 'Невідомо'}\n🕒 {safe_text(deleted_at, 80)}\n\n{safe_text(body, 2200)}")
        if media_type and media_file_id:
            await send_archived_media(bot, call.from_user.id, media_type, media_file_id, f"Архів eSIM • {title or 'Чат'}")


@dp.callback_query(F.data == "menu:edits")
async def menu_edits(call: CallbackQuery):
    await call.answer()
    rows = await db.latest_edits(call.from_user.id, 10)
    if not rows:
        await call.message.answer("✏️ Історія редагувань поки порожня. Вона накопичуватиметься для повідомлень, які бот отримав до редагування.")
        return
    for title, sender, old_body, new_body, edited_at in rows:
        await call.message.answer(f"✏️ <b>{safe_text(title or 'Чат', 180)}</b>\n👤 {sender or 'Невідомо'}\n🕒 {safe_text(edited_at, 80)}\n<b>Було:</b> {safe_text(old_body or '[без тексту]', 900)}\n<b>Стало:</b> {safe_text(new_body or '[без тексту]', 900)}")


@dp.callback_query(F.data == "menu:ai")
async def menu_ai(call: CallbackQuery):
    enabled = (await db.get_setting(call.from_user.id, "ai_enabled") or "0") == "1"
    await call.answer()
    await call.message.answer("🤖 <b>ШІ-автовідповідач</b>\nВідповідає через 2–5 хвилин, якщо ти не відповів раніше. Вимкнення скасовує очікувані відповіді.", reply_markup=ai_keyboard(enabled))


@dp.callback_query(F.data == "menu:guide")
async def menu_guide(call: CallbackQuery):
    await call.answer()
    path = Path(__file__).with_name("esim_connect_guide.png")
    caption = "📘 <b>Як підключити eSIM</b>\n1. Telegram → Налаштування.\n2. Telegram Business → Чат-боти.\n3. Додай @eSimcorporationbot.\n4. Вибери потрібні чати та доступні дозволи."
    if path.exists():
        await call.message.answer_photo(FSInputFile(path), caption=caption)
    else:
        await call.message.answer(caption)


@dp.callback_query(F.data == "menu:help")
async def menu_help(call: CallbackQuery):
    await call.answer()
    await call.message.answer("<b>Команди eSIM</b>\n/start — меню\n/guide — підключення\n/chats — список чатів\n/deleted — архів видалених\n/edits — історія редагувань\n/auto текст — резервна відповідь\n/auto off — очистити резервний текст\n/ai on — увімкнути AI\n/ai off — вимкнути AI\n\nУ підключеному Business-чаті: <code>.mute</code> / <code>.unmute</code>, <code>.warn 5</code> / <code>.warn off</code>, <code>.spam 100 привіт</code> (до 100 повідомлень із паузою 1 с), <code>.stopspam</code>. Команди працюють лише в чатах, доступних через Telegram Business, не в особистому чаті з ботом. Це локальний режим eSIM, а не блокування в самому Telegram.")


async def handle_block_callback(call: CallbackQuery, blocked: bool):
    try:
        _, connection_id, chat_id_text = call.data.split(":", 2)
        chat_id = int(chat_id_text)
    except (ValueError, AttributeError):
        await call.answer("Некоректна кнопка", show_alert=True)
        return
    connection = await db.get_connection(connection_id)
    if not connection or connection[0] != call.from_user.id:
        await call.answer("Це підключення не належить тобі.", show_alert=True)
        return
    await db.set_muted(call.from_user.id, connection_id, chat_id, blocked)
    try:
        await call.message.edit_reply_markup(reply_markup=chat_actions(connection_id, chat_id, blocked))
    except Exception:
        pass
    await call.answer("Заблоковано в eSIM" if blocked else "Розблоковано в eSIM")


@dp.callback_query(F.data.startswith("block:"))
async def block_callback(call: CallbackQuery):
    await handle_block_callback(call, True)


@dp.callback_query(F.data.startswith("unblock:"))
async def unblock_callback(call: CallbackQuery):
    await handle_block_callback(call, False)


@dp.callback_query(F.data.startswith("unmute:"))
async def unmute_notice_callback(call: CallbackQuery):
    try:
        _, connection_id, chat_id_text = call.data.split(":", 2)
        chat_id = int(chat_id_text)
    except (ValueError, AttributeError):
        await call.answer("Некоректна кнопка", show_alert=True)
        return
    connection = await db.get_connection(connection_id)
    if not connection or connection[0] != call.from_user.id:
        await call.answer("Ця кнопка доступна лише власнику підключення.", show_alert=True)
        return
    await db.set_muted(call.from_user.id, connection_id, chat_id, False)
    try:
        await call.message.edit_text("🔊 <b>Режим блокування в eSIM вимкнено.</b>", reply_markup=None)
    except Exception:
        pass
    await call.answer("Розблоковано в eSIM")


async def run_test_spam(bot: Bot, owner_id: int, connection_id: str, chat_id: int, count: int, text: str):
    """Send a controlled test sequence in the explicitly selected connected Business chat."""
    key = (owner_id, connection_id, chat_id)
    completed = 0
    try:
        for index in range(count):
            connection = await db.get_connection(connection_id)
            if not connection or connection[0] != owner_id or not connection[1]:
                break
            if await db.is_muted(owner_id, connection_id, chat_id):
                break
            await bot.send_message(
                chat_id=chat_id,
                business_connection_id=connection_id,
                text=text,
            )
            completed += 1
            logging.info("TEST_MESSAGE_SENT %s/%s chat=%s", completed, count, chat_id)
            if index + 1 < count:
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        logging.info("Тестову серію зупинено через .stopspam; sent=%s", completed)
        return
    except Exception:
        logging.exception("Тестова серія перервалася після %s повідомлень", completed)
    finally:
        if pending_spam.get(key) is asyncio.current_task():
            pending_spam.pop(key, None)


@dp.callback_query(F.data.startswith("warnclear:"))
async def warning_clear_callback(call: CallbackQuery, bot: Bot):
    """Let only the person receiving the warning clear it and request unmute."""
    try:
        _, owner_text, chat_text, sender_text = (call.data or "").split(":", 3)
        owner_id = int(owner_text)
        chat_id = int(chat_text)
        sender_id = int(sender_text)
    except (ValueError, AttributeError):
        await call.answer("Invalid warning button.", show_alert=True)
        return

    # Never let the warned user (or any other account) clear their own warning/mute.
    # Only the owner of the connected eSIM Business account may do this.
    if call.from_user.id != owner_id:
        await call.answer("Only the account owner can remove this warning.", show_alert=True)
        return

    rows = await db.list_chats(owner_id, 1000)
    connection_id = next((row[0] for row in rows if int(row[1]) == chat_id), None)
    if not connection_id:
        await call.answer("Could not find this chat connection.", show_alert=True)
        return
    connection = await db.get_connection(connection_id)
    if not connection or connection[0] != owner_id or not connection[1]:
        await call.answer("This chat connection is no longer active.", show_alert=True)
        return

    warning_state = await db.get_warning(owner_id, connection_id, chat_id)
    warning_limit_was_reached = bool(warning_state and warning_state[2] and warning_state[1] <= 0)
    await db.clear_warning(owner_id, connection_id, chat_id)
    if warning_limit_was_reached:
        await db.set_muted(owner_id, connection_id, chat_id, False)
    await call.answer("Warning removed.")
    try:
        await bot.send_message(
            chat_id=chat_id,
            business_connection_id=connection_id,
            text=("✅ <b>WARNING REMOVED!</b> ❗ You can send messages again." if warning_limit_was_reached else "✅ <b>WARNING REMOVED!</b> ❗ The warning counter has been disabled."),
        )
    except Exception:
        logging.exception("Could not send warning-cleared confirmation")


@dp.business_message()
async def business_message(message: Message, bot: Bot):
    connection_id = message.business_connection_id
    logging.info(
        "ESIM_DIAG business_message RECEIVED: connection=%s chat=%s message=%s from=%s text=%r",
        connection_id, getattr(message.chat, "id", None), message.message_id,
        getattr(message.from_user, "id", None), (message.text or message.caption or "")[:120]
    )
    if not connection_id or not message.chat:
        logging.warning("ESIM_DIAG business_message SKIPPED: missing connection_id or chat")
        return
    connection = await db.get_connection(connection_id)
    if not connection or not connection[1]:
        logging.warning("ESIM_DIAG business_message SKIPPED: connection not found/disabled id=%s row=%s", connection_id, connection)
        return
    owner_id = connection[0]
    sender = message.from_user
    chat = message.chat
    title = chat.full_name if chat.type == ChatType.PRIVATE else (chat.title or "Чат")
    sender_name = profile_link(sender)
    body = message.text or message.caption or ""
    media_type, media_file_id = get_media(message)
    if not body:
        labels = {"photo": "Фото", "video": "Відео", "video_note": "Відеоповідомлення-кружок", "animation": "GIF/анімація", "document": "Файл", "audio": "Аудіо", "voice": "Голосове повідомлення"}
        body = f"[{labels.get(media_type, 'Повідомлення без тексту')}]"

    is_owner_message = bool(sender and sender.id == owner_id)
    command = (message.text or "").strip().lower()
    if is_owner_message:
        # Any owner reply cancels the pending AI answer in this chat.
        cancel_pending(owner_id, connection_id, chat.id)
        ai_replied_chats.discard((owner_id, connection_id, chat.id))
        # .spam N text sends a controlled test sequence in this connected Business chat, max 100, 1 second apart.
        raw_command = (message.text or "").strip()
        spam_match = re.match(r"^\.spam\s+(\d{1,3})\s+([\s\S]+)$", raw_command, re.IGNORECASE)
        if raw_command.lower() == ".stopspam":
            spam_key = (owner_id, connection_id, chat.id)
            task = pending_spam.pop(spam_key, None)
            if task and not task.done():
                task.cancel()
            try:
                await bot.delete_business_messages(business_connection_id=connection_id, message_ids=[message.message_id])
            except Exception:
                pass
            # Stop silently: the command is removed and no extra status message is sent.
            return
        if spam_match:
            count = int(spam_match.group(1))
            payload = spam_match.group(2).strip()
            try:
                await bot.delete_business_messages(business_connection_id=connection_id, message_ids=[message.message_id])
            except Exception:
                pass
            if not payload or count < 1:
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                                       text="Формат: <code>.spam 5 привіт</code>")
                return
            if count > 100:
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                                       text="Доступно максимум 100 повідомлень. Приклад: <code>.spam 100 привіт</code>")
                return
            spam_key = (owner_id, connection_id, chat.id)
            previous = pending_spam.pop(spam_key, None)
            if previous and not previous.done():
                previous.cancel()
            pending_spam[spam_key] = asyncio.create_task(
                run_test_spam(bot, owner_id, connection_id, chat.id, count, payload[:1000])
            )
            return
        warn_match = re.fullmatch(r"\.warn\s+(\d{1,3}|off)", raw_command, re.IGNORECASE)
        if raw_command.lower().startswith(".warn"):
            await try_delete_business_message(bot, connection_id, message.message_id)
            if not warn_match:
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                    text="❗ <b>WARNING SYSTEM</b> ❗\nUse <code>.warn 5</code> to allow 5 more incoming messages, or <code>.warn off</code> to disable it.")
                return
            value = warn_match.group(1).lower()
            if value == "off":
                # Disable the counter first, so later incoming messages cannot trigger it.
                warning_before = await db.get_warning(owner_id, connection_id, chat.id)
                await db.clear_warning(owner_id, connection_id, chat.id)
                # Only release the mute when a warning limit had actually been reached.
                # This avoids accidentally undoing a separate manual .mute.
                warning_limit_was_reached = bool(warning_before and warning_before[2] and warning_before[1] <= 0)
                if warning_limit_was_reached:
                    await db.set_muted(owner_id, connection_id, chat.id, False)
                status = " The warning-triggered eSIM mute has been removed." if warning_limit_was_reached else ""
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                    text="✅ <b>WARNING SYSTEM DISABLED!</b> ❗ The message counter is now off." + status)
                return
            count = int(value)
            if not 1 <= count <= 100:
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                    text="❗ Choose a limit from 1 to 100 messages.")
                return
            await db.set_warning(owner_id, connection_id, chat.id, count)
            await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                text=f"⚠️ <b>WARNING SYSTEM ENABLED!</b> ❗\nThe other person can send {count} more messages before the existing eSIM mute is activated.")
            return
        if command in (".mute", ".unmute"):
            blocked = command == ".mute"
            await db.set_muted(owner_id, connection_id, chat.id, blocked)
            await try_delete_business_message(bot, connection_id, message.message_id)
            try:
                if blocked:
                    await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                        text="🔇 <b>Режим блокування в eSIM увімкнено.</b> Бот намагатиметься видаляти нові вхідні повідомлення, якщо Telegram дозволяє.",
                        reply_markup=unmute_keyboard(connection_id, chat.id))
                else:
                    await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                        text="🔊 <b>Режим блокування в eSIM вимкнено.</b> Можна знову писати.")
            except Exception:
                logging.exception("Не вдалося надіслати повідомлення про mute/unmute")
            return

    logging.info(
        "ESIM_DIAG SAVE_MESSAGE START: db_file=%s owner=%s connection=%s chat=%s message=%s sender=%s",
        getattr(db, "DB_PATH", "unknown"), owner_id, connection_id, chat.id,
        message.message_id, sender.id if sender else None
    )
    try:
        await db.save_message(owner_id, connection_id, chat.id, title, message.message_id,
                              sender_name, sender.id if sender else None, body,
                              message.date.isoformat(), media_type, media_file_id)
        saved_record = await db.get_message(owner_id, connection_id, chat.id, message.message_id)
        logging.info(
            "ESIM_DIAG SAVE_MESSAGE RESULT: found=%s db_file=%s owner=%s connection=%s chat=%s message=%s",
            bool(saved_record), getattr(db, "DB_PATH", "unknown"), owner_id, connection_id,
            chat.id, message.message_id
        )
    except Exception:
        logging.exception(
            "ESIM_DIAG SAVE_MESSAGE FAILED: db_file=%s owner=%s connection=%s chat=%s message=%s",
            getattr(db, "DB_PATH", "unknown"), owner_id, connection_id, chat.id, message.message_id
        )
        raise

    if not is_owner_message and not await db.is_muted(owner_id, connection_id, chat.id):
        remaining = await db.decrement_warning(owner_id, connection_id, chat.id)
        if remaining is not None:
            if remaining > 0:
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                    text=f"⚠️ <b>WARNING!</b> ❗ You have <b>{remaining}</b> message(s) left!!!",
                    reply_markup=warning_keyboard(owner_id, chat.id, sender.id))
            else:
                await db.set_muted(owner_id, connection_id, chat.id, True)
                await bot.send_message(chat_id=chat.id, business_connection_id=connection_id,
                    text="🚫 <b>WARNING LIMIT REACHED!!!</b> ❗ You have no messages left. The eSIM mute is now active!!!",
                    reply_markup=warning_keyboard(owner_id, chat.id, sender.id))
    if not is_owner_message and await db.is_muted(owner_id, connection_id, chat.id):
        inserted = await db.save_deleted(owner_id, connection_id, chat.id, message.message_id,
                                          title, sender_name, body, message.date.isoformat(), media_type, media_file_id)
        if inserted:
            await notify_deleted(bot, owner_id, connection_id, chat.id, title, sender_name, body,
                                 message.date.isoformat(), media_type, media_file_id, blocked=True)
        if not await try_delete_business_message(bot, connection_id, message.message_id):
            try:
                await bot.send_message(owner_id, "⚠️ eSIM не зміг видалити повідомлення. Перевір права Business-бота в Telegram.")
            except Exception:
                pass
        return

    # Only private chats; group chats are deliberately excluded from automatic replies.
    if not is_owner_message and chat.type == ChatType.PRIVATE and (await db.get_setting(owner_id, "ai_enabled") or "0") == "1":
        key = (owner_id, connection_id, chat.id)
        # Never send multiple AI replies in the same private chat until owner replies.
        if key not in ai_replied_chats:
            cancel_pending(owner_id, connection_id, chat.id)
            pending_ai[key] = asyncio.create_task(delayed_ai_reply(bot, owner_id, connection_id, chat.id, body))


@dp.edited_business_message()
async def edited_business_message(message: Message, bot: Bot):
    connection_id = message.business_connection_id
    logging.info(
        "ESIM_DIAG edited_business_message RECEIVED: connection=%s chat=%s message=%s edit_date=%s text=%r",
        connection_id, getattr(message.chat, "id", None), message.message_id,
        message.edit_date, (message.text or message.caption or "")[:120]
    )
    if not connection_id:
        logging.warning("ESIM_DIAG edited_business_message SKIPPED: missing business_connection_id")
        return
    connection = await db.get_connection(connection_id)
    if not connection or not connection[1]:
        logging.warning("ESIM_DIAG edited_business_message SKIPPED: connection not found/disabled id=%s row=%s", connection_id, connection)
        return
    owner_id = connection[0]
    edit_value = message.edit_date
    if isinstance(edit_value, (int, float)):
        edited_at = datetime.fromtimestamp(edit_value, timezone.utc).isoformat()
    elif edit_value:
        edited_at = edit_value.isoformat()
    else:
        edited_at = datetime.now(timezone.utc).isoformat()
    new_body = message.text or message.caption or "[Медіа або повідомлення без тексту]"
    result = await db.update_message(owner_id, connection_id, message.chat.id, message.message_id, new_body, edited_at)
    title = message.chat.full_name if message.chat.type == ChatType.PRIVATE else (message.chat.title or "Чат")
    sender = profile_link(message.from_user)
    old_body = None
    if result:
        title, saved_sender, old_body = result
        sender = saved_sender or sender
    else:
        # The original update may have arrived before the bot was connected or while it was offline.
        # Telegram does not provide the previous text in an edit update.
        await db.save_message(
            owner_id, connection_id, message.chat.id, title, message.message_id,
            sender, message.from_user.id if message.from_user else None,
            new_body, message.date.isoformat(), *get_media(message)
        )
    if not result or (old_body or "") != (new_body or ""):
        try:
            profile = sender if sender else "Невідомий користувач"
            old_text = safe_text(old_body, 1000) if old_body is not None else "[старий текст недоступний: початкове повідомлення не було збережене]"
            await bot.send_message(
                owner_id,
                "✏️ <b>Повідомлення відредаговано</b>\n"
                f"💬 Чат: {safe_text(title or 'Чат', 180)}\n"
                f"👤 Користувач: {profile}\n"
                f"🆔 ID чату: <code>{message.chat.id}</code>\n"
                f"🕒 Час: {safe_text(edited_at, 80)}\n\n"
                f"<b>Було:</b>\n{old_text}\n\n"
                f"<b>Стало:</b>\n{safe_text(new_body, 1000)}"
            )
        except Exception:
            logging.exception("Не вдалося повідомити про редагування")


@dp.deleted_business_messages()
async def deleted_business_messages(event, bot: Bot):
    connection_id = event.business_connection_id
    logging.info(
        "ESIM_DIAG deleted_business_messages RECEIVED: connection=%s chat=%s message_ids=%s",
        connection_id, getattr(event.chat, "id", None), getattr(event, "message_ids", None)
    )
    connection = await db.get_connection(connection_id)
    if not connection or not connection[1]:
        logging.warning("ESIM_DIAG deleted_business_messages SKIPPED: connection not found/disabled id=%s row=%s", connection_id, connection)
        return
    owner_id = connection[0]
    for message_id in event.message_ids:
        record = await db.get_message(owner_id, connection_id, event.chat.id, message_id)
        logging.info(
            "ESIM_DIAG DELETE_LOOKUP: db_file=%s owner=%s connection=%s chat=%s message=%s found=%s",
            getattr(db, "DB_PATH", "unknown"), owner_id, connection_id, event.chat.id,
            message_id, bool(record)
        )
        if not record:
            logging.warning(
                "ESIM_DIAG deleted_business_messages: no saved record owner=%s connection=%s chat=%s message=%s",
                owner_id, connection_id, event.chat.id, message_id
            )
            continue
        title, sender, body, created_at, _, media_type, media_file_id, _sender_id = record
        try:
            inserted = await db.save_deleted(
                owner_id, connection_id, event.chat.id, message_id,
                title, sender, body, created_at, media_type, media_file_id
            )
            logging.info(
                "ESIM_DIAG SAVE_DELETED RESULT: inserted=%s owner=%s connection=%s chat=%s message=%s",
                inserted, owner_id, connection_id, event.chat.id, message_id
            )
        except Exception:
            logging.exception(
                "ESIM_DIAG SAVE_DELETED FAILED: owner=%s connection=%s chat=%s message=%s",
                owner_id, connection_id, event.chat.id, message_id
            )
            continue

        # Notify even if the archive row already existed: an earlier attempt may have
        # saved the row but failed before the owner received the notification.
        try:
            blocked = await db.is_muted(owner_id, connection_id, event.chat.id)
            logging.info(
                "ESIM_DIAG NOTIFY_DELETED START: owner=%s connection=%s chat=%s message=%s blocked=%s",
                owner_id, connection_id, event.chat.id, message_id, blocked
            )
            await notify_deleted(
                bot, owner_id, connection_id, event.chat.id, title, sender, body,
                created_at, media_type, media_file_id, blocked=blocked
            )
        except Exception:
            logging.exception(
                "ESIM_DIAG NOTIFY_DELETED CALL FAILED: owner=%s connection=%s chat=%s message=%s",
                owner_id, connection_id, event.chat.id, message_id
            )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не заданий. Перевір файл .env.")
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=[
        "message", "callback_query", "business_connection", "business_message",
        "edited_business_message", "deleted_business_messages",
    ])


if __name__ == "__main__":
    asyncio.run(main())
