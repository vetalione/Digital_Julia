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
    PicklePersistence,
    filters,
)
from openai import OpenAI, AsyncOpenAI

from config import (
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY,
    TRIBUTE_PRODUCT_LINK, PORT, RAILWAY_PUBLIC_DOMAIN,
    FREE_SCENARIO_LIMIT,
)

JULIA_TG = "https://t.me/JFilipenko"
ASSISTANT_TG = "https://t.me/vetalsmirnov"

# Прямая оплата в гривнах
DIRECT_PAY_AMOUNT_UAH = 1543
DIRECT_PAY_CARD_FULL = "5169 1551 2428 3993"
DIRECT_PAY_CARD_LAST4 = "3993"
from prompts import (
    DIAGNOSIS_TIPS_PROMPT,
    SCENARIO_SYSTEM_PROMPT,
    NEWS_SEARCH_PROMPT,
    REELS_STYLES,
    AUDIENCE_TARGETS,
    DURATIONS,
    build_scenario_prompt,
)
from db import (
    init_db, close_db, check_access, get_access_until, log_visit, visitor_exists,
    grant_access, get_receipt_by_hash, save_receipt_upload, log_pay_click,
    log_event, get_profile, save_profile, clear_profile,
    set_preferred_model, get_preferred_model, count_scenarios,
)
from ai_providers import (
    DEFAULT_MODEL, available_models, model_name,
    normalize_model, stream_generate,
)
from receipt_validator import validate_receipt, image_sha256
from admin_stats import is_admin, stats_command_pay, stats_command_usage
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
    CHOOSE_MODEL,
) = range(14)

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
            "model": DEFAULT_MODEL,
        }
    return user_data_store[user_id]


# ── Вспомогательные функции ─────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Сгенерировать сценарий Reels", callback_data="generate_scenario")],
        [InlineKeyboardButton("🤖 Сменить нейросеть", callback_data="change_model")],
        [InlineKeyboardButton("🎓 Посмотреть программу курса", callback_data="show_course")],
        [InlineKeyboardButton("📋 Запись на консультацию — 200$", url=JULIA_TG)],
        [InlineKeyboardButton("🎤 Попасть на разбор — 100$", url=JULIA_TG)],
        [InlineKeyboardButton("🔄 Пройти диагностику заново", callback_data="restart_diagnosis")],
    ])


def _md_escape(text: str) -> str:
    """Экранирует спецсимволы legacy-Markdown, чтобы текст профиля не ломал парсинг."""
    if not text:
        return ""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _short(text: str, limit: int = 200) -> str:
    """Обрезает длинное поле профиля для показа в сообщении (защита от лимита 4096)."""
    if not text:
        return ""
    text = " ".join(text.split())  # схлопываем переносы/множественные пробелы
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def model_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора нейросети. Показывает только доступные (с API-ключом)."""
    buttons = []
    for key, meta in available_models().items():
        buttons.append([InlineKeyboardButton(meta["name"], callback_data=f"model_{key}")])
    return InlineKeyboardMarkup(buttons)


async def resolve_user_model(user_id: int, ud: dict) -> str:
    """Возвращает актуальную модель юзера: из памяти, иначе из БД, иначе дефолт."""
    model = ud.get("model")
    if not model:
        try:
            model = await get_preferred_model(user_id)
        except Exception as e:
            logger.warning(f"get_preferred_model failed: {e}")
            model = None
    model = normalize_model(model)
    ud["model"] = model
    return model


async def free_scenarios_left(user_id: int, username: str | None) -> int | None:
    """Сколько бесплатных сценариев осталось. None = безлимит (есть доступ)."""
    try:
        if await check_access(user_id, username):
            return None
    except Exception as e:
        logger.warning(f"check_access in free_scenarios_left failed: {e}")
    try:
        used = await count_scenarios(user_id)
    except Exception as e:
        logger.warning(f"count_scenarios failed: {e}")
        used = 0
    return max(0, FREE_SCENARIO_LIMIT - used)


async def send_paywall(bot, chat_id: int) -> None:
    """Отправляет сообщение об исчерпании бесплатных сценариев + кнопки оплаты."""
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎁 Бесплатные сценарии закончились — ты использовал(а) все {FREE_SCENARIO_LIMIT}!\n\n"
            "Надеюсь, ты увидел(а), насколько круто я генерирую Reels под твою нишу 🚀\n\n"
            "💳 *Доступ — 35$ на 30 дней*\n"
            "Безлимитная генерация сценариев на любой из нейросетей "
            "(ChatGPT, Claude, Gemini), без ограничений.\n\n"
            "Готов(а) продолжить? Выбери способ оплаты 👇"
        ),
        parse_mode="Markdown",
        reply_markup=payment_keyboard(),
    )


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


def news_target_keyboard() -> InlineKeyboardMarkup:
    """Два варианта цели — только для стиля Новости."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Привлечение клиентов", callback_data="target_leads")],
        [InlineKeyboardButton("📈 Охваты / набор аудитории", callback_data="target_subscribe")],
    ])


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
    parts = _split_text(text)
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            await target.reply_text(part, parse_mode=parse_mode)
        else:
            await target.reply_text(part, parse_mode=parse_mode, reply_markup=reply_markup)


