import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Message,
)
from fastapi import FastAPI
import uvicorn

# Токен и ID администратора из переменных окружения (скрыты от посторонних)
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Включаем логирование
logging.basicConfig(level=logging.INFO)
router = Router()

# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db():
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    
    # Таблица привязанных каналов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            user_id INTEGER PRIMARY KEY,
            channel_id TEXT
        )
    """)
    
    # Таблица постов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_id TEXT,
            message_id INTEGER,
            secret_type TEXT,
            base_text TEXT,
            is_expired BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица типов секреток
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS secret_types (
            name TEXT PRIMARY KEY,
            declined TEXT
        )
    """)
    
    # Таблица настроек
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    default_types = [
        ("Лапка", "лапки"),
        ("Сердечко", "сердечка"),
        ("Тропы", "троп")
    ]
    cursor.executemany("INSERT OR IGNORE INTO secret_types (name, declined) VALUES (?, ?)", default_types)
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('timer_seconds', '510')")
    
    conn.commit()
    conn.close()

init_db()

def get_timer_duration() -> int:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'timer_seconds'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 510

def get_secret_types_dict() -> dict:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, declined FROM secret_types")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

# Состояния FSM
class SecretForm(StatesGroup):
    waiting_for_type = State()
    waiting_for_photo = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class AdminStates(StatesGroup):
    waiting_for_new_type_name = State()
    waiting_for_new_type_declined = State()
    waiting_for_new_timer = State()

# Клавиатуры
def get_main_reply_keyboard(user_id: int):
    keyboard = [
        [KeyboardButton(text="📥 Отправить секретку")],
        [KeyboardButton(text="📋 Мои посты")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика за день", callback_data="adm_stats")],
            [InlineKeyboardButton(text="➕ Добавить тип секретки", callback_data="adm_add_type")],
            [InlineKeyboardButton(text="🗑 Удалить тип секретки", callback_data="adm_del_type")],
            [InlineKeyboardButton(text="⏱ Изменить время таймера", callback_data="adm_set_timer")],
            [InlineKeyboardButton(text="🔙 Выход", callback_data="adm_exit")],
        ]
    )

def get_types_keyboard():
    types_dict = get_secret_types_dict()
    buttons = []
    for s_name in types_dict.keys():
        buttons.append([InlineKeyboardButton(text=f"🔹 {s_name}", callback_data=f"type_{s_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="confirm_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="confirm_no"),
            ]
        ]
    )

