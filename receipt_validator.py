"""GPT-4o vision-based payment receipt validator (UAH transfers)."""
import base64
import hashlib
import json
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def image_sha256(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


async def validate_receipt(
    client: AsyncOpenAI,
    image_bytes: bytes,
    expected_amount_uah: int,
    expected_card_last4: str,
) -> dict:
    """Анализирует скриншот банковского перевода через GPT-4o vision.

    Возвращает dict со всеми распознанными полями плюс:
      is_valid: bool — прошёл ли все проверки
      validation_reason: str | None — текст причины отказа (для отправки юзеру)
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""Ты проверяешь скриншот банковского платежа в Telegram-боте.

Сегодня: {today}

Ожидается перевод:
- Сумма: {expected_amount_uah} UAH (гривен, ₴)
- На карту, заканчивающуюся на: *{expected_card_last4}

ВАЖНО — это НОРМА (НЕ считай мошенничеством):
- Скриншот из любого банка: Privat24, Monobank, ПУМБ, Sense, A-Bank и т.д.
- Имя получателя может не отображаться — это нормально, не требуй его
- Любые часовые пояса
- Перевод по номеру карты или по номеру телефона — оба варианта норм

ПОДДЕЛКА — только если есть ВИЗУАЛЬНЫЕ признаки:
- Следы фотошопа, нечёткие края цифр, артефакты в области суммы или карты
- Несовпадающие шрифты в разных полях
- Явные следы редактирования

СВЕЖЕСТЬ: скрин считается свежим, если дата платежа — в пределах 7 дней до {today}.

Верни СТРОГО JSON без markdown, без комментариев:
{{
  "is_receipt": true/false,
  "amount": число или null,
  "currency": "UAH"/"RUB"/"USD"/"EUR"/null,
  "card_last4": "последние 4 цифры карты получателя как видно на скрине" или null,
  "date_str": "дата платежа как видно на скрине" или null,
  "is_recent": true/false,
  "is_fraud": true/false,
  "confidence": 0-100,
  "fraud_reason": "если is_fraud=true — описание визуальных признаков подделки" или null
}}"""

    response = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "high",
                }},
            ],
        }],
        response_format={"type": "json_object"},
        max_completion_tokens=600,
    )
    raw = response.choices[0].message.content or "{}"
    logger.info(f"Receipt validator raw: {raw[:500]}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse GPT response as JSON: {raw}")
        return {
            "is_valid": False,
            "validation_reason": "Не удалось проанализировать скриншот. Попробуйте ещё раз или пришлите более чёткое изображение.",
        }

    # Применяем правила валидации
    issues: list[str] = []

    if not data.get("is_receipt"):
        issues.append(
            "Это не похоже на скриншот банковского перевода. "
            "Пришлите, пожалуйста, скриншот из приложения банка с подтверждением платежа."
        )
    else:
        amount = data.get("amount")
        currency = (data.get("currency") or "").upper()

        if amount is None:
            issues.append("Не удалось распознать сумму на скриншоте.")
        else:
            try:
                amount_f = float(amount)
            except (TypeError, ValueError):
                amount_f = None

            if amount_f is None:
                issues.append("Не удалось распознать сумму на скриншоте.")
            elif currency and currency != "UAH":
                issues.append(
                    f"На скриншоте сумма указана в валюте {currency}, "
                    f"а оплата принимается только в гривнах (UAH). "
                    f"Нужно перевести {expected_amount_uah} ₴."
                )
            elif abs(amount_f - expected_amount_uah) > 5:
                issues.append(
                    f"На скриншоте сумма {amount_f:g} ₴, "
                    f"а нужно перевести {expected_amount_uah} ₴."
                )

        card_last4 = data.get("card_last4") or ""
        # Берём только цифры
        card_digits = "".join(ch for ch in card_last4 if ch.isdigit())
        if card_digits and len(card_digits) >= 4 and card_digits[-4:] != expected_card_last4:
            issues.append(
                f"На скриншоте перевод на карту *{card_digits[-4:]}, "
                f"а нужно на карту *{expected_card_last4}."
            )

        if not data.get("is_recent"):
            issues.append(
                "Дата перевода старше 7 дней. "
                "Пришлите свежий скриншот (не старше недели)."
            )

        if data.get("is_fraud"):
            fraud_reason = data.get("fraud_reason") or "обнаружены признаки редактирования."
            issues.append(f"Обнаружены признаки редактирования изображения: {fraud_reason}")

        if not issues and data.get("confidence", 0) < 60:
            issues.append(
                "Не удалось уверенно распознать платёж. "
                "Пришлите более чёткий скриншот, где видны сумма и реквизиты."
            )

    data["is_valid"] = len(issues) == 0
    data["validation_reason"] = " ".join(issues) if issues else None
    return data