def _split_text(text: str, limit: int = 4000) -> list[str]:
    """Разбивает текст по абзацам не разрывая Markdown-сущности."""
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n\n', 0, limit)
        if cut == -1:
            cut = text.rfind('\n', 0, limit)
        if cut == -1:
            cut = text.rfind(' ', 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


async def safe_send_bot(bot, chat_id: int, text: str, reply_markup=None, parse_mode="Markdown"):
    """Отправляет через bot.send_message, разбивая на части."""
    if len(text) <= 4000:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    parts = _split_text(text)
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
            max_completion_tokens=4000,
            temperature=0.8,
        )
        result = response.choices[0].message.content or "Не удалось сгенерировать ответ."
        return clean_md_for_telegram(strip_followup(result))
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return "⚠️ Произошла ошибка при генерации. Попробуйте ещё раз."


async def stream_ai_to_chat(
    bot, chat_id: int, system_prompt: str, user_prompt: str,
    model_key: str = DEFAULT_MODEL,
) -> str:
    """Стримит ответ выбранной нейросети (GPT/Claude/Gemini) в чат через
    sendMessageDraft (живое появление текста), возвращает финальный текст."""
    draft_id = random.randint(1, 2**31 - 1)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessageDraft"

    accumulated = ""
    last_update_time = 0.0

    try:
        async with aiohttp.ClientSession() as session:
            async for delta in stream_generate(model_key, system_prompt, user_prompt):
                accumulated += delta

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
        logger.error(f"Streaming error ({model_key}): {e}")
        # Финальный фоллбэк — обычный (нестриминговый) вызов GPT
        return await call_ai(system_prompt, user_prompt)


