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

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import permission_is_granted
from app.models import (
    Account,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    Employee,
    Wallet,
)
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

INTERNAL_TRANSFER_ARTICLE_CODE = "internal_transfer"

FLOW_BY_ARTICLE_CODE: dict[str, str] = {
    EMPLOYEE_ADVANCE_ARTICLE_CODE: "employee_advance",
    EMPLOYEE_LOAN_ARTICLE_CODE: "employee_loan",
    SUPPLIER_PREPAYMENT_ARTICLE_CODE: "supplier_prepayment",
    SUPPLIER_INVOICES_ARTICLE_CODE: "supplier_invoices",
    # Внутренний перевод — маршрут «Нового платежа»: наличный источник → двухногий
    # перевод; банк-источник → черновик-пополнение Сейфа (topup_only). Требует счёт-получатель.
    INTERNAL_TRANSFER_ARTICLE_CODE: "internal_transfer",
    **{code: "employee_payout" for code in EMPLOYEE_PAYOUT_ARTICLE_CODES},
}

# Движковые статьи вне канонического каталога (0114, KEEP_ACTIVE_CODES) — цели правил
# классификации банка, руками по ним не платят. Переводы между счетами уже в
# PROTECTED_ARTICLE_CODES.
ENGINE_ARTICLE_CODES = frozenset({"revenue_acquiring_tbank", "internal_transfer", "unknown"})

# Приходные статьи с собственными авто-контурами (выручка, контур кассовой смены,
# резервы) — ручной приход по ним из окна задвоил бы движковые проводки.
INCOME_ENGINE_ARTICLE_CODES = frozenset(
    {
        "revenue_cash",
        "revenue_acquiring_sber",
        "korretirovki_kassy",
        "postuplenie_deneg_s_torg_tochek",
        "rezervy_postuplenie",
    }
)

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
    # Внутренний перевод — как ручной резерв/движение Сейфа.
    "internal_transfer": ("finance.safe.allocate",),
    # Наличное поступление — реальное движение денег сразу (не намерение):
    # уровень права подтверждения оплат, как у выдачи резерва и pay_now.
    "income": ("finance.safe.confirm_paid",),
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
    if not article.is_active:
        return None
    # Статьи-маршруты (в т.ч. «Внутренний перевод» с movement_type=internal) — по коду,
    # до гейта по направлению: у перевода своё направление на ногах, не «outflow».
    flow = FLOW_BY_ARTICLE_CODE.get(article.code)
    if flow is not None:
        return flow
    # Приходные статьи → маршрут «поступление» (наличный приход на Сейф/в Кассу).
    # Технические/движковые и статьи с авто-контурами исключены — их книжат движки.
    if article.movement_type == "inflow":
        if (
            article.activity_type in ("technical", "internal")
            or article.kassa_enabled
            or article.code in PROTECTED_ARTICLE_CODES
            or article.code in ENGINE_ARTICLE_CODES
            or article.code in INCOME_ENGINE_ARTICLE_CODES
        ):
            return None
        return "income"
    if article.movement_type != "outflow":
        return None
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


def ensure_reservable_article_allowed(article: DdsArticle, *, has_counterparty: bool) -> None:
    """Статья годится для наличного резерва / целевой передачи из окна.

    Расходные статьи свободного вывода — всегда; «Авансы поставщикам»
    (маршрут ``supplier_prepayment``) — только с контрагентом: при выплате такого
    резерва создаётся предоплата-дебиторка (см. ``pay_allocation``).
    """
    if new_payment_article_flow(article) == "supplier_prepayment":
        if not article.is_active:
            raise ValueError("Статья ДДС неактивна")
        if not has_counterparty:
            raise ValueError("Резерв предоплаты поставщику требует контрагента")
        return
    ensure_expense_article_allowed(article)


def ensure_income_article_allowed(article: DdsArticle) -> None:
    """Статья годится для ручного наличного поступления (маршрут ``income``)?

    Симметрично фильтру селекта: только активные приходные без собственных
    авто-контуров (выручка/смена/резервы) и не технические переводы.
    """
    if not article.is_active:
        raise ValueError("Статья ДДС неактивна")
    if article.movement_type != "inflow":
        raise ValueError("Поступление можно провести только по приходной статье")
    if new_payment_article_flow(article) != "income":
        raise ValueError("У этой статьи собственный контур поступлений — ручной приход недоступен")


async def _counterparties_by_article(
    session: AsyncSession, article_ids: list[Any]
) -> dict[Any, list[dict[str, Any]]]:
    """Контрагенты, закреплённые за статьёй (``default_dds_article_id``, галка «Закрепить
    за контрагентом»). Для атрибуции свободного вывода: кому платим по этой статье."""
    if not article_ids:
        return {}
    rows = await session.execute(
        select(
            CounterpartyPayableProfile.default_dds_article_id,
            Counterparty.id,
            Counterparty.name,
            Counterparty.inn,
            CounterpartyPayableProfile.relationship,
            CounterpartyPayableProfile.requisites,
            CounterpartyPayableProfile.requisites_verified,
        )
        .join(Counterparty, Counterparty.id == CounterpartyPayableProfile.counterparty_id)
        .where(
            CounterpartyPayableProfile.default_dds_article_id.in_(article_ids),
            Counterparty.status != "archived",
        )
        .order_by(Counterparty.name)
    )
    by_article: dict[Any, list[dict[str, Any]]] = {}
    for article_id, cp_id, name, inn, relationship, requisites, requisites_verified in rows:
        by_article.setdefault(article_id, []).append(
            {
                "counterparty_id": cp_id,
                "name": name,
                "inn": inn,
                "relationship": relationship,
                "has_requisites": bool(requisites),
                "requisites_verified": bool(requisites_verified),
            }
        )
    return by_article


