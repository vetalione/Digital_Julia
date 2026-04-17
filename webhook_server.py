"""
Веб-сервер для приёма вебхуков от Tribute.
"""

import hashlib
import hmac
import json
import logging

from aiohttp import web

from config import TRIBUTE_API_KEY
from db import grant_access, revoke_access

logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str) -> bool:
    """Проверка HMAC-SHA256 подписи вебхука от Tribute."""
    expected = hmac.new(
        TRIBUTE_API_KEY.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_tribute_webhook(request: web.Request) -> web.Response:
    """Обработка вебхуков от Tribute."""
    body = await request.read()
    signature = request.headers.get("trbt-signature", "")

    if TRIBUTE_API_KEY and not verify_signature(body, signature):
        logger.warning("Invalid webhook signature")
        return web.json_response({"error": "invalid signature"}, status=401)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    event_name = data.get("name", "")
    payload = data.get("payload", {})

    logger.info(f"Tribute webhook: {event_name}")

    if event_name == "new_digital_product":
        telegram_user_id = payload.get("telegram_user_id")
        telegram_username = payload.get("telegram_username", "")
        purchase_id = payload.get("purchase_id")
        product_id = payload.get("product_id")
        product_name = payload.get("product_name", "")
        amount = payload.get("amount", 0)
        currency = payload.get("currency", "")

        if telegram_user_id and purchase_id:
            await grant_access(
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_username,
                purchase_id=purchase_id,
                product_id=product_id,
                product_name=product_name,
                amount=amount,
                currency=currency,
            )
            logger.info(
                f"Access granted via webhook: user={telegram_user_id} "
                f"purchase={purchase_id}"
            )

    elif event_name == "digital_product_refunded":
        purchase_id = payload.get("purchase_id")
        if purchase_id:
            await revoke_access(purchase_id)
            logger.info(f"Access revoked via webhook: purchase={purchase_id}")

    return web.json_response({"status": "ok"})


async def handle_health(request: web.Request) -> web.Response:
    """Health check для Railway."""
    return web.json_response({"status": "healthy"})


def create_webhook_app() -> web.Application:
    """Создаёт aiohttp приложение с маршрутами."""
    app = web.Application()
    app.router.add_post("/webhook/tribute", handle_tribute_webhook)
    app.router.add_get("/health", handle_health)
    return app