async def generate_scenario_with_model(
    bot, chat_id: int, model_key: str, system_prompt: str, user_prompt: str
) -> str:
    """Генерирует сценарий выбранной нейросетью со стримингом в чат."""
    return await stream_ai_to_chat(
        bot, chat_id, system_prompt, user_prompt, model_key=model_key
    )


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
        raw = response.output_text or "Не удалось найти новости."
        # Экранируем символы, которые ломают Telegram Markdown v1
        raw = raw.replace("_", "\\_").replace("[", "\\[")
        return raw
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
    """Keyboard с кнопками оплаты. Все три — callback'и для трекинга кликов."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить картой", callback_data="pay_card")],
        [InlineKeyboardButton("⭐ Оплата звёздами", callback_data="pay_stars")],
        [InlineKeyboardButton("🇦 Оплатить напрямую (₴)", callback_data="pay_direct_uah")],
    ])


async def require_access(update: Update) -> bool:
    """Проверяет доступ юзера. Возвращает True если доступ есть."""
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name or "друг"
    has_access = await check_access(user_id, username)
    if not has_access:
        text = (
            f"Привет, {first_name}! 👋\n"
            "Я — бот *ЦифроЮля* 🎬\n\n"
            "Я не просто «ещё один ИИ». Я обучена думать как Юля — с её методикой, "
            "глубоким знанием алгоритмов и реальными результатами. "
            "За последние 3 месяца Юля сделала 33 миллиона охватов и набрала +30k подписчиков "
            "в Instagram, поэтому я умею не просто «писать тексты», а собирать Reels, "
            "которые дают рост. То, что я делаю, нельзя повторить, просто зайдя в ChatGPT.\n\n"
            "*Вот что я умею:*\n\n"
            "🔍 *Диагностика* — разбор твоего бизнеса/экспертности: кому, про что и как снимать, "
            "какие посылы зайдут твоей аудитории\n\n"
            "🎬 *Генерация сценариев Reels* с нуля в 4 стилях:\n"
            "📰 Новостной\n"
            "🧠 Экспертный\n"
            "🙋 Личный\n"
            "🔥 Провокационный\n\n"
            "✏️ *Улучшение твоих сценариев* — присылай текстом или надиктовывай голосом, я доработаю\n\n"
            "📡 *Поиск вирусных новостей* по твоей теме + интеграция в сценарий одной кнопкой\n\n"
            "Каждый сценарий генерируется по специальной методике: с профессиональными хуками, "
            "петлями вовлечения, триггерами досмотра и шеринга, сильным СТА — всё под твою нишу, "
            "продукт и ЦА.\n\n"
            "💳 *Доступ — 35$ на 30 дней*\n"
            "Безлимит. Никаких ограничений по количеству сценариев.\n\n"
            "Готов(а) начать? 👇\n"
            "Нажми «Оплатить» — и через минуту уже будешь генерировать свой первый сценарий 🚀"
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
    user = update.effective_user

    # Проект закрыт: новеньких, кто заходит впервые и ещё не оплачивал,
    # дальше не пускаем. Проверяем ДО log_visit, чтобы отличить новичка.
    try:
        is_new_visitor = not await visitor_exists(user.id)
    except Exception as e:
        logger.warning(f"visitor_exists failed: {e}")
        is_new_visitor = False

    if is_new_visitor:
        try:
            has_access = await check_access(user.id, user.username)
        except Exception as e:
            logger.warning(f"check_access in start failed: {e}")
            has_access = False
        if not has_access:
            # Всё равно фиксируем визит для статистики
            try:
                await log_visit(
                    telegram_user_id=user.id,
                    telegram_username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    language_code=user.language_code,
                )
            except Exception as e:
                logger.warning(f"log_visit failed: {e}")
            await update.message.reply_text("Проект закрыт")
            return ConversationHandler.END

    # Логируем визит для будущих рассылок
    try:
        await log_visit(
            telegram_user_id=user.id,
            telegram_username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )
    except Exception as e:
        logger.warning(f"log_visit failed: {e}")

    # Полный сброс старого состояния (защита от "молчащего" бота после redeploy)
    context.user_data.clear()
    user_data_store.pop(user.id, None)

    # Доступ больше не блокирует вход: даём 3 бесплатных сценария.
    # has_access нужен только чтобы подобрать текст приветствия.
    left = await free_scenarios_left(user.id, user.username)
    has_access = left is None

    # Если у юзера уже есть сохранённый профиль (ниша/продукт/ЦА) —
    # пропускаем диагностику и сразу ведём в главное меню.
    profile = None
    try:
        profile = await get_profile(user.id)
    except Exception as e:
        logger.warning(f"get_profile failed: {e}")

    if profile and profile.get("niche") and profile.get("product") and profile.get("audience"):
        ud = get_user(user.id)
        ud["niche"] = profile["niche"]
        ud["product"] = profile["product"]
        ud["audience"] = profile["audience"]
        ud["model"] = normalize_model(profile.get("preferred_model"))
        limit_note = "" if has_access else f"\n🎁 Бесплатных сценариев осталось: *{left}*\n"
        # Поля профиля могут быть очень длинными (юзер вставил документ) или
        # содержать спецсимволы Markdown — обрезаем и экранируем, чтобы не
        # упереться в лимит 4096 и не сломать парсинг (иначе start() падает и
        # юзер застревает в цикле /reset → /start → ошибка).
        niche_d = _md_escape(_short(profile["niche"]))
        product_d = _md_escape(_short(profile["product"]))
        audience_d = _md_escape(_short(profile["audience"]))
        try:
            await update.message.reply_text(
                f"С возвращением, {user.first_name}! 👋\n\n"
                f"Твой профиль уже сохранён:\n"
                f"• *Ниша:* {niche_d}\n"
                f"• *Продукт:* {product_d}\n"
                f"• *ЦА:* {audience_d}\n"
                f"• *Нейросеть:* {model_name(ud['model'])}\n"
                f"{limit_note}\n"
                f"Что хочешь сделать? 👇",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            # Фоллбэк без Markdown — гарантированно доставляем меню
            logger.warning(f"start: returning-user message failed, plain fallback: {e}")
            await update.message.reply_text(
                f"С возвращением, {user.first_name}! 👋\n\n"
                "Твой профиль уже сохранён. Что хочешь сделать? 👇",
                reply_markup=main_menu_keyboard(),
            )
        return MAIN_MENU

    free_note = (
        "🎁 *3 первых сценария — бесплатно!* Попробуй без оплаты.\n\n"
        if not has_access else ""
    )
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Я — бот ЦифроЮли 🎬\n\n"
        "Помогу тебе:\n"
        "• Разобраться, какой контент снимать для твоей ниши\n"
        "• Получить советы по визуалу, хукам и смыслам\n"
        "• Сгенерировать готовый сценарий Reels\n\n"
        f"{free_note}"
        "Для начала давай проведём быструю диагностику 🔍\n\n"
        "**В какой ты нише?**\n"
        "Например: маркетинг, фитнес, психология, бьюти, коучинг, e-commerce...",
        parse_mode="Markdown",
    )
    return ASK_NICHE


async def post_payment_entry(update: Update, context) -> int:
    """Entry-point для ConversationHandler: ловит первое текстовое сообщение юзера
    после успешной оплаты и вводит его в флоу диагностики как в ASK_NICHE.
    Если флаг не выставлен — возвращаем END, чтобы не ловить чужие сообщения."""
    if not context.user_data.get("post_payment_pending"):
        return ConversationHandler.END
    context.user_data.pop("post_payment_pending", None)
    return await ask_niche(update, context)


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

    try:
        await log_event(update.effective_user.id, "diagnostic_done")
    except Exception as e:
        logger.warning(f"log_event diagnostic_done failed: {e}")

    # Сохраняем профиль в Postgres, чтобы пережил редеплой
    try:
        await save_profile(
            update.effective_user.id,
            ud["niche"], ud["product"], ud["audience"],
        )
    except Exception as e:
        logger.warning(f"save_profile failed: {e}")

    # Если юзер ещё не выбирал нейросеть — предлагаем выбрать (один раз).
    chosen = None
    try:
        chosen = await get_preferred_model(update.effective_user.id)
    except Exception as e:
        logger.warning(f"get_preferred_model failed: {e}")

    avail = available_models()
    if not chosen and len(avail) > 1:
        await update.message.reply_text(
            "🤖 *Выбери нейросеть для генерации сценариев:*\n\n"
            "Можно будет сменить в любой момент через меню.",
            parse_mode="Markdown",
            reply_markup=model_keyboard(),
        )
        return CHOOSE_MODEL

    # Иначе фиксируем дефолт/единственную модель и идём в меню
    ud["model"] = normalize_model(chosen)
    await update.message.reply_text(
        "Что хочешь сделать дальше? 👇",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def choose_model(update: Update, context) -> int:
    """Юзер выбрал нейросеть. Сохраняем и ведём в меню."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    model_key = normalize_model(query.data.replace("model_", ""))
    ud["model"] = model_key
    try:
        await set_preferred_model(query.from_user.id, model_key)
    except Exception as e:
        logger.warning(f"set_preferred_model failed: {e}")

    await query.edit_message_text(
        f"Готово! Сценарии будет генерировать *{model_name(model_key)}* ✅",
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="Что хочешь сделать дальше? 👇",
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
        # Проверяем лимит бесплатных сценариев
        left = await free_scenarios_left(query.from_user.id, query.from_user.username)
        if left is not None and left <= 0:
            await query.edit_message_reply_markup(reply_markup=None)
            await send_paywall(context.bot, query.from_user.id)
            return MAIN_MENU

        ud = get_user(query.from_user.id)
        await resolve_user_model(query.from_user.id, ud)
        left_note = "" if left is None else f"\n🎁 Бесплатных сценариев осталось: {left}"
        await query.edit_message_text(
            "🎬 **Генерация сценария Reels**\n\n"
            f"Нейросеть: {model_name(ud['model'])}{left_note}\n\n"
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

    elif query.data == "change_model":
        avail = available_models()
        if len(avail) <= 1:
            await query.answer("Доступна только одна нейросеть", show_alert=True)
            return MAIN_MENU
        ud = get_user(query.from_user.id)
        await resolve_user_model(query.from_user.id, ud)
        await query.edit_message_text(
            f"🤖 *Текущая нейросеть:* {model_name(ud['model'])}\n\n"
            "Выбери, какой генерировать сценарии 👇",
            parse_mode="Markdown",
            reply_markup=model_keyboard(),
        )
        return CHOOSE_MODEL

    elif query.data == "restart_diagnosis":
        ud = get_user(query.from_user.id)
        ud["product"] = ""
        ud["niche"] = ""
        ud["audience"] = ""
        ud["user_input"] = None
        # Стираем сохранённый профиль в БД — юзер хочет начать заново
        try:
            await clear_profile(query.from_user.id)
        except Exception as e:
            logger.warning(f"clear_profile failed: {e}")

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
        reply_markup=news_target_keyboard(),
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
        try:
            await log_event(query.from_user.id, "news_searched", {"refresh": True})
        except Exception as e:
            logger.warning(f"log_event news_searched(refresh) failed: {e}")

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
    try:
        await log_event(query.from_user.id, "news_used", {"index": news_idx})
    except Exception as e:
        logger.warning(f"log_event news_used failed: {e}")

    await query.edit_message_text(
        f"Выбрана новость: {chosen_news} ✅\n\n"
        "🎯 **Куда ведём аудиторию?** 👇",
        parse_mode="Markdown",
        reply_markup=news_target_keyboard(),
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

    # Контроль лимита бесплатных сценариев
    left = await free_scenarios_left(query.from_user.id, query.from_user.username)
    if left is not None and left <= 0:
        await query.edit_message_reply_markup(reply_markup=None)
        await send_paywall(context.bot, query.from_user.id)
        return MAIN_MENU

    model_key = await resolve_user_model(query.from_user.id, ud)
    await query.edit_message_text(
        f"⏳ Генерирую сценарий ({model_name(model_key)})... Это займёт несколько секунд."
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

    scenario = await generate_scenario_with_model(
        context.bot, query.from_user.id, model_key,
        SCENARIO_SYSTEM_PROMPT, user_prompt,
    )

    try:
        await log_event(
            query.from_user.id,
            "scenario_generated",
            {
                "style": ud["settings"].get("style"),
                "target": ud["settings"].get("target"),
                "duration": ud["settings"].get("duration"),
                "from_user_input": bool(ud.get("user_input")),
                "model": model_key,
            },
        )
    except Exception as e:
        logger.warning(f"log_event scenario_generated failed: {e}")

    await safe_send_bot(
        context.bot, query.from_user.id, scenario,
        reply_markup=after_scenario_keyboard(),
    )

    # Напоминаем сколько бесплатных осталось
    if left is not None:
        remaining = max(0, left - 1)
        if remaining > 0:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=f"🎁 Бесплатных сценариев осталось: *{remaining}*",
                parse_mode="Markdown",
            )
        else:
            await send_paywall(context.bot, query.from_user.id)

    return SHOW_SCENARIO


async def after_scenario_handler(update: Update, context) -> int:
    """Обработка кнопок после сценария."""
    query = update.callback_query
    await query.answer()
    ud = get_user(query.from_user.id)

    if query.data == "regenerate":
        # regenerate тоже считается как генерация и тратит лимит
        left = await free_scenarios_left(query.from_user.id, query.from_user.username)
        if left is not None and left <= 0:
            await query.edit_message_reply_markup(reply_markup=None)
            await send_paywall(context.bot, query.from_user.id)
            return SHOW_SCENARIO

        await query.edit_message_reply_markup(reply_markup=None)
        model_key = await resolve_user_model(query.from_user.id, ud)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"⏳ Генерирую новый вариант ({model_name(model_key)})..."
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

        scenario = await generate_scenario_with_model(
            context.bot, query.from_user.id, model_key,
            SCENARIO_SYSTEM_PROMPT, user_prompt,
        )

        try:
            await log_event(
                query.from_user.id,
                "scenario_generated",
                {
                    "style": ud["settings"].get("style"),
                    "target": ud["settings"].get("target"),
                    "duration": ud["settings"].get("duration"),
                    "from_user_input": bool(ud.get("user_input")),
                    "regenerate": True,
                    "model": model_key,
                },
            )
        except Exception as e:
            logger.warning(f"log_event regenerate failed: {e}")

        await safe_send_bot(
            context.bot, query.from_user.id, scenario,
            reply_markup=after_scenario_keyboard(),
        )

        if left is not None:
            remaining = max(0, left - 1)
            if remaining > 0:
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=f"🎁 Бесплатных сценариев осталось: *{remaining}*",
                    parse_mode="Markdown",
                )
            else:
                await send_paywall(context.bot, query.from_user.id)
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


# ── Админ-команды статистики ─────────────────────────────
async def admin_stats_pay(update: Update, context) -> None:
    user = update.effective_user
    if not is_admin(user.username):
        return  # молча игнорируем
    try:
        text = await stats_command_pay(user.id)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.exception(f"/stats failed: {e}")
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def admin_stats_usage(update: Update, context) -> None:
    user = update.effective_user
    if not is_admin(user.username):
        return
    try:
        text = await stats_command_usage(user.id)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.exception(f"/usage_stats failed: {e}")
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


# ── Прямая оплата в гривнах ────────────────────────────────────────

def _direct_pay_assistant_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать ассистенту", url=ASSISTANT_TG)],
        [InlineKeyboardButton("⬅️ Другие способы оплаты", callback_data="pay_back")],
    ])