async def expire_post(bot: Bot, channel_id: str, message_id: int, secret_type: str, post_db_id: int):
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE posts SET is_expired = 1 WHERE id = ?", (post_db_id,))
    conn.commit()
    conn.close()

    expired_text = (
        f"❕Секретка❕\n"
        f"Секретка: {secret_type}\n\n"
        f"Секретка: Время вышло! В канале еще будут секретки и вы успеете попасть на них🤍\n\n"
        f"🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | "
        f"Наш <a href='https://t.me/ToHSecretss'>канал</a> | "
        f"Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
    )

    try:
        await bot.edit_message_caption(
            chat_id=channel_id,
            message_id=message_id,
            caption=expired_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось обновить пост: {e}")

async def timer_task(bot: Bot, channel_id: str, message_id: int, secret_type: str, post_db_id: int):
    duration = get_timer_duration()
    await asyncio.sleep(duration)
    
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_expired FROM posts WHERE id = ?", (post_db_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == 0:
        await expire_post(bot, channel_id, message_id, secret_type, post_db_id)

async def check_user_and_bot_rights(bot: Bot, user_id: int) -> str | None:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM channels WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    channel_id = row[0]

    try:
        bot_member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            return None

        user_member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            conn = sqlite3.connect("bot.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channels WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return None

        return channel_id
    except Exception:
        return None


# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "Привет, ты попал в бота админа для публикации постов с секретками, "
        "для начала использования бота используй /addchannel @юз_канала!"
    )
    await message.answer(welcome_text, reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.message(Command("addchannel"))
async def cmd_add_channel(message: Message, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Укажи канал после команды.\nПример: `/addchannel @my_channel`", parse_mode="Markdown")
        return

    channel_input = args[1].strip()
    user_id = message.from_user.id

    try:
        bot_member = await bot.get_chat_member(chat_id=channel_input, user_id=bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await message.answer("❌ Бот не является администратором в этом канале! Добавь его с правом публикации сообщений.")
            return

        user_member = await bot.get_chat_member(chat_id=channel_input, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            await message.answer("❌ Ты не являешься администратором или создателем этого канала, поэтому не можешь его привязать!")
            return

        conn = sqlite3.connect("bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO channels (user_id, channel_id) VALUES (?, ?)", 
                       (user_id, channel_input))
        conn.commit()
        conn.close()

        await message.answer(
            f"✅ Канал <b>{channel_input}</b> успешно привязан!\nТеперь тебе доступно меню снизу.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard(user_id)
        )

    except TelegramBadRequest:
        await message.answer("❌ Не удалось найти канал. Убедись, что бот добавлен в канал и указан правильный юзернейм.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ==================== АДМИН-ПАНЕЛЬ И СТАТИСТИКА ====================

@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    current_timer = get_timer_duration()
    minutes = current_timer // 60
    seconds = current_timer % 60
    
    text = (
        f"⚙️ <b>Панель управления администратора</b>\n\n"
        f"⏱ Время таймера до удаления ссылки: <b>{minutes} мин. {seconds} сек.</b>\n"
        f"Выберите действие:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_admin_keyboard())


@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    yesterday = datetime.now() - timedelta(days=1)
    
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, COUNT(*) as post_count 
        FROM posts 
        WHERE created_at >= ? 
        GROUP BY user_id
    """, (yesterday.strftime('%Y-%m-%d %H:%M:%S'),))
    user_stats = cursor.fetchall()

    cursor.execute("""
        SELECT user_id, channel_id, message_id, secret_type, created_at 
        FROM posts 
        WHERE created_at >= ? 
        ORDER BY created_at DESC
    """, (yesterday.strftime('%Y-%m-%d %H:%M:%S'),))
    all_posts = cursor.fetchall()
    conn.close()

    if not user_stats:
        await callback.message.edit_text(
            "📊 <b>Статистика за последние 24 часа:</b>\n\n📭 За это время не было опубликовано ни одной секретки.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]])
        )
        await callback.answer()
        return

    stats_text = "📊 <b>Статистика постов за последние 24 часа:</b>\n\n"
    
    stats_text += "<b>👤 По количеству от администраторов:</b>\n"
    for u_id, count in user_stats:
        stats_text += f"• ID <code>{u_id}</code>: <b>{count}</b> пост(ов)\n"
    
    stats_text += "\n<b>📋 Список всех постов:</b>\n"
    for u_id, ch_id, msg_id, s_type, dt in all_posts:
        clean_ch = ch_id.replace("@", "")
        if clean_ch.startswith("https://t.me/"):
            post_link = f"{clean_ch}/{msg_id}"
        else:
            post_link = f"https://t.me/{clean_ch}/{msg_id}"
            
        stats_text += f"• [{s_type}] Администратор <code>{u_id}</code> — <a href='{post_link}'>Открыть пост</a> ({dt})\n"

    if len(stats_text) > 4000:
        stats_text = stats_text[:3950] + "\n\n... (список сокращен из-за лимита длины)"

    await callback.message.edit_text(
        stats_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]])
    )
    await callback.answer()


@router.callback_query(F.data == "adm_exit")
async def adm_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Выход из админ-панели.")
    await callback.answer()


@router.callback_query(F.data == "adm_set_timer")
async def adm_set_timer(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "⏱ Введите новое время таймера **в секундах** (например, `510` для 8.5 минут):",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_for_new_timer)
    await callback.answer()


@router.message(AdminStates.waiting_for_new_timer, F.text)
async def process_new_timer(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    
    if not message.text.isdigit():
        await message.answer("❌ Введите число (количество секунд). Попробуйте снова:")
        return

    new_seconds = int(message.text)
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'timer_seconds'", (str(new_seconds),))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(f"✅ Время таймера успешно изменено на {new_seconds} секунд!", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "adm_add_type")
async def adm_add_type(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text("➕ Введите название нового типа секретки (например, <code>Звезда</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_new_type_name)
    await callback.answer()


@router.message(AdminStates.waiting_for_new_type_name, F.text)
async def process_new_type_name(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    type_name = message.text.strip()
    await state.update_data(new_type_name=type_name)

    await message.answer(
        f"Теперь введите форму для правила склонения (например, для 'Звезда' родительный падеж — <code>звезды</code>):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_new_type_declined)


@router.message(AdminStates.waiting_for_new_type_declined, F.text)
async def process_new_type_declined(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    declined_form = message.text.strip()
    data = await state.get_data()
    type_name = data["new_type_name"]

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO secret_types (name, declined) VALUES (?, ?)", (type_name, declined_form))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(f"✅ Тип секретки <b>{type_name}</b> успешно добавлен!", parse_mode="HTML", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "adm_del_type")
async def adm_del_type(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    types_dict = get_secret_types_dict()
    if not types_dict:
        await callback.message.edit_text("📭 Нет доступных типов секреток для удаления.")
        await callback.answer()
        return

    buttons = []
    for s_name in types_dict.keys():
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить: {s_name}", callback_data=f"deltype_{s_name}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])

    await callback.message.edit_text("🗑 Выберите тип секретки для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("deltype_"))
async def process_delete_type(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    type_to_del = callback.data.split("_", 1)[1]

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM secret_types WHERE name = ?", (type_to_del,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Тип секретки <b>{type_to_del}</b> удален.", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    current_timer = get_timer_duration()
    minutes = current_timer // 60
    seconds = current_timer % 60
    text = (
        f"⚙️ <b>Панель управления администратора</b>\n\n"
        f"⏱ Время таймера до удаления ссылки: <b>{minutes} мин. {seconds} сек.</b>\n"
        f"Выберите действие:"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_admin_keyboard())
    await callback.answer()


# ==================== РАБОТА С ПОСТАМИ ====================

@router.message(F.text == "📥 Отправить секретку")
async def btn_send_secret(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    channel_id = await check_user_and_bot_rights(bot, user_id)

    if not channel_id:
        await message.answer(
            "❌ Канал не привязан или у тебя больше нет прав администратора!\n"
            "Используй команду `/addchannel @юз_канала`",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(user_id)
        )
        return

    types_dict = get_secret_types_dict()
    if not types_dict:
        await message.answer("❌ В базе данных нет доступных типов секреток. Добавь их через админ-панель.")
        return

    await state.clear()
    await message.answer("Выбери тип секретки:", reply_markup=get_types_keyboard())
    await state.set_state(SecretForm.waiting_for_type)


@router.message(F.text == "📋 Мои посты")
async def btn_my_posts(message: Message, bot: Bot):
    user_id = message.from_user.id
    channel_id = await check_user_and_bot_rights(bot, user_id)
    if not channel_id:
        await message.answer(
            "❌ Сначала привяжи канал через `/addchannel @юз_канала`",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(user_id)
        )
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, secret_type, message_id FROM posts WHERE user_id = ? AND is_expired = 0", (user_id,))
    posts = cursor.fetchall()
    conn.close()

    if not posts:
        await message.answer("📭 У тебя нет активных опубликованных постов.")
        return

    keyboard_buttons = []
    for post in posts:
        post_id, s_type, msg_id = post
        keyboard_buttons.append([InlineKeyboardButton(text=f"📌 {s_type} (ID поста: {msg_id})", callback_data=f"expire_{post_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("📋 Твои активные посты:\nНажми на пост, чтобы досрочно завершить его (удалить ссылку):", reply_markup=keyboard)


@router.callback_query(F.data.startswith("expire_"))
async def process_early_expire(callback: CallbackQuery, bot: Bot):
    post_db_id = int(callback.data.split("_")[1])

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message_id, secret_type, is_expired FROM posts WHERE id = ?", (post_db_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ Пост не найден.", show_alert=True)
        return

    channel_id, message_id, secret_type, is_expired = row

    if is_expired:
        await callback.answer("⚠️ Этот пост уже завершен.", show_alert=True)
        return

    await expire_post(bot, channel_id, message_id, secret_type, post_db_id)
    await callback.message.edit_text("✅ Пост успешно закрыт досрочно (ссылка удалена в канале).")
    await callback.answer()


@router.callback_query(SecretForm.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    secret_type = callback.data.split("_", 1)[1]
    await state.update_data(secret_type=secret_type)

    await callback.message.edit_text(
        f"Выбрано: <b>{secret_type}</b>\n\nТеперь отправь <b>фотографию</b> секретки:",
        parse_mode="HTML",
    )
    await state.set_state(SecretForm.waiting_for_photo)
    await callback.answer()


@router.message(SecretForm.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)

    await message.answer("Отлично! Теперь отправь **ссылку**:")
    await state.set_state(SecretForm.waiting_for_link)

@router.message(SecretForm.waiting_for_photo)
async def process_photo_invalid(message: Message):
    await message.answer("Пожалуйста, отправь именно **фотографию**.")


@router.message(SecretForm.waiting_for_link, F.text)
async def process_link(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    clean_text = re.sub(r'\s+', '', raw_text)

    if "roblox.com/share?code=" not in clean_text or not clean_text.endswith("type=Server"):
        await message.answer("Отправьте ссылку именно на вип сервер.")
        return

    link = clean_text
    await state.update_data(link=link)

    data = await state.get_data()
    secret_type = data["secret_type"]
    photo_id = data["photo_id"]
    
    types_dict = get_secret_types_dict()
    declined_type = types_dict.get(secret_type, secret_type)

    preview_text = (
        f"❕Секретка❕\n"
        f"Секретка: {secret_type}\n\n"
        f"Правила:\n"
        f"1. Не ускорять\n"
        f"2. Выйти с сервера после получения {declined_type}\n"
        f"3. Не покупать негативные мутаторы\n"
        f"4. Не подниматься выше уровня над секреткой и не идти к воротам ускорения\n"
        f"При несоблюдении правил, вы получите бан.\n"
        f"Обжаловать бан можно в <a href='https://t.me/ToHSecrets_bot'>поддержке</a>!\n\n"
        f"Секретка: {link}\n\n"
        f"🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | Наш <a href='https://t.me/ToHSecretss'>канал</a> | Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
    )

    await message.answer("Вот как будет выглядеть твой пост:")
    await message.answer_photo(
        photo=photo_id,
        caption=preview_text,
        parse_mode="HTML",
        reply_markup=get_confirm_keyboard(),
    )
    await state.set_state(SecretForm.waiting_for_confirm)

@router.message(SecretForm.waiting_for_link)
async def process_link_invalid(message: Message):
    await message.answer("Пожалуйста, отправь ссылку текстом.")


@router.callback_query(SecretForm.waiting_for_confirm, F.data.startswith("confirm_"))
async def process_confirmation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split("_")[1]
    user_id = callback.from_user.id

    if action == "yes":
        channel_id = await check_user_and_bot_rights(bot, user_id)
        if not channel_id:
            await callback.message.edit_caption(
                caption="❌ Ошибка: канал не привязан или у тебя нет прав администратора!", reply_markup=None
            )
            await state.clear()
            await callback.answer()
            return

        data = await state.get_data()
        secret_type = data["secret_type"]
        photo_id = data["photo_id"]
        link = data["link"]
        
        types_dict = get_secret_types_dict()
        declined_type = types_dict.get(secret_type, secret_type)

        final_text = (
            f"❕Секретка❕\n"
            f"Секретка: {secret_type}\n\n"
            f"Правила:\n"
            f"1. Не ускорять\n"
            f"2. Выйти с сервера после получения {declined_type}\n"
            f"3. Не покупать негативные мутаторы\n"
            f"4. Не подниматься выше уровня над секреткой и не идти к воротам ускорения\n"
            f"При несоблюдении правил, вы получите бан.\n"
            f"Обжаловать бан можно в <a href='https://t.me/ToHSecrets_bot'>поддержке</a>!\n\n"
            f"Секретка: {link}\n\n"
            f"🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | Наш <a href='https://t.me/ToHSecretss'>канал</a> | Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
        )

        try:
            sent_msg = await bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=final_text,
                parse_mode="HTML",
            )

            conn = sqlite3.connect("bot.db")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO posts (user_id, channel_id, message_id, secret_type, base_text, is_expired)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (user_id, channel_id, sent_msg.message_id, secret_type, final_text))
            post_db_id = cursor.lastrowid
            conn.commit()
            conn.close()

            asyncio.create_task(timer_task(bot, channel_id, sent_msg.message_id, secret_type, post_db_id))

            await callback.message.edit_caption(
                caption="✅ Успешно отправлено в канал!", reply_markup=None
            )
        except Exception as e:
            await callback.message.edit_caption(
                caption=f"❌ Ошибка при отправке в канал: {e}", reply_markup=None
            )
    else:
        await callback.message.edit_caption(
            caption="❌ Отменено.", reply_markup=None
        )

    await state.clear()
    await callback.answer()


# ==================== НАСТРОЙКА FASTAPI И ЗАПУСК ====================

app = FastAPI()

@app.get("/")
def index():
    return {"status": "Bot is alive!"}

async def start_telegram_bot():
    if not TOKEN:
        print("❌ ОШИБКА: Не задан токен бота! Укажи переменную окружения TOKEN.")
        return

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Бот запущен через polling в фоновом режиме...")
    await dp.start_polling(bot)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
