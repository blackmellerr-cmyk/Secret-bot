import asyncio
import logging
import os
import re
import sqlite3
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

# Токен из переменных окружения Render
TOKEN = os.getenv("TOKEN")

# Включаем логирование
logging.basicConfig(level=logging.INFO)
router = Router()

# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db():
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            user_id INTEGER PRIMARY KEY,
            channel_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_id TEXT,
            message_id INTEGER,
            secret_type TEXT,
            base_text TEXT,
            is_expired BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Состояния FSM
class SecretForm(StatesGroup):
    waiting_for_type = State()
    waiting_for_photo = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

# Словарь для склонения типов секреток
SECRET_DECLINE = {
    "Лапка": "лапки",
    "Сердечко": "сердечка",
    "Тропы": "троп",
}

# Главная Reply-клавиатура
def get_main_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Отправить секретку")],
            [KeyboardButton(text="📋 Мои посты")]
        ],
        resize_keyboard=True
    )

# Inline-клавиатура выбора типа секретки
def get_types_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐾 Лапка", callback_data="type_Лапка")],
            [InlineKeyboardButton(text="💖 Сердечко", callback_data="type_Сердечко")],
            [InlineKeyboardButton(text="🌴 Тропы", callback_data="type_Тропы")],
        ]
    )

# Inline-клавиатура подтверждения
def get_confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="confirm_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="confirm_no"),
            ]
        ]
    )

# Функция изменения поста на статус "Время вышло"
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

# Фоновый таймер на 8 минут 30 секунд (510 секунд)
async def timer_task(bot: Bot, channel_id: str, message_id: int, secret_type: str, post_db_id: int):
    await asyncio.sleep(510)
    
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_expired FROM posts WHERE id = ?", (post_db_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == 0:
        await expire_post(bot, channel_id, message_id, secret_type, post_db_id)

# Динамическая проверка прав пользователя и бота
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


# ==================== ОБРАБОТЧИКИ КОМАНД И КНОПОК ====================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "Привет, ты попал в бота админа для публикации постов с секретками, "
        "для начала использования бота используй /addchannel @юз_канала!"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())


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
            reply_markup=get_main_reply_keyboard()
        )

    except TelegramBadRequest:
        await message.answer("❌ Не удалось найти канал. Убедись, что бот добавлен в канал и указан правильный юзернейм.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(F.text == "📥 Отправить секретку")
async def btn_send_secret(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    channel_id = await check_user_and_bot_rights(bot, user_id)

    if not channel_id:
        await message.answer(
            "❌ Канал не привязан или у тебя больше нет прав администратора!\n"
            "Используй команду `/addchannel @юз_канала`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
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
            reply_markup=ReplyKeyboardRemove()
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
    secret_type = callback.data.split("_")[1]
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

    if not clean_text.startswith("https://www.roblox.com/share?code=") or not clean_text.endswith("type=Server"):
        await message.answer("Отправьте ссылку именно на вип сервер.")
        return

    link = clean_text
    await state.update_data(link=link)

    data = await state.get_data()
    secret_type = data["secret_type"]
    photo_id = data["photo_id"]
    declined_type = SECRET_DECLINE.get(secret_type, secret_type)

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
        declined_type = SECRET_DECLINE.get(secret_type, secret_type)

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
    # Запускаем бота асинхронно вместе с FastAPI
    asyncio.create_task(start_telegram_bot())

if __name__ == "__main__":
    # Render передает порт через системную переменную PORT, по умолчанию ставим 8080
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