def _payment_failure_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать ассистенту", url=ASSISTANT_TG)],
        [InlineKeyboardButton("🔁 Прислать другой скриншот", callback_data="pay_direct_uah")],
    ])


async def pay_card_callback(update: Update, context) -> None:
    """Юзер нажал «Оплатить картой» — логируем клик, шлём ссылку Tribute."""
    query = update.callback_query
    await query.answer()
    try:
        await log_pay_click(query.from_user.id, query.from_user.username, "tribute_web")
    except Exception as e:
        logger.warning(f"log_pay_click failed: {e}")
    await query.message.reply_text(
        "💳 *Оплата картой*\n\n"
        "Нажми на кнопку ниже — откроется страница Tribute с формой оплаты.\n\n"
        "Если ссылка не открывается или поля не нажимаются:\n"
        "• Попробуй открыть ссылку *в браузере* (Safari / Chrome), а не во встроенном окне Telegram\n"
        "• Включи *VPN* (например европейский) — это решает проблему в большинстве случаев\n\n"
        "Если и это не помогло — выбери другой способ оплаты (звёздами или прямым "
        "переводом в гривнах) или напиши ассистенту 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Перейти к оплате", url="https://web.tribute.tg/p/uzV")],
            [InlineKeyboardButton("⬅️ Другие способы оплаты", callback_data="pay_back")],
            [InlineKeyboardButton("💬 Написать ассистенту", url=ASSISTANT_TG)],
        ]),
    )


