"""Admin stats: расчёт метрик + рендер с дельтой относительно прошлого вызова.

Доступ только для @vetalsmirnov.
"""
from __future__ import annotations

import logging
from typing import Any

from db import pool, get_last_snapshot, save_snapshot

logger = logging.getLogger(__name__)

ADMIN_USERNAME = "vetalsmirnov"


def is_admin(username: str | None) -> bool:
    return (username or "").lower() == ADMIN_USERNAME


# ── Расчёт метрик ────────────────────────────────────────────────

async def compute_pay_stats() -> dict[str, int]:
    """Метрики платёжной воронки для команды /stats."""
    assert pool is not None
    async with pool.acquire() as conn:
        visitors = await conn.fetchval("SELECT COUNT(*) FROM bot_visitors") or 0
        visits_total = await conn.fetchval(
            "SELECT COALESCE(SUM(visits_count),0) FROM bot_visitors"
        ) or 0

        # Клики по платёжным кнопкам (уникальные юзеры)
        click_rows = await conn.fetch(
            "SELECT method, COUNT(DISTINCT telegram_user_id) AS u FROM pay_clicks GROUP BY method"
        )
        clicks = {r["method"]: r["u"] for r in click_rows}

        # Реальные оплаты:
        # - direct_uah: product_name='direct_uah_transfer'
        # - stars: currency='XTR' (Telegram Stars в Tribute)
        # - card: всё остальное Tribute (не XTR, не direct/manual)
        paid_direct_uah = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE product_name='direct_uah_transfer' AND is_refunded=FALSE"
        ) or 0
        paid_stars = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE UPPER(currency)='XTR' AND is_refunded=FALSE"
        ) or 0
        paid_card = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE is_refunded=FALSE "
            "AND product_name NOT IN ('direct_uah_transfer','manual_grant','manual_access','Manual access','friends') "
            "AND UPPER(COALESCE(currency,'')) <> 'XTR' "
            "AND telegram_user_id != 0"
        ) or 0

        # Скриншоты
        screenshots_total = await conn.fetchval(
            "SELECT COUNT(*) FROM receipt_uploads"
        ) or 0
        screenshots_valid = await conn.fetchval(
            "SELECT COUNT(*) FROM receipt_uploads WHERE is_valid=TRUE"
        ) or 0
        screenshots_users = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM receipt_uploads"
        ) or 0

    return {
        "visitors": visitors,
        "visits_total": visits_total,
        "clicks_card": clicks.get("tribute_web", 0),
        "clicks_stars": clicks.get("tribute_stars", 0),
        "clicks_direct_uah": clicks.get("direct_uah", 0),
        "paid_card": paid_card,
        "paid_stars": paid_stars,
        "paid_direct_uah": paid_direct_uah,
        "screenshots_total": screenshots_total,
        "screenshots_valid": screenshots_valid,
        "screenshots_users": screenshots_users,
    }


async def compute_usage_stats() -> dict[str, int]:
    """Метрики использования для команды /usage_stats."""
    assert pool is not None
    async with pool.acquire() as conn:
        users_with_access = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE is_refunded=FALSE AND telegram_user_id != 0"
        ) or 0

        diagnostics = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type='diagnostic_done'"
        ) or 0
        scenarios_total = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type='scenario_generated'"
        ) or 0
        scenarios_improved = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type='scenario_generated' "
            "AND (meta->>'from_user_input')::boolean = TRUE"
        ) or 0
        news_searched = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type='news_searched'"
        ) or 0
        news_used = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type='news_used'"
        ) or 0

        # Разбивка сценариев по стилям
        style_rows = await conn.fetch(
            "SELECT meta->>'style' AS style, COUNT(*) AS cnt "
            "FROM events WHERE event_type='scenario_generated' AND meta ? 'style' "
            "GROUP BY style"
        )
        styles = {r["style"]: r["cnt"] for r in style_rows}

    return {
        "users_with_access": users_with_access,
        "diagnostics": diagnostics,
        "scenarios_total": scenarios_total,
        "scenarios_improved": scenarios_improved,
        "news_searched": news_searched,
        "news_used": news_used,
        "style_news": styles.get("news", 0),
        "style_personal": styles.get("personal", 0),
        "style_expert": styles.get("expert", 0),
        "style_provoke": styles.get("provoke", 0),
    }


