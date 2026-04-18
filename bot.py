"""
ЦифроЮля — Telegram-бот для диагностики контента и генерации сценариев Reels.
"""

import asyncio
import logging
import os
import random
import tempfile
import time

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from openai import OpenAI, AsyncOpenAI

from config import (
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY,
    TRIBUTE_PRODUCT_LINK, PORT, RAILWAY_PUBLIC_DOMAIN,
)

JULIA_TG = "https://t.me/JFilipenko"
from prompts import (
    DIAGNOSIS_TIPS_PROMPT,
    SCENARIO_SYSTEM_PROMPT,
    NEWS_SEARCH_PROMPT,
    REELS_STYLES,
    AUDIENCE_TARGETS,
    DURATIONS,
    build_scenario_prompt,
)
from db import init_db, close_db, check_access, get_access_until
from webhook_server import create_webhook_app

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)
async_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ── Состояния диалога ──────────────────────────────────────────────
(
    ASK_NICHE,
    ASK_PRODUCT,
    ASK_AUDIENCE,
    SHOW_TIPS,
    MAIN_MENU,
    SCENARIO_INPUT_CHOICE,
    SCENARIO_TEXT_INPUT,
    SCENARIO_VOICE_INPUT,
    CHOOSE_STYLE,
    CHOOSE_NEWS,
    CHOOSE_TARGET,
    CHOOSE_DURATION,
    SHOW_SCENARIO,
) = range(13)

# ── Хранилище данных юзеров (in-memory) ────────────────────────────
user_data_store: dict[int, dict] = {}


def get_user(user_id: int) -> dict:
    if user_id not in user_data_store:
        user_data_store[user_id] = {
            "product": "",
            "niche": "",
            "audience": "",
            "user_input": None,
            "settings": {"style": "", "target": "", "duration": ""},
            "news_list": [],
        }
    return user_data_store[user_id]


# ── Вспомогательные функции ─────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Сгенерировать сценарий Reels", callback_data="generate_scenario")],
        [InlineKeyboardButton("🎓 Посмотреть программу курса", callback_data="show_course")],
        [InlineKeyboardButton("📋 Запись на консультацию — 200$", url=JULIA_TG)],
        [InlineKeyboardButton("🎤 Попасть на разбор — 100$", url=JULIA_TG)],
        [InlineKeyboardButton("🔄 Пройти диагностику заново", callback_data="restart_diagnosis")],
    ])


def after_scenario_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сгенерировать заново", callback_data="regenerate")],
        [InlineKeyboardButton("⚙️ Изменить настройки", callback_data="change_settings")],
        [InlineKeyboardButton("🎓 Посмотреть программу курса", callback_data="show_course")],
        [InlineKeyboardButton("📋 Запись на консультацию — 200$", url=JULIA_TG)],
        [InlineKeyboardButton("🎤 Попасть на разбор — 100$", url=JULIA_TG)],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])


def style_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key, style in REELS_STYLES.items():
        buttons.append([InlineKeyboardButton(
            f"{style['name']}", callback_data=f"style_{key}"
        )])
    return InlineKeyboardMarkup(buttons)


def target_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key, desc in AUDIENCE_TARGETS.items():
        buttons.append([InlineKeyboardButton(desc, callback_data=f"target_{key}")])
    return InlineKeyboardMarkup(buttons)


def duration_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key, desc in DURATIONS.items():
        buttons.append([InlineKeyboardButton(desc, callback_data=f"dur_{key}")])
    return InlineKeyboardMarkup(buttons)


async def safe_send(target, text: str, reply_markup=None, parse_mode="Markdown"):
    """Отправляет сообщение, разбивая на части если > 4000 символов."""
    if len(text) <= 4000:
        await target.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            await target.reply_text(part, parse_mode=parse_mode)
        else:
            await target.reply_text(part, parse_mode=parse_mode, reply_markup=reply_markup)


async def safe_send_bot(bot, chat_id: int, text: str, reply_markup=None, parse_mode="Markdown"):
    """Отправляет через bot.send_message, разбивая на части."""
    if len(text) <= 4000:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode=parse_mode)
        else:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode=parse_mode, reply_markup=reply_markup)


def clean_md_for_telegram(text: str) -> str:
    """Убирает из GPT-ответа Markdown-элементы, которые Telegram не поддерживает."""
    import re
    # Убираем ### ## # заголовки → просто жирный текст
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Убираем горизонтальные линии ---
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    # Убираем ``` блоки кода (оставляем содержимое)
    text = re.sub(r'```[a-z]*\n?', '', text)
    return text.strip()