async def pay_stars_callback(update: Update, context) -> None:
    """Юзер нажал «Оплата звёздами» — логируем клик, шлём ссылку на mini-app."""
    query = update.callback_query
    await query.answer()
    try:
        await log_pay_click(query.from_user.id, query.from_user.username, "tribute_stars")
    except Exception as e:
        logger.warning(f"log_pay_click failed: {e}")
    await query.message.reply_text(
        "⭐ *Оплата звёздами Telegram*\n\n"
        "Нажми на кнопку ниже — откроется mini-app Tribute прямо в Telegram.\n\n"
        "_Курс звёзд может отличаться по гео. Если стоимость кажется завышенной — "
        "попробуй оплату картой или прямым переводом гривной._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Перейти к оплате", url="https://t.me/tribute/app?startapp=puzV")],
            [InlineKeyboardButton("⬅️ Другие способы оплаты", callback_data="pay_back")],
            [InlineKeyboardButton("💬 Написать ассистенту", url=ASSISTANT_TG)],
        ]),
    )


async def pay_direct_uah_callback(update: Update, context) -> None:
    """Юзер нажал «Оплатить напрямую (₴)» — показываем реквизиты и ждём скриншот."""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_receipt"] = True
    try:
        await log_pay_click(query.from_user.id, query.from_user.username, "direct_uah")
    except Exception as e:
        logger.warning(f"log_pay_click failed: {e}")

    text = (
        "🇦 *Прямая оплата в гривнах*\n\n"
        f"Переведите *{DIRECT_PAY_AMOUNT_UAH} ₴* на карту:\n\n"
        f"`{DIRECT_PAY_CARD_FULL}`\n"
        "_(нажми, чтобы скопировать)_\n\n"
        "После перевода *пришлите сюда скриншот* из приложения банка — "
        "я автоматически проверю платёж и открою доступ на 30 дней.\n\n"
        "⚠️ Важно:\n"
        "• Скрин должен быть свежим (не старше 7 дней)\n"
        "• Сумма и номер карты должны быть видны\n"
        "• Один скриншот = один доступ\n\n"
        "Если что-то пошло не так — напиши ассистенту 👇"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=_direct_pay_assistant_keyboard(),
    )


