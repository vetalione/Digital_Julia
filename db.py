"""
Модуль для работы с PostgreSQL — управление доступом пользователей.
"""

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from config import DATABASE_URL, ACCESS_DURATION_DAYS

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None


async def init_db():
    """Инициализация пула соединений и создание таблицы."""
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                telegram_username TEXT,
                purchase_id INTEGER UNIQUE NOT NULL,
                product_id INTEGER,
                product_name TEXT,
                amount INTEGER,
                currency TEXT,
                access_until TIMESTAMPTZ NOT NULL,
                is_refunded BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_purchases_tg_user
                ON purchases (telegram_user_id);
            CREATE TABLE IF NOT EXISTS bot_visitors (
                telegram_user_id BIGINT PRIMARY KEY,
                telegram_username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                visits_count INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS receipt_uploads (
                id SERIAL PRIMARY KEY,
                image_hash TEXT UNIQUE NOT NULL,
                telegram_user_id BIGINT NOT NULL,
                extracted_amount NUMERIC,
                extracted_card_last4 TEXT,
                extracted_date TEXT,
                is_valid BOOLEAN,
                reason TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_uploads_user
                ON receipt_uploads (telegram_user_id);
            CREATE TABLE IF NOT EXISTS pay_clicks (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                telegram_username TEXT,
                method TEXT NOT NULL,
                clicked_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_pay_clicks_user
                ON pay_clicks (telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_pay_clicks_method
                ON pay_clicks (method);
        """)
    logger.info("Database initialized")


async def close_db():
    """Закрытие пула соединений."""
    global pool
    if pool:
        await pool.close()
        pool = None


async def check_access(telegram_user_id: int, telegram_username: str | None = None) -> bool:
    """Проверяет, есть ли у пользователя активный доступ. Проверяет по ID, затем по username."""
    if not pool:
        return False
    row = await pool.fetchrow(
        """
        SELECT MAX(access_until) AS max_until
        FROM purchases
        WHERE telegram_user_id = $1 AND is_refunded = FALSE
        """,
        telegram_user_id,
    )
    if row and row["max_until"]:
        if row["max_until"] > datetime.now(timezone.utc):
            return True
    # Фоллбэк: проверка по username
    if telegram_username:
        row = await pool.fetchrow(
            """
            SELECT MAX(access_until) AS max_until
            FROM purchases
            WHERE LOWER(telegram_username) = LOWER($1) AND is_refunded = FALSE
            """,
            telegram_username,
        )
        if row and row["max_until"] and row["max_until"] > datetime.now(timezone.utc):
            # Обновляем user_id для будущих проверок
            await pool.execute(
                "UPDATE purchases SET telegram_user_id = $1 WHERE LOWER(telegram_username) = LOWER($2) AND is_refunded = FALSE",
                telegram_user_id,
                telegram_username,
            )
            logger.info(f"Access found by username @{telegram_username}, updated user_id to {telegram_user_id}")
            return True
    return False


async def get_access_until(telegram_user_id: int) -> datetime | None:
    """Возвращает дату окончания доступа или None."""
    if not pool:
        return None
    row = await pool.fetchrow(
        """
        SELECT MAX(access_until) AS max_until
        FROM purchases
        WHERE telegram_user_id = $1 AND is_refunded = FALSE
        """,
        telegram_user_id,
    )
    if row and row["max_until"]:
        return row["max_until"]
    return None


async def grant_access(
    telegram_user_id: int,
    telegram_username: str,
    purchase_id: int,
    product_id: int,
    product_name: str,
    amount: int,
    currency: str,
) -> datetime:
    """Выдаёт доступ пользователю. Продлевает если уже есть активный."""
    if not pool:
        raise RuntimeError("Database not initialized")

    current_until = await get_access_until(telegram_user_id)
    now = datetime.now(timezone.utc)

    # Если есть активный доступ — продлеваем от его конца
    if current_until and current_until > now:
        new_until = current_until + timedelta(days=ACCESS_DURATION_DAYS)
    else:
        new_until = now + timedelta(days=ACCESS_DURATION_DAYS)

    await pool.execute(
        """
        INSERT INTO purchases (
            telegram_user_id, telegram_username, purchase_id,
            product_id, product_name, amount, currency, access_until
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (purchase_id) DO NOTHING
        """,
        telegram_user_id,
        telegram_username,
        purchase_id,
        product_id,
        product_name,
        amount,
        currency,
        new_until,
    )
    logger.info(
        f"Access granted: user={telegram_user_id} until={new_until} purchase={purchase_id}"
    )
    return new_until


async def revoke_access(purchase_id: int):
    """Отзывает доступ при возврате средств."""
    if not pool:
        return
    await pool.execute(
        "UPDATE purchases SET is_refunded = TRUE WHERE purchase_id = $1",
        purchase_id,
    )
    logger.info(f"Access revoked: purchase={purchase_id}")


async def log_visit(
    telegram_user_id: int,
    telegram_username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """Фиксирует визит юзера в бота. Сходится по telegram_user_id."""
    if not pool:
        return
    await pool.execute(
        """
        INSERT INTO bot_visitors (
            telegram_user_id, telegram_username, first_name, last_name, language_code
        ) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (telegram_user_id) DO UPDATE SET
            telegram_username = COALESCE(EXCLUDED.telegram_username, bot_visitors.telegram_username),
            first_name = COALESCE(EXCLUDED.first_name, bot_visitors.first_name),
            last_name = COALESCE(EXCLUDED.last_name, bot_visitors.last_name),
            language_code = COALESCE(EXCLUDED.language_code, bot_visitors.language_code),
            last_seen_at = NOW(),
            visits_count = bot_visitors.visits_count + 1
        """,
        telegram_user_id,
        telegram_username,
        first_name,
        last_name,
        language_code,
    )


async def get_receipt_by_hash(image_hash: str) -> dict | None:
    """Возвращает запись о скрине по хэшу или None если такого не было."""
    if not pool:
        return None
    row = await pool.fetchrow(
        """SELECT telegram_user_id, is_valid, created_at
           FROM receipt_uploads WHERE image_hash = $1""",
        image_hash,
    )
    return dict(row) if row else None


async def save_receipt_upload(
    image_hash: str,
    telegram_user_id: int,
    extracted_amount: float | None,
    extracted_card_last4: str | None,
    extracted_date: str | None,
    is_valid: bool,
    reason: str | None,
) -> None:
    """Сохраняет факт загрузки скрина (для дедупа)."""
    if not pool:
        return
    await pool.execute(
        """INSERT INTO receipt_uploads
           (image_hash, telegram_user_id, extracted_amount,
            extracted_card_last4, extracted_date, is_valid, reason)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (image_hash) DO NOTHING""",
        image_hash,
        telegram_user_id,
        extracted_amount,
        extracted_card_last4,
        extracted_date,
        is_valid,
        reason,
    )


async def log_pay_click(
    telegram_user_id: int,
    telegram_username: str | None,
    method: str,
) -> None:
    """Фиксирует нажатие кнопки оплаты любым способом. method: 'direct_uah' | 'tribute_web' | 'tribute_stars'"""
    if not pool:
        return
    await pool.execute(
        """INSERT INTO pay_clicks (telegram_user_id, telegram_username, method)
           VALUES ($1, $2, $3)""",
        telegram_user_id,
        telegram_username,
        method,
    )
