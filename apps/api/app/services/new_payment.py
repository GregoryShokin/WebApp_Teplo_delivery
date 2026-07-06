"""Окно «Новый платёж»: контекст формы и маршрутизация «статья ДДС → механизм».

Единое окно создаёт банковские черновики существующими механизмами; драйвер — статья:

- ``expense`` — статья без получателя («просто трата»): via-safe черновик на карту ИП
  (``counterparty_payments.create_expense_payment_draft``), paid-переход заводит
  транзит р/с→Сейф и целёвку этой статьи;
- ``employee_payout`` — зарплатные статьи: разовая выплата сотруднику
  (``employee_payouts``, только on_demand);
- ``employee_advance`` / ``employee_loan`` — аванс/заём (``payroll_advance_service``);
- ``supplier_prepayment`` — предоплата поставщику (``create_standalone_payment_draft``);
- ``supplier_invoices`` — оплата накладных (``create_payment_draft_for_invoices``).

Селект статей собирается ПО ПРАВАМ пользователя: каждый маршрут виден только при его
операционном праве; свободные расходные статьи (маршрут ``expense``) не включают
движковые/защищённые и статьи с флагом «доступна в кассе» — у тех свои контуры выдачи.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import permission_is_granted
from app.models import Account, DdsArticle, Employee, Wallet
from app.services.counterparty_registry import NON_PAYOUT_WALLET_CODES
from app.services.kassa.payouts import (
    EMPLOYEE_ADVANCE_ARTICLE_CODE,
    EMPLOYEE_LOAN_ARTICLE_CODE,
    PROTECTED_ARTICLE_CODES,
    SUPPLIER_PREPAYMENT_ARTICLE_CODE,
)
from app.services.payroll_admin import (
    PAYOUT_MODE_ON_DEMAND,
    _load_okladnik_payout_modes,
)

# Статьи-маршруты окна. Зарплатные коды и «Оплата поставщикам» продублированы из
# профильных сервисов (employee_payouts / payroll_payout_allocation /
# counterparty_payments), чтобы не тянуть их тяжёлые модули на импорт; равенство
# закреплено тестами tests/counterparties/test_new_payment_window.py.
EMPLOYEE_PAYOUT_ARTICLE_CODES = (
    "zarplata_administrativnogo_personala",
    "zarplata_proizvodstvennogo_personala",
)
SUPPLIER_INVOICES_ARTICLE_CODE = "payment_to_supplier"

FLOW_BY_ARTICLE_CODE: dict[str, str] = {
    EMPLOYEE_ADVANCE_ARTICLE_CODE: "employee_advance",
    EMPLOYEE_LOAN_ARTICLE_CODE: "employee_loan",
    SUPPLIER_PREPAYMENT_ARTICLE_CODE: "supplier_prepayment",
    SUPPLIER_INVOICES_ARTICLE_CODE: "supplier_invoices",
    **{code: "employee_payout" for code in EMPLOYEE_PAYOUT_ARTICLE_CODES},
}

# Движковые статьи вне канонического каталога (0114, KEEP_ACTIVE_CODES) — цели правил
# классификации банка, руками по ним не платят. Переводы между счетами уже в
# PROTECTED_ARTICLE_CODES.
ENGINE_ARTICLE_CODES = frozenset({"revenue_acquiring_tbank", "internal_transfer", "unknown"})

# Право каждого маршрута — существующие операционные права, новых не вводим.
FLOW_PERMISSIONS: dict[str, tuple[str, ...]] = {
    # Свободный вывод на Сейф = целёвка произвольной статьи (как ручной резерв Сейфа).
    "expense": ("finance.safe.allocate",),
    "employee_payout": ("payroll.employee_payouts.create",),
    "employee_advance": (
        "payroll.advances.admin.issue",
        "payroll.advances.production.issue",
    ),
    # Займ выдаётся тем же POST /payroll/advances: роутер требует issue-право по
    # должности сотрудника ДО allow_loan, поэтому маршрут гейтится issue-правами,
    # а право займов проверяется ДОПОЛНИТЕЛЬНО (см. _allowed_flows).
    "employee_loan": (
        "payroll.advances.admin.issue",
        "payroll.advances.production.issue",
    ),
    "supplier_prepayment": ("invoices.normal.pay",),
    "supplier_invoices": ("invoices.normal.pay",),
}

# Займ = аванс сверх заработанного: без этого права статья займа в селект не попадает.
LOAN_PERMISSION_CODE = "payroll.loans.issue"

# Пункт FAB «Создать новый платёж» виден при любом из прав маршрутов (см. фронтовый
# ACTION_PERMISSIONS["payments.create"] — списки должны совпадать).
NEW_PAYMENT_PERMISSION_CODES: tuple[str, ...] = tuple(
    dict.fromkeys(code for codes in FLOW_PERMISSIONS.values() for code in codes)
)

# Маршруты, которым нужен справочник сотрудников.
_EMPLOYEE_FLOWS = ("employee_payout", "employee_advance", "employee_loan")


def _allowed_flows(permissions: frozenset[str]) -> set[str]:
    allowed = {
        flow
        for flow, codes in FLOW_PERMISSIONS.items()
        if any(permission_is_granted(code, permissions) for code in codes)
    }
    if "employee_loan" in allowed and not permission_is_granted(LOAN_PERMISSION_CODE, permissions):
        allowed.discard("employee_loan")
    return allowed


def new_payment_article_flow(article: DdsArticle) -> str | None:
    """Маршрут окна для статьи; ``None`` — статья в окне недоступна.

    Статьи-маршруты определяются по коду независимо от флагов (у зарплатных и «Оплаты
    поставщикам» kassa-флаг запрещён, но и с ним маршрут остался бы верным). Остальные
    расходные статьи — свободный вывод (``expense``), кроме движковых/защищённых и
    статей с флагом «доступна в кассе»: это окно про банк, у тех — свои контуры.
    """
    if not article.is_active or article.movement_type != "outflow":
        return None
    flow = FLOW_BY_ARTICLE_CODE.get(article.code)
    if flow is not None:
        return flow
    if (
        article.kassa_enabled
        or article.code in PROTECTED_ARTICLE_CODES
        or article.code in ENGINE_ARTICLE_CODES
    ):
        return None
    return "expense"


def ensure_expense_article_allowed(article: DdsArticle) -> None:
    """Статья годится для свободного вывода на Сейф (маршрут ``expense``)?

    Бэкенд-страховка симметрично фильтру селекта: статьи-маршруты, движковые,
    защищённые и кассовые статьи свободным выводом не оплачиваются.
    """
    if not article.is_active:
        raise ValueError("Статья ДДС неактивна")
    if article.movement_type != "outflow":
        raise ValueError("Свободный вывод на Сейф доступен только по расходным статьям")
    if new_payment_article_flow(article) != "expense":
        raise ValueError(
            "У этой статьи собственный контур выдачи — свободный вывод на Сейф недоступен"
        )


async def list_new_payment_articles(
    session: AsyncSession, *, permissions: frozenset[str]
) -> list[dict[str, Any]]:
    """Статьи селекта окна: только маршруты, доступные пользователю по правам."""
    allowed = _allowed_flows(permissions)
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(DdsArticle.is_active.is_(True), DdsArticle.movement_type == "outflow")
            .order_by(DdsArticle.name)
        )
    ).all()
    return [
        {"id": article.id, "code": article.code, "name": article.name, "flow": flow}
        for article in articles
        if (flow := new_payment_article_flow(article)) is not None and flow in allowed
    ]


async def list_new_payment_wallets(session: AsyncSession) -> list[dict[str, Any]]:
    """Счета списания: активные банковские кошельки без накопительных фондов.

    ``bank_code`` — чтобы фронт знал, где создаются черновики (только Т-Банк);
    для выплат сотрудникам допустим и Сбер (черновика нет, подтверждение привязкой).
    """
    rows = (
        await session.execute(
            select(Wallet, Account.bank_code)
            .outerjoin(Account, Account.id == Wallet.account_id)
            .where(
                Wallet.status == "active",
                Wallet.type.in_(("bank", "bank_account")),
                Wallet.code.notin_(NON_PAYOUT_WALLET_CODES),
            )
            .order_by(Wallet.code)
        )
    ).all()
    return [
        {"id": wallet.id, "code": wallet.code, "name": wallet.name, "bank_code": bank_code}
        for wallet, bank_code in rows
    ]


async def list_new_payment_employees(
    session: AsyncSession, *, include_all: bool = True
) -> list[dict[str, Any]]:
    """Активные сотрудники + признак on_demand (режим оклада «по востребованию»).

    Для маршрута выплат активны только on_demand (остальные в селекте disabled —
    им доступны аванс или займ); для авансов/займов — все активные.
    ``include_all=False`` — только on_demand: пользователю с одним правом выплат
    полный штат не раскрываем (staff-видимость — отдельные права).
    """
    modes = await _load_okladnik_payout_modes(session)
    on_demand_positions = {
        position for position, mode in modes.items() if mode == PAYOUT_MODE_ON_DEMAND
    }
    rows = await session.scalars(
        select(Employee).where(Employee.status == "active").order_by(Employee.full_name)
    )
    return [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "position": employee.position,
            "on_demand": employee.position in on_demand_positions,
        }
        for employee in rows.all()
        if include_all or employee.position in on_demand_positions
    ]


async def build_new_payment_context(
    session: AsyncSession, *, permissions: frozenset[str]
) -> dict[str, Any]:
    """Контекст окна одним запросом: статьи по правам, счета, сотрудники (если нужны)."""
    articles = await list_new_payment_articles(session, permissions=permissions)
    wallets = await list_new_payment_wallets(session)
    flows = {article["flow"] for article in articles}
    employees: list[dict[str, Any]] = []
    if flows.intersection(_EMPLOYEE_FLOWS):
        # Полный штат — только тем, кто выдаёт авансы/займы (выбор любого сотрудника
        # операционно нужен); c одним правом выплат отдаём только on_demand.
        include_all = bool(flows.intersection(("employee_advance", "employee_loan")))
        employees = await list_new_payment_employees(session, include_all=include_all)
    return {"articles": articles, "wallets": wallets, "employees": employees}


__all__ = [
    "ENGINE_ARTICLE_CODES",
    "EMPLOYEE_PAYOUT_ARTICLE_CODES",
    "FLOW_BY_ARTICLE_CODE",
    "FLOW_PERMISSIONS",
    "NEW_PAYMENT_PERMISSION_CODES",
    "SUPPLIER_INVOICES_ARTICLE_CODE",
    "build_new_payment_context",
    "ensure_expense_article_allowed",
    "list_new_payment_articles",
    "list_new_payment_employees",
    "list_new_payment_wallets",
    "new_payment_article_flow",
]