async def pay_back_callback(update: Update, context) -> None:
    """Возврат к выбору способов оплаты."""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_receipt"] = False
    await query.edit_message_text(
        "Выбери способ оплаты доступа 👇",
        reply_markup=payment_keyboard(),
    )


async def handle_receipt_photo(update: Update, context) -> None:
    """Обрабатывает скриншот платежа: GPT-vision → дедуп → выдача доступа."""
    if not context.user_data.get("awaiting_receipt"):
        # Юзер прислал фото, но не нажимал кнопку прямой оплаты — игнорируем
        return

    user = update.effective_user
    msg = update.message

    if not msg or not msg.photo:
        return

    status_msg = await msg.reply_text("🔍 Проверяю скриншот... Это займёт 5-15 секунд.")

    try:
        # Берём самый большой размер
        photo = msg.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())

        # Дедуп по хэшу
        img_hash = image_sha256(image_bytes)
        existing = await get_receipt_by_hash(img_hash)
        if existing:
            await status_msg.delete()
            owner_id = existing["telegram_user_id"]
            if existing["is_valid"] and owner_id == user.id:
                # Сами уже использовали — но возможно доступ ещё активен
                until = await get_access_until(user.id)
                if until:
                    await msg.reply_text(
                        f"✅ Этот скриншот уже использован — доступ активен до "
                        f"*{until.strftime('%d.%m.%Y')}*.\n\nНажми /start чтобы начать.",
                        parse_mode="Markdown",
                    )
                    return
            await msg.reply_text(
                "❌ *Этот скриншот уже использовался* для получения доступа.\n\n"
                "Один скрин = один доступ. Если хочешь продлить — сделай новый перевод "
                f"({DIRECT_PAY_AMOUNT_UAH} ₴ на карту `{DIRECT_PAY_CARD_FULL}`) "
                "и пришли свежий скриншот.\n\n"
                "Если считаешь что это ошибка — напиши ассистенту 👇",
                parse_mode="Markdown",
                reply_markup=_payment_failure_keyboard(),
            )
            return

        # GPT-валидация
        result = await validate_receipt(
            async_client,
            image_bytes,
            expected_amount_uah=DIRECT_PAY_AMOUNT_UAH,
            expected_card_last4=DIRECT_PAY_CARD_LAST4,
        )

        # Сохраняем факт загрузки (даже если невалид — чтобы повторно тот же скрин не пытались скормить)
        await save_receipt_upload(
            image_hash=img_hash,
            telegram_user_id=user.id,
            extracted_amount=float(result["amount"]) if result.get("amount") is not None else None,
            extracted_card_last4=(result.get("card_last4") or "")[-4:] or None,
            extracted_date=result.get("date_str"),
            is_valid=bool(result.get("is_valid")),
            reason=result.get("validation_reason"),
        )

        await status_msg.delete()

        if not result.get("is_valid"):
            reason = result.get("validation_reason") or "Не удалось подтвердить платёж."
            await msg.reply_text(
                f"❌ *Скриншот не прошёл проверку.*\n\n{reason}\n\n"
                "Можешь прислать другой скриншот или связаться с ассистентом 👇",
                parse_mode="Markdown",
                reply_markup=_payment_failure_keyboard(),
            )
            return

        # Всё ок — выдаём доступ
        purchase_id = int(time.time())
        until = await grant_access(
            telegram_user_id=user.id,
            telegram_username=user.username or "",
            purchase_id=purchase_id,
            product_id=0,
            product_name="direct_uah_transfer",
            amount=DIRECT_PAY_AMOUNT_UAH,
            currency="UAH",
        )
        context.user_data["awaiting_receipt"] = False

        # Присылаем такое же сообщение как после успешной оплаты через Tribute
        await msg.reply_text(
            "✅ *Оплата получена!*\n\n"
            "Доступ к боту активирован 🎉\n\n"
            "Нажми /start чтобы начать диагностику и сгенерировать "
            "свой первый сценарий Reels 🎬",
            parse_mode="Markdown",
        )

        logger.info(f"Direct UAH access granted to {user.id} (@{user.username}) until {until}")

    except Exception as e:
        logger.exception(f"Error in handle_receipt_photo: {e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await msg.reply_text(
            "⚠️ Произошла ошибка при проверке скриншота. Попробуй ещё раз "
            "или напиши ассистенту 👇",
            reply_markup=_payment_failure_keyboard(),
        )