def strip_followup(text: str) -> str:
    """Удаляет follow-up предложения GPT в конце ответа."""
    paragraphs = text.rstrip().split('\n\n')
    if len(paragraphs) < 2:
        return text

    followup_starters = (
        'хочешь', 'если хочешь', 'если хотите', 'могу также', 'могу ещё',
        'давай также', 'давайте также', 'нужна помощь', 'готов помочь',
        'готова помочь', 'обращайся', 'обращайтесь', 'напиши ', 'напишите ',
        'если нужно', 'если нужна', 'желаешь', 'хотите', 'могу помочь',
        'буду рад', 'буду рада',
    )

    while len(paragraphs) > 1:
        last = paragraphs[-1].strip().lstrip('-').strip()
        if any(last.lower().startswith(s) for s in followup_starters):
            paragraphs.pop()
        else:
            break

    return '\n\n'.join(paragraphs).strip()


async def call_ai(system_prompt: str, user_prompt: str) -> str:
    """Вызов OpenAI для генерации ответа."""
    try:
        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=2000,
            temperature=0.8,
        )
        result = response.choices[0].message.content or "Не удалось сгенерировать ответ."
        return clean_md_for_telegram(strip_followup(result))
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return "⚠️ Произошла ошибка при генерации. Попробуйте ещё раз."


async def stream_ai_to_chat(bot, chat_id: int, system_prompt: str, user_prompt: str) -> str:
    """Стримит ответ GPT в чат через sendMessageDraft, возвращает финальный текст."""
    draft_id = random.randint(1, 2**31 - 1)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessageDraft"

    try:
        stream = await async_client.chat.completions.create(
            model="gpt-5.4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=2000,
            temperature=0.8,
            stream=True,
        )

        accumulated = ""
        last_update_time = 0.0

        async with aiohttp.ClientSession() as session:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    accumulated += chunk.choices[0].delta.content

                    now = time.time()
                    if now - last_update_time >= 0.5 and len(accumulated) > 20:
                        try:
                            await session.post(api_url, json={
                                "chat_id": chat_id,
                                "draft_id": draft_id,
                                "text": accumulated[:4096],
                            })
                            last_update_time = now
                        except Exception as e:
                            logger.debug(f"Draft update failed: {e}")

        if not accumulated:
            return "Не удалось сгенерировать ответ."

        return clean_md_for_telegram(strip_followup(accumulated))

    except Exception as e:
        logger.error(f"OpenAI streaming error: {e}")
        # Фоллбэк на обычный вызов без стриминга
        return await call_ai(system_prompt, user_prompt)


async def search_news(user_prompt: str) -> str:
    """Поиск реальных новостей через модель с веб-поиском."""
    try:
        response = client.responses.create(
            model="gpt-5.4",
            input=[
                {"role": "system", "content": NEWS_SEARCH_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tools=[{"type": "web_search"}],
        )
        return response.output_text or "Не удалось найти новости."
    except Exception as e:
        logger.error(f"News search error: {e}")
        return "⚠️ Не удалось найти новости. Попробуйте ещё раз."

def parse_news_items(text: str) -> list[str]:
    """Разбивает текст новостей на отдельные пункты по нумерации 1. 2. 3."""
    import re
    items = re.split(r'\n(?=\d+\.\s)', text.strip())
    items = [item.strip() for item in items if item.strip()]
    return items if items else [text.strip()]

async def transcribe_voice(file_path: str) -> str:
    """Транскрибация голосового сообщения через Whisper."""
    try:
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )
        return transcript.text
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return ""


# ── Хендлеры ────────────────────────────────────────────────────────
def payment_keyboard() -> InlineKeyboardMarkup:
    """Keyboard с кнопкой оплаты."""
    pay_url = TRIBUTE_PRODUCT_LINK or JULIA_TG
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить доступ", url=pay_url)],
    ])


async def require_access(update: Update) -> bool:
    """Проверяет доступ юзера. Возвращает True если доступ есть."""
    user_id = update.effective_user.id
    username = update.effective_user.username
    has_access = await check_access(user_id, username)
    if not has_access:
        text = (
            "⚠️ **Доступ не активен**\n\n"
            "Чтобы пользоваться ботом, оплати доступ по кнопке ниже 👇\n"
            "После оплаты напиши /start чтобы начать."
        )
        if update.message:
            await update.message.reply_text(
                text, parse_mode="Markdown", reply_markup=payment_keyboard(),
            )
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=payment_keyboard(),
            )
        return False
    return True