async def list_new_payment_articles(
    session: AsyncSession, *, permissions: frozenset[str]
) -> list[dict[str, Any]]:
    """Статьи селекта окна: только маршруты, доступные пользователю по правам.

    К каждой статье прикладываются закреплённые за ней контрагенты (``counterparties``) —
    для свободного вывода это выбор «кому платим» (атрибуция), у остальных маршрутов свой
    выбор получателя.
    """
    allowed = _allowed_flows(permissions)
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(
                DdsArticle.is_active.is_(True),
                # Расходные и приходные статьи + статья-маршрут «Внутренний перевод»
                # (movement_type=internal).
                or_(
                    DdsArticle.movement_type.in_(("outflow", "inflow")),
                    DdsArticle.code == INTERNAL_TRANSFER_ARTICLE_CODE,
                ),
            )
            .order_by(DdsArticle.name)
        )
    ).all()
    result = [
        {
            "id": article.id,
            "code": article.code,
            "name": article.name,
            "flow": flow,
            # Вид деятельности — для леджер-фильтра палитры (операционная/финансовая/…).
            "activity": article.activity_type,
        }
        for article in articles
        if (flow := new_payment_article_flow(article)) is not None and flow in allowed
    ]
    pinned = await _counterparties_by_article(session, [item["id"] for item in result])
    for item in result:
        item["counterparties"] = pinned.get(item["id"], [])
    return result


async def list_new_payment_wallets(session: AsyncSession) -> list[dict[str, Any]]:
    """Счета списания: банковские кошельки (черновик) + наличные Сейф/Касса (резерв).

    ``bank_code`` — где создаётся черновик (только Т-Банк; Сбер — свободный расход).
    ``kind``: ``bank`` — уходит банковским черновиком; ``cash`` — оплата наличными без
    черновика (сразу резерв). ``location`` наличных: ``safe`` (Сейф) / ``kassa`` (Касса).
    """
    rows = (
        await session.execute(
            select(Wallet, Account.bank_code)
            .outerjoin(Account, Account.id == Wallet.account_id)
            .where(
                Wallet.status == "active",
                or_(
                    and_(
                        Wallet.type.in_(("bank", "bank_account")),
                        Wallet.code.notin_(NON_PAYOUT_WALLET_CODES),
                    ),
                    Wallet.type.in_(("cash_safe", "store_cash")),
                ),
            )
            .order_by(Wallet.type.in_(("cash_safe", "store_cash")), Wallet.code)
        )
    ).all()
    result: list[dict[str, Any]] = []
    for wallet, bank_code in rows:
        if wallet.type == "cash_safe":
            kind, location = "cash", "safe"
        elif wallet.type == "store_cash":
            kind, location = "cash", "kassa"
        else:
            kind, location = "bank", None
        result.append(
            {
                "id": wallet.id,
                "code": wallet.code,
                "name": wallet.name,
                "bank_code": bank_code,
                "kind": kind,
                "location": location,
            }
        )
    return result


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
        select(Employee)
        # Плейсхолдеры пула «Внештат №N» не выбираемы в окне платежа.
        .where(
            Employee.status == "active",
            Employee.is_freelancer_placeholder.is_(False),
        )
        .order_by(Employee.full_name)
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


# Статусы сотрудников, доступных для привязки уже совершённой выплаты в журнале ДДС:
# активные и увольняемые (финальный расчёт нередко платится в статусе dismissing).
# Терминальный статус уволенного — inactive (там же системные аккаунты), поэтому его не берём.
PAYOUT_ATTRIBUTION_STATUSES = ("active", "dismissing")


async def list_payout_attribution_employees(session: AsyncSession) -> list[dict[str, Any]]:
    """Сотрудники для привязки выплаты при разборе операции ДДС: активные + увольняемые
    (dismissing). Плейсхолдеры пула исключены. С признаком on_demand (kind='owner_salary' vs
    'salary') и статусом — для пометки «увольняется» в UI."""
    modes = await _load_okladnik_payout_modes(session)
    on_demand_positions = {
        position for position, mode in modes.items() if mode == PAYOUT_MODE_ON_DEMAND
    }
    rows = await session.scalars(
        select(Employee)
        .where(
            Employee.status.in_(PAYOUT_ATTRIBUTION_STATUSES),
            Employee.is_freelancer_placeholder.is_(False),
        )
        .order_by(Employee.full_name)
    )
    return [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "position": employee.position,
            "on_demand": employee.position in on_demand_positions,
            "status": str(employee.status),
        }
        for employee in rows.all()
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
    "INCOME_ENGINE_ARTICLE_CODES",
    "NEW_PAYMENT_PERMISSION_CODES",
    "SUPPLIER_INVOICES_ARTICLE_CODE",
    "build_new_payment_context",
    "ensure_expense_article_allowed",
    "ensure_income_article_allowed",
    "ensure_reservable_article_allowed",
    "list_new_payment_articles",
    "list_new_payment_employees",
    "list_payout_attribution_employees",
    "list_new_payment_wallets",
    "new_payment_article_flow",
]