async def reset_command(update: Update, context) -> int:
    """Полный сброс состояния пользователя. Спасает, если бот "молчит" из-за
    застрявшего ConversationHandler state в pickle-персистентности."""
    user = update.effective_user
    try:
        context.user_data.clear()
        context.chat_data.clear()
    except Exception as e:
        logger.warning(f"reset: failed to clear user/chat data: {e}")
    user_data_store.pop(user.id, None)

    # Главное: сбрасываем state самого ConversationHandler. Без этого юзер
    # остаётся "застрявшим" в каком-то state (например MAIN_MENU) и его текст
    # уходит не в тот handler — бот молчит.
    try:
        conv = context.bot_data.get("conv_handler")
        if conv is not None:
            chat_id = update.effective_chat.id if update.effective_chat else user.id
            # ConversationHandler хранит состояния в conv._conversations.
            # Ключ зависит от per_chat/per_user (по умолчанию (chat_id, user_id)).
            for key in list(conv._conversations.keys()):
                if user.id in key or chat_id in key:
                    conv._conversations.pop(key, None)
            # Сбрасываем и в персистентности (PicklePersistence)
            persistence = context.application.persistence
            if persistence is not None:
                try:
                    await persistence.update_conversation(conv.name, (chat_id, user.id), None)
                except Exception as e:
                    logger.warning(f"reset: persistence.update_conversation failed: {e}")
    except Exception as e:
        logger.warning(f"reset: failed to clear conv state: {e}")

    if update.message:
        await update.message.reply_text(
            "🔄 Состояние сброшено. Нажми /start чтобы начать заново."
        )
    return ConversationHandler.END