async def start(update: Update, context) -> int:
    """Приветствие и начало диагностики."""
    if not await require_access(update):
        return ConversationHandler.END

    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Я — бот ЦифроЮли 🎬\n\n"
        "Помогу тебе:\n"
        "• Разобраться, какой контент снимать для твоей ниши\n"
        "• Получить советы по визуалу, хукам и смыслам\n"
        "• Сгенерировать готовый сценарий Reels\n\n"
        "Для начала давай проведём быструю диагностику 🔍\n\n"
        "**В какой ты нише?**\n"
        "Например: маркетинг, фитнес, психология, бьюти, коучинг, e-commerce...",
        parse_mode="Markdown",
    )
    return ASK_NICHE


async def ask_niche(update: Update, context) -> int:
    """Получаем нишу, спрашиваем продукт."""
    ud = get_user(update.effective_user.id)
    ud["niche"] = update.message.text

    await update.message.reply_text(
        "Отлично! 👍\n\n"
        "**Что ты продаёшь?** Расскажи о своём продукте или услуге.",
        parse_mode="Markdown",
    )
    return ASK_PRODUCT


async def ask_product(update: Update, context) -> int:
    """Получаем продукт, спрашиваем ЦА."""
    ud = get_user(update.effective_user.id)
    ud["product"] = update.message.text

    await update.message.reply_text(
        "Понял! 🎯\n\n"
        "**Кто твоя целевая аудитория?**\n"
        "Опиши кому ты продаёшь: кто эти люди, какие у них боли и потребности.",
        parse_mode="Markdown",
    )
    return ASK_AUDIENCE


