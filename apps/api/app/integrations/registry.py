from __future__ import annotations

from pydantic import BaseModel


class IntegrationDefinition(BaseModel):
    code: str
    name: str
    pattern: str
    script_path: str
    status: str


def list_integration_definitions() -> list[IntegrationDefinition]:
    return [
        IntegrationDefinition(
            code="iiko",
            name="iiko Server API",
            pattern="direct_api",
            script_path="integrations/iiko/scripts",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="sber",
            name="Sber Business",
            pattern="direct_api",
            script_path="integrations/sber/scripts",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="tbank",
            name="T-Bank Business",
            pattern="direct_api",
            script_path="integrations/tbank/scripts",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="mailru",
            name="Mail.ru mailbox",
            pattern="mail_with_ai_ocr",
            script_path="integrations/mailru/scripts",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="mango",
            name="Mango Office",
            pattern="lk_browser_cookie",
            script_path="integrations/mango/scripts",
            status="existing_python_scripts",
        ),
        # Приёмка документов из Телеграма живёт в приложении с 02.08.2026: владелец пересылает
        # боту фотографию от арендодателя, файл уходит в ту же дверь, что и кнопка «Загрузить
        # документ». Прежний локальный скрипт (integrations/tbank/scripts/payment_order_bot.py)
        # выведен из работы — он готовил платёжные поручения T-Bank, а это владельцу не нужно.
        # Запускать его с тем же токеном нельзя: getUpdates читает только один потребитель.
        IntegrationDefinition(
            code="telegram_intake",
            name="Telegram document intake bot",
            pattern="telegram_long_polling",
            script_path="apps/api/app/services/telegram_intake.py",
            status="in_app_service",
        ),
    ]