# ── Рендер ───────────────────────────────────────────────────────

def _delta(curr: int, prev: dict[str, Any] | None, key: str) -> str:
    if prev is None:
        return "(первый замер)"
    p = int(prev.get(key, 0) or 0)
    d = curr - p
    if d == 0:
        return "(±0)"
    return f"(+{d})" if d > 0 else f"({d})"


def _pct(part: int, total: int) -> str:
    if not total:
        return "—"
    return f"{part * 100 / total:.1f}%"


def render_pay_stats(curr: dict, prev: dict | None) -> str:
    d = lambda k: _delta(curr[k], prev, k)
    lines = [
        "📊 *Платёжная статистика*",
        "",
        f"👥 Уникальных юзеров в боте: *{curr['visitors']}* {d('visitors')}",
        f"🚪 Всего визитов: *{curr['visits_total']}* {d('visits_total')}",
        "",
        "*Оплата звёздами ⭐*",
        f"  кликов: *{curr['clicks_stars']}* {d('clicks_stars')}",
        f"  оплатили: *{curr['paid_stars']}* {d('paid_stars')}",
        "",
        "*Оплата картой 💳*",
        f"  кликов: *{curr['clicks_card']}* {d('clicks_card')}",
        f"  оплатили: *{curr['paid_card']}* {d('paid_card')}",
        "",
        "*Прямой перевод гривной 🇦*",
        f"  кликов: *{curr['clicks_direct_uah']}* {d('clicks_direct_uah')}",
        f"  оплатили: *{curr['paid_direct_uah']}* {d('paid_direct_uah')}",
        "",
        f"📸 Скриншотов прислано: *{curr['screenshots_total']}* {d('screenshots_total')}",
        f"  из них валидных: *{curr['screenshots_valid']}* {d('screenshots_valid')}",
        f"  уникальных юзеров: *{curr['screenshots_users']}* {d('screenshots_users')}",
    ]
    return "\n".join(lines)


def render_usage_stats(curr: dict, prev: dict | None) -> str:
    d = lambda k: _delta(curr[k], prev, k)
    total_styles = (curr["style_news"] + curr["style_personal"]
                    + curr["style_expert"] + curr["style_provoke"])
    lines = [
        "📈 *Статистика использования*",
        "",
        f"🔓 Юзеров с активным доступом: *{curr['users_with_access']}* {d('users_with_access')}",
        f"🔍 Диагностик проведено: *{curr['diagnostics']}* {d('diagnostics')}",
        f"🎬 Сценариев сгенерировано: *{curr['scenarios_total']}* {d('scenarios_total')}",
        "",
        "*Распределение по стилям:*",
        f"  📰 Новости: *{curr['style_news']}* ({_pct(curr['style_news'], total_styles)}) {d('style_news')}",
        f"  🙋 Личный: *{curr['style_personal']}* ({_pct(curr['style_personal'], total_styles)}) {d('style_personal')}",
        f"  🧠 Экспертный: *{curr['style_expert']}* ({_pct(curr['style_expert'], total_styles)}) {d('style_expert')}",
        f"  🔥 Провокация: *{curr['style_provoke']}* ({_pct(curr['style_provoke'], total_styles)}) {d('style_provoke')}",
        "",
        f"📡 Новостей подобрано: *{curr['news_searched']}* {d('news_searched')}",
        f"📡 Новостей использовано в сценарии: *{curr['news_used']}* {d('news_used')}",
        f"✏️ Сценариев улучшено (текст/голос юзера): *{curr['scenarios_improved']}* {d('scenarios_improved')}",
    ]
    return "\n".join(lines)


# ── Точки входа из bot.py ────────────────────────────────────────

async def stats_command_pay(admin_user_id: int) -> str:
    curr = await compute_pay_stats()
    prev = await get_last_snapshot(admin_user_id, "stats")
    text = render_pay_stats(curr, prev)
    await save_snapshot(admin_user_id, "stats", curr)
    return text


async def stats_command_usage(admin_user_id: int) -> str:
    curr = await compute_usage_stats()
    prev = await get_last_snapshot(admin_user_id, "usage_stats")
    text = render_usage_stats(curr, prev)
    await save_snapshot(admin_user_id, "usage_stats", curr)
    return text