async def ask_audience(update: Update, context) -> int:
    """Получаем ЦА, генерируем советы."""
    ud = get_user(update.effective_user.id)
    ud["audience"] = update.message.text

    await update.message.reply_text("⏳ Анализирую твою нишу и готовлю персональные советы...")

    user_prompt = (
        f"Продукт/услуга: {ud['product']}\n"
        f"Ниша: {ud['niche']}\n"
        f"Целевая аудитория: {ud['audience']}"
    )

    tips = await stream_ai_to_chat(
        context.bot, update.effective_user.id,
        DIAGNOSIS_TIPS_PROMPT, user_prompt,
    )

    await safe_send_bot(
        context.bot, update.effective_user.id,
        f"🔍 **Результаты диагностики:**\n\n{tips}",
    )

    await update.message.reply_text(
        "Что хочешь сделать дальше? 👇",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def main_menu_handler(update: Update, context) -> int:
    """Обработка кнопок главного меню."""
    query = update.callback_query
    await query.answer()

    if query.data == "show_course":
        await query.edit_message_text(
            "🎬💋 **Снимите это немедленно!** — курс по Reels, который реально приносит клиентов\n\n"
            "За 4 недели ты пройдёшь путь от «снимаю хаотично» до системы, которая работает на тебя 24/7:\n\n"
            "✅ Разберёшься в алгоритмах Instagram и прогреешь аккаунт правильно\n\n"
            "✅ Найдёшь свой архетип и поймёшь, КАК говорить с аудиторией — так, чтобы покупали\n\n"
            "✅ Получишь готовые промты для ChatGPT: идеи, сценарии, хуки, CTA — за минуты\n\n"
            "✅ Освоишь 34 формата Reels и научишься балансировать охваты, экспертность и продажи\n\n"
            "✅ Узнаешь, как собирать лиды прямо из роликов — через кодовые слова, автоворонки и ботов\n\n"
            "✅ Получишь модуль по монтажу от эксперта: свет, цвет, субтитры, ИИ-инструменты — 7 уроков\n\n"
            "✅ 4 живых созвона с Юлей — задашь вопросы и разберёшь свои ролики\n\n"
            "💡 Реальный кейс: $1500 с 3000 просмотров. Не магия — система.\n\n"
            "🏆 Бонус: челлендж «30 Reels за 30 дней» с призами — консультация, разбор аккаунта, доступ в закрытый клуб.\n\n"
            "Хватит кричать в подушку — пора, чтобы тебя услышали! 🚀",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Написать Юле чтобы занять место", url=JULIA_TG)],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]),
        )
        return MAIN_MENU

    elif query.data == "generate_scenario":
        await query.edit_message_text(
            "🎬 **Генерация сценария Reels**\n\n"
            "У тебя уже есть задумка или идея для ролика?\n\n"
            "Выбери вариант 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Напишу текстом", callback_data="input_text")],
                [InlineKeyboardButton("🎤 Надиктую голосом", callback_data="input_voice")],
                [InlineKeyboardButton("🚀 Сгенерировать с нуля", callback_data="input_skip")],
            ]),
        )
        return SCENARIO_INPUT_CHOICE

    elif query.data == "restart_diagnosis":
        ud = get_user(query.from_user.id)
        ud["product"] = ""
        ud["niche"] = ""
        ud["audience"] = ""
        ud["user_input"] = None

        await query.edit_message_text(
            "🔄 Начинаем диагностику заново!\n\n"
            "**В какой ты нише?**\n"
            "Например: маркетинг, фитнес, психология, бьюти, коучинг, e-commerce...",
            parse_mode="Markdown",
        )
        return ASK_NICHE

    elif query.data == "main_menu":
        await query.edit_message_text(
            "Что хочешь сделать? 👇",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU


async def scenario_input_choice(update: Update, context) -> int:
    """Выбор способа ввода идеи."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    if query.data == "input_text":
        await query.edit_message_text(
            "✍️ Напиши свою задумку, идею или готовый сценарий.\n"
            "Я возьму это за основу и сделаю профессиональный сценарий.",
        )
        return SCENARIO_TEXT_INPUT

    elif query.data == "input_voice":
        await query.edit_message_text(
            "🎤 Надиктуй голосовое сообщение со своей задумкой.\n"
            "Я распознаю речь и использую как основу для сценария.",
        )
        return SCENARIO_VOICE_INPUT

    elif query.data == "input_skip":
        ud["user_input"] = None
        await query.edit_message_text(
            "🎨 **Выбери стиль Reels:**\n\n"
            "Какой формат тебе ближе? 👇",
            parse_mode="Markdown",
            reply_markup=style_keyboard(),
        )
        return CHOOSE_STYLE


async def receive_text_input(update: Update, context) -> int:
    """Получаем текстовую задумку юзера."""
    ud = get_user(update.effective_user.id)
    ud["user_input"] = update.message.text

    await update.message.reply_text(
        "✅ Принял!\n\n"
        "🎨 **Выбери стиль Reels:**\n\n"
        "Какой формат тебе ближе? 👇",
        parse_mode="Markdown",
        reply_markup=style_keyboard(),
    )
    return CHOOSE_STYLE


async def receive_voice_input(update: Update, context) -> int:
    """Получаем голосовое, транскрибируем."""
    ud = get_user(update.effective_user.id)

    await update.message.reply_text("⏳ Распознаю голосовое сообщение...")

    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text(
            "Не вижу голосового сообщения. Попробуй ещё раз или напиши текстом.",
        )
        return SCENARIO_VOICE_INPUT

    file = await voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

    try:
        text = await transcribe_voice(tmp_path)
        if not text:
            await update.message.reply_text(
                "Не удалось распознать голос. Попробуй ещё раз или напиши текстом."
            )
            return SCENARIO_VOICE_INPUT

        ud["user_input"] = text
        await update.message.reply_text(
            f"✅ Распознал:\n\n_{text}_\n\n"
            "🎨 **Выбери стиль Reels:** 👇",
            parse_mode="Markdown",
            reply_markup=style_keyboard(),
        )
        return CHOOSE_STYLE
    finally:
        os.unlink(tmp_path)


async def choose_style(update: Update, context) -> int:
    """Выбор стиля из Карты Форматов."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    style_key = query.data.replace("style_", "")
    ud["settings"]["style"] = style_key

    # Если выбран стиль "Новости" — подбираем 3 новости по нише
    if style_key == "news":
        await query.edit_message_text("⏳ Подбираю актуальные новости по твоей нише...")

        news_prompt = (
            f"Ниша: {ud['niche']}\n"
            f"Продукт/услуга: {ud['product']}\n"
            f"Целевая аудитория: {ud['audience']}"
        )
        news_text = await search_news(news_prompt)
        ud["news_list"] = parse_news_items(news_text)

        news_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1️⃣", callback_data="news_0"),
             InlineKeyboardButton("2️⃣", callback_data="news_1"),
             InlineKeyboardButton("3️⃣", callback_data="news_2")],
            [InlineKeyboardButton("🔄 Подобрать другие новости", callback_data="news_refresh")],
        ])
        await safe_send_bot(
            context.bot, query.from_user.id,
            f"📰 **Актуальные новости для твоей ниши:**\n\n{news_text}",
            reply_markup=news_kb,
        )
        return CHOOSE_NEWS

    style_name = REELS_STYLES.get(style_key, {}).get("name", style_key)
    await query.edit_message_text(
        f"Стиль: {style_name} ✅\n\n"
        "🎯 **Куда ведём аудиторию?** 👇",
        parse_mode="Markdown",
        reply_markup=target_keyboard(),
    )
    return CHOOSE_TARGET