async def error_handler(update: object, context) -> None:
    """Log unhandled exceptions from update handlers and notify user."""
    logger.exception("Unhandled exception in update handler: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ Что-то пошло не так. Нажми /reset, а затем /start — "
                    "это вернёт бота в рабочее состояние."
                ),
            )
    except Exception:
        pass


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Инициализация БД
    loop.run_until_complete(init_db())

    # Персистентность состояний ConversationHandler — выживают redeploy
    # Если в Railway подключён Volume на /data — храним там, иначе рядом с кодом (эфемерно)
    persistence_path = "/data/bot_persistence.pickle" if os.path.isdir("/data") else "bot_persistence.pickle"
    persistence = PicklePersistence(filepath=persistence_path)
    logger.info(f"Using persistence file: {persistence_path}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
        ],
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
            CHOOSE_MODEL: [
                CallbackQueryHandler(choose_model, pattern=r"^model_"),
            ],
            SHOW_SCENARIO: [
                CallbackQueryHandler(after_scenario_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        # Состояние диалога переживает редеплой (хранится в PicklePersistence на
        # volume /data). Это нужно, чтобы кнопки старых меню продолжали работать
        # после обновления кода, а юзерам не приходилось заново жать /start.
        # От "застрявших" состояний защищают allow_reentry=True (через /start всегда
        # чистый вход) и команда /reset.
        name="main_conversation",
        persistent=True,
    )

    app.add_handler(conv)

    # Глобальные хендлеры прямой оплаты в гривнах (работают вне ConversationHandler,
    # чтобы быть доступными неоплаченным пользователям).
    app.add_handler(CallbackQueryHandler(pay_card_callback, pattern=r"^pay_card$"), group=1)
    app.add_handler(CallbackQueryHandler(pay_stars_callback, pattern=r"^pay_stars$"), group=1)
    app.add_handler(CallbackQueryHandler(pay_direct_uah_callback, pattern=r"^pay_direct_uah$"), group=1)
    app.add_handler(CallbackQueryHandler(pay_back_callback, pattern=r"^pay_back$"), group=1)
    app.add_handler(MessageHandler(filters.PHOTO, handle_receipt_photo), group=1)

    # Админ-команды статистики — только для @vetalsmirnov
    app.add_handler(CommandHandler("stats", admin_stats_pay), group=1)
    app.add_handler(CommandHandler("usage_stats", admin_stats_usage), group=1)

    # /reset — глобальный сброс состояния (фикс "молчащего" бота после redeploy)
    app.add_handler(CommandHandler("reset", reset_command), group=1)

    app.add_error_handler(error_handler)
    app.bot_data["conv_handler"] = conv

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
