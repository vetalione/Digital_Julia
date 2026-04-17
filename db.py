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
        """)
    logger.info("Database initialized")


async def close_db():
    """Закрытие пула соединений."""
    global pool
    if pool:
        await pool.close()
        pool = None


async def check_access(telegram_user_id: int) -> bool:
    """Проверяет, есть ли у пользователя активный доступ."""
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
        return row["max_until"] > datetime.now(timezone.utc)
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