async def choose_news(update: Update, context) -> int:
    """Выбор новости или запрос новых."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    if query.data == "news_refresh":
        await query.edit_message_text("⏳ Подбираю другие новости...")

        news_prompt = (
            f"Ниша: {ud['niche']}\n"
            f"Продукт/услуга: {ud['product']}\n"
            f"Целевая аудитория: {ud['audience']}\n\n"
            f"Предыдущие новости (НЕ повторяй их):\n" +
            "\n".join(ud.get("news_list", []))
        )
        news_text = await search_news(news_prompt)
        ud["news_list"] = parse_news_items(news_text)

        news_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1️⃣", callback_data="news_0"),
             InlineKeyboardButton("2️⃣", callback_data="news_1"),
             InlineKeyboardButton("3️⃣", callback_data="news_2")],
            [InlineKeyboardButton("🔄 Подобрать другие новости", callback_data="news_refresh")],
        ])
        await safe_send_bot(
            context.bot, query.from_user.id,
            f"📰 **Новые новости:**\n\n{news_text}",
            reply_markup=news_kb,
        )
        return CHOOSE_NEWS

    # Юзер выбрал конкретную новость
    news_idx = int(query.data.replace("news_", ""))
    news_lines = ud.get("news_list", [])
    if news_idx < len(news_lines):
        chosen_news = news_lines[news_idx]
    else:
        chosen_news = news_lines[0] if news_lines else "Актуальная новость по нише"

    ud["user_input"] = f"Новость для Reels: {chosen_news}"

    await query.edit_message_text(
        f"Выбрана новость: {chosen_news} ✅\n\n"
        "🎯 **Куда ведём аудиторию?** 👇",
        parse_mode="Markdown",
        reply_markup=target_keyboard(),
    )
    return CHOOSE_TARGET


async def choose_target(update: Update, context) -> int:
    """Выбор цели контента."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    target_key = query.data.replace("target_", "")
    ud["settings"]["target"] = target_key

    await query.edit_message_text(
        "⏱ **Какая длительность ролика?** 👇",
        parse_mode="Markdown",
        reply_markup=duration_keyboard(),
    )
    return CHOOSE_DURATION


