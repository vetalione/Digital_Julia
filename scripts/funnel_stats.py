"""Одноразовый скрипт: воронка по визитёрам/оплатам."""
import asyncio
import asyncpg
import os


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    async with pool.acquire() as conn:
        visitors = await conn.fetchval("SELECT COUNT(*) FROM bot_visitors")
        total_visits = await conn.fetchval("SELECT COALESCE(SUM(visits_count),0) FROM bot_visitors")

        users_with_access = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE is_refunded = FALSE AND telegram_user_id != 0"
        )

        by_source = await conn.fetch(
            "SELECT product_name, currency, COUNT(*) AS cnt "
            "FROM purchases WHERE is_refunded = FALSE "
            "GROUP BY product_name, currency ORDER BY cnt DESC"
        )

        unique_uploaders = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM receipt_uploads"
        )
        total_uploads = await conn.fetchval("SELECT COUNT(*) FROM receipt_uploads")
        valid_uploads = await conn.fetchval(
            "SELECT COUNT(*) FROM receipt_uploads WHERE is_valid = TRUE"
        )
        invalid_uploads = await conn.fetchval(
            "SELECT COUNT(*) FROM receipt_uploads WHERE is_valid = FALSE"
        )
        unique_valid_users = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM receipt_uploads WHERE is_valid = TRUE"
        )

        direct_uah_grants = await conn.fetchval(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM purchases "
            "WHERE product_name='direct_uah_transfer' AND is_refunded=FALSE"
        )

        reasons = await conn.fetch(
            "SELECT reason, COUNT(*) AS cnt FROM receipt_uploads "
            "WHERE is_valid = FALSE AND reason IS NOT NULL "
            "GROUP BY reason ORDER BY cnt DESC LIMIT 5"
        )

        # pay_clicks (новая таблица; может быть пуста)
        clicks_total = 0
        clicks_by_method = []
        try:
            clicks_total = await conn.fetchval("SELECT COUNT(*) FROM pay_clicks")
            clicks_by_method = await conn.fetch(
                "SELECT method, COUNT(*) AS cnt, COUNT(DISTINCT telegram_user_id) AS users "
                "FROM pay_clicks GROUP BY method ORDER BY cnt DESC"
            )
        except Exception:
            pass

        print("=" * 70)
        print("ВОРОНКА БОТА")
        print("=" * 70)
        print(f"\n[1] Визитёров (уникальных)         : {visitors}")
        print(f"    Всего визитов (сумма /start)   : {total_visits}")
        print(f"\n[2] Получили доступ (всего)        : {users_with_access}")
        print(f"\n[3] По источникам оплаты:")
        for r in by_source:
            pn = (r["product_name"] or "?")[:30]
            cur = (r["currency"] or "?")[:10]
            print(f"    {pn:30s} {cur:10s} : {r['cnt']}")

        print(f"\n[4] Прямой UAH перевод (скрин-флоу):")
        print(f"    Уникальных юзеров прислали скрин : {unique_uploaders}")
        print(f"    Всего загруженных скринов        : {total_uploads}")
        print(f"      прошли проверку (валидные)     : {valid_uploads}")
        print(f"      отклонены                      : {invalid_uploads}")
        print(f"    Юзеров получили доступ через UAH : {direct_uah_grants}")

        if reasons:
            print(f"\n[5] Топ причин отказа скринов:")
            for r in reasons:
                short = (r["reason"] or "")[:80]
                print(f"    [{r['cnt']:3d}] {short}")

        print(f"\n[6] Клики по кнопкам оплаты (всего записей: {clicks_total}):")
        if clicks_by_method:
            for r in clicks_by_method:
                print(f"    {r['method']:20s} clicks={r['cnt']:4d}  unique_users={r['users']}")
        else:
            print("    (нет данных — трекинг включился с commit 2be8f91)")

        print()
        print("=" * 70)
        print("КОНВЕРСИИ:")
        if visitors:
            print(f"  визитёр → доступ через UAH       : "
                  f"{direct_uah_grants}/{visitors} = {direct_uah_grants*100/visitors:.1f}%")
            print(f"  визитёр → доступ ЛЮБЫМ способом  : "
                  f"{users_with_access}/{visitors} = {users_with_access*100/visitors:.1f}%")
        if unique_uploaders:
            print(f"  прислал скрин → получил доступ   : "
                  f"{unique_valid_users}/{unique_uploaders} = {unique_valid_users*100/unique_uploaders:.1f}%")
        print("=" * 70)

    await pool.close()


asyncio.run(main())
