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
            script_path="scripts/iiko",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="sber",
            name="Sber Business",
            pattern="direct_api",
            script_path="scripts/sber",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="tbank",
            name="T-Bank Business",
            pattern="direct_api",
            script_path="scripts/tbank",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="mailru",
            name="Mail.ru mailbox",
            pattern="mail_with_ai_ocr",
            script_path="scripts/mail",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="mango",
            name="Mango Office",
            pattern="lk_browser_cookie",
            script_path="scripts/mango",
            status="existing_python_scripts",
        ),
        IntegrationDefinition(
            code="telegram_payment_ocr",
            name="Telegram payment OCR bot",
            pattern="telegram_ocr_bot",
            script_path="scripts/tbank/payment_order_bot.py",
            status="existing_python_script",
        ),
    ]