async def choose_duration(update: Update, context) -> int:
    """Выбор длительности и запуск генерации."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    duration_key = query.data.replace("dur_", "")
    ud["settings"]["duration"] = duration_key

    await query.edit_message_text("⏳ Генерирую сценарий... Это займёт несколько секунд.")

    user_prompt = build_scenario_prompt(
        user_profile={
            "product": ud["product"],
            "niche": ud["niche"],
            "audience": ud["audience"],
        },
        settings=ud["settings"],
        user_input=ud.get("user_input"),
    )

    scenario = await stream_ai_to_chat(
        context.bot, query.from_user.id,
        SCENARIO_SYSTEM_PROMPT, user_prompt,
    )

    await safe_send_bot(
        context.bot, query.from_user.id, scenario,
        reply_markup=after_scenario_keyboard(),
    )

    return SHOW_SCENARIO


async def after_scenario_handler(update: Update, context) -> int:
    """Обработка кнопок после сценария."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    if query.data == "regenerate":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="⏳ Генерирую новый вариант сценария..."
        )

        user_prompt = build_scenario_prompt(
            user_profile={
                "product": ud["product"],
                "niche": ud["niche"],
                "audience": ud["audience"],
            },
            settings=ud["settings"],
            user_input=ud.get("user_input"),
        )

        scenario = await stream_ai_to_chat(
            context.bot, query.from_user.id,
            SCENARIO_SYSTEM_PROMPT, user_prompt,
        )

        await safe_send_bot(
            context.bot, query.from_user.id, scenario,
            reply_markup=after_scenario_keyboard(),
        )
        return SHOW_SCENARIO

    elif query.data == "show_course":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                "🎥💋 **Снимите это немедленно!** — курс по Reels, который реально приносит клиентов\n\n"
                "За 4 недели ты пройдёшь путь от «снимаю хаотично» до системы, которая работает на тебя 24/7:\n\n"
                "✅ Разберёшься в алгоритмах Instagram и прогреешь аккаунт правильно\n\n"
                "✅ Найдёшь свой архетип и поймёшь, КАК говорить с аудиторией — так, чтобы покупали\n\n"
                "✅ Получишь готовые промты для ChatGPT: идеи, сценарии, хуки, CTA — за минуты\n\n"
                "✅ Освоишь 34 формата Reels и научишься балансировать охваты, экспертность и продажи\n\n"
                "✅ Узнаешь, как собирать лиды прямо из роликов — через кодовые слова, автоворонки и ботов\n\n"
                "✅ Получишь модуль по монтажу от эксперта: свет, цвет, субтитры, ИИ-инструменты — 7 уроков\n\n"
                "✅ 4 живых созвона с Юлей — задашь вопросы и разберёшь свои ролики\n\n"
                "💡 Реальный кейс: $1500 с 3000 просмотров. Не магия — система.\n\n"
                "🏆 Бонус: челлендж «30 Reels за 30 дней» с призами — консультация, разбор аккаунта, доступ в закрытый клуб.\n\n"
                "Хватит кричать в подушку — пора, чтобы тебя услышали! 🚀"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Написать Юле чтобы занять место", url=JULIA_TG)],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]),
        )
        return SHOW_SCENARIO

    elif query.data == "change_settings":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🎨 **Выбери новый стиль Reels:** 👇",
            parse_mode="Markdown",
            reply_markup=style_keyboard(),
        )
        return CHOOSE_STYLE

    elif query.data == "main_menu":
        await query.edit_message_text(
            "Что хочешь сделать? 👇",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU


async def cancel(update: Update, context) -> int:
    """Отмена диалога."""
    await update.message.reply_text(
        "Отменено. Напиши /start чтобы начать заново."
    )
    return ConversationHandler.END


async def error_handler(update: object, context) -> None:
    """Log unhandled exceptions from update handlers."""
    logger.exception("Unhandled exception in update handler: %s", context.error)


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Инициализация БД
    loop.run_until_complete(init_db())

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NICHE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_niche),
            ],
            ASK_PRODUCT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_product),
            ],
            ASK_AUDIENCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_audience),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(main_menu_handler),
            ],
            SCENARIO_INPUT_CHOICE: [
                CallbackQueryHandler(scenario_input_choice),
            ],
            SCENARIO_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_input),
            ],
            SCENARIO_VOICE_INPUT: [
                MessageHandler(filters.VOICE | filters.AUDIO, receive_voice_input),
            ],
            CHOOSE_STYLE: [
                CallbackQueryHandler(choose_style, pattern=r"^style_"),
            ],
            CHOOSE_NEWS: [
                CallbackQueryHandler(choose_news, pattern=r"^news_"),
            ],
            CHOOSE_TARGET: [
                CallbackQueryHandler(choose_target, pattern=r"^target_"),
            ],
            CHOOSE_DURATION: [
                CallbackQueryHandler(choose_duration, pattern=r"^dur_"),
            ],
            SHOW_SCENARIO: [
                CallbackQueryHandler(after_scenario_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    # Инициализация PTB приложения (без запуска polling)
    loop.run_until_complete(app.initialize())
    loop.run_until_complete(app.start())

    # Регистрация вебхука в Telegram
    webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
    loop.run_until_complete(app.bot.set_webhook(webhook_url))
    logger.info(f"Telegram webhook set to {webhook_url}")

    # Запуск веб-сервера для Tribute вебхуков + Telegram вебхука
    webhook_app = create_webhook_app(app)
    runner = web.AppRunner(webhook_app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    logger.info(f"Webhook server started on port {PORT}")

    logger.info("Бот запущен в режиме вебхука!")
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(app.bot.delete_webhook())
        loop.run_until_complete(app.stop())
        loop.run_until_complete(app.shutdown())
        loop.run_until_complete(runner.cleanup())


if __name__ == "__main__":
    main()
