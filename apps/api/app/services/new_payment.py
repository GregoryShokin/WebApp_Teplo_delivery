"""Окно «Новый платёж»: контекст формы и маршрутизация «статья ДДС → механизм».

Единое окно создаёт банковские черновики существующими механизмами; драйвер — статья:

- ``expense`` — статья без получателя («просто трата»): via-safe черновик на карту ИП
  (``counterparty_payments.create_expense_payment_draft``), paid-переход заводит
  транзит р/с→Сейф и целёвку этой статьи;
- ``employee_payout`` — зарплатные статьи: разовая выплата сотруднику
  (``employee_payouts``, только on_demand);
- ``employee_advance`` / ``employee_loan`` — аванс/заём (``payroll_advance_service``);
- ``supplier_prepayment`` — предоплата поставщику (``create_standalone_payment_draft``).

Селект статей собирается ПО ПРАВАМ пользователя: каждый маршрут виден только при его
операционном праве. Внутри своего маршрута каталог ДДС открыт ЦЕЛИКОМ: любая активная
статья доступна к оплате — расходная свободным выводом, приходная ручным поступлением.
Флаг «доступна в кассе» на окно не влияет (он лишь пускает статью в форму «Выплата из
кассы»), собственные контуры выдачи статью тоже не прячут: у депозитов, ЗП и накладных
свои механизмы гашения долга, но заплатить по их статье из окна владелец вправе —
проводка ДДС встанет, леджер профильного модуля при этом не двигается.
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
from app.services.counterparty_registry import ARCHIVED_STATUSES, NON_PAYOUT_WALLET_CODES
from app.services.kassa.payouts import (
    EMPLOYEE_ADVANCE_ARTICLE_CODE,
    EMPLOYEE_LOAN_ARTICLE_CODE,
    SUPPLIER_PREPAYMENT_ARTICLE_CODE,
)
from app.services.payroll_admin import (
    PAYOUT_MODE_ON_DEMAND,
    _load_okladnik_payout_modes,
)

# Статьи-маршруты окна: у них в окне СВОЯ форма (не «просто трата»), поэтому маршрут
# определяется по коду. Зарплатные коды продублированы из профильных сервисов
# (employee_payouts / payroll_payout_allocation), чтобы не тянуть их тяжёлые модули на
# импорт; равенство закреплено тестами tests/counterparties/test_new_payment_window.py.
EMPLOYEE_PAYOUT_ARTICLE_CODES = (
    "zarplata_administrativnogo_personala",
    "zarplata_proizvodstvennogo_personala",
)

INTERNAL_TRANSFER_ARTICLE_CODE = "internal_transfer"

FLOW_BY_ARTICLE_CODE: dict[str, str] = {
    EMPLOYEE_ADVANCE_ARTICLE_CODE: "employee_advance",
    EMPLOYEE_LOAN_ARTICLE_CODE: "employee_loan",
    SUPPLIER_PREPAYMENT_ARTICLE_CODE: "supplier_prepayment",
    # Внутренний перевод — маршрут «Нового платежа»: наличный источник → двухногий
    # перевод; банк-источник → черновик-пополнение Сейфа (topup_only). Требует счёт-получатель.
    INTERNAL_TRANSFER_ARTICLE_CODE: "internal_transfer",
    **{code: "employee_payout" for code in EMPLOYEE_PAYOUT_ARTICLE_CODES},
}

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

    Каталог ДДС открыт целиком: любая активная статья платится из окна. Сначала
    статьи-маршруты по коду (у них в окне своя форма — аванс, заём, выплата по ЗП,
    предоплата поставщику, перевод), остальные — по направлению движения: расход →
    свободный вывод (``expense``), приход → ручное поступление (``income``).

    Флаги статьи маршрут не меняют: «доступна в кассе» — про форму «Выплата из кассы»,
    а не про банк; статьи с собственными контурами (депозиты, ЗП, накладные, движковые
    цели правил классификации) тоже доступны — ручной платёж по ним заводит проводку
    ДДС, но НЕ двигает леджер профильного модуля (долг по депозиту, ведомость,
    остаток накладной гасятся своими механизмами).
    """
    if not article.is_active:
        return None
    # Статьи-маршруты (в т.ч. «Внутренний перевод» с movement_type=internal) — по коду,
    # до гейта по направлению: у перевода своё направление на ногах, не «outflow».
    flow = FLOW_BY_ARTICLE_CODE.get(article.code)
    if flow is not None:
        return flow
    if article.movement_type == "inflow":
        return "income"
    if article.movement_type == "outflow":
        return "expense"
    return None


def ensure_expense_article_allowed(article: DdsArticle) -> None:
    """Статья годится для свободного вывода на Сейф (маршрут ``expense``)?

    Бэкенд-страховка симметрично фильтру селекта: свободным выводом не платятся только
    статьи-маршруты — у них в окне своя форма (аванс/заём/выплата по ЗП/предоплата
    поставщику/перевод), и голая трата по такой статье не завела бы ни удержание, ни
    дебиторку. Остальной каталог открыт.
    """
    if not article.is_active:
        raise ValueError("Статья ДДС неактивна")
    if article.movement_type != "outflow":
        raise ValueError("Свободный вывод на Сейф доступен только по расходным статьям")
    if new_payment_article_flow(article) != "expense":
        raise ValueError(
            "У этой статьи собственная форма в окне — свободный вывод на Сейф недоступен"
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

    Симметрично фильтру селекта: любая активная приходная статья каталога. Отсекаются
    только статьи-маршруты со своей формой (например «Внутренний перевод»).
    """
    if not article.is_active:
        raise ValueError("Статья ДДС неактивна")
    if article.movement_type != "inflow":
        raise ValueError("Поступление можно провести только по приходной статье")
    if new_payment_article_flow(article) != "income":
        raise ValueError("У этой статьи собственная форма в окне — ручной приход недоступен")


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
            CounterpartyPayableProfile.service_period_required,
            CounterpartyPayableProfile.default_service_period_offset_months,
        )
        .join(Counterparty, Counterparty.id == CounterpartyPayableProfile.counterparty_id)
        .where(
            CounterpartyPayableProfile.default_dds_article_id.in_(article_ids),
            # notin_: легаси-статус 'inactive' — тоже архив, в пикер попадать не должен.
            Counterparty.status.notin_(ARCHIVED_STATUSES),
        )
        .order_by(Counterparty.name)
    )
    by_article: dict[Any, list[dict[str, Any]]] = {}
    for (
        article_id,
        cp_id,
        name,
        inn,
        relationship,
        requisites,
        requisites_verified,
        service_period_required,
        default_service_period_offset_months,
    ) in rows:
        by_article.setdefault(article_id, []).append(
            {
                "counterparty_id": cp_id,
                "name": name,
                "inn": inn,
                "relationship": relationship,
                "has_requisites": bool(requisites),
                "requisites_verified": bool(requisites_verified),
                "service_period_required": bool(service_period_required),
                "default_service_period_offset_months": default_service_period_offset_months,
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
            "location_required": article.location_required,
            "lease_bound": article.lease_bound,
            # Без этого признака окно не знает, что статья требует объект, и покупка уходит
            # в расход мимо баланса. Ровно так поле уже терялось в /dds/articles.
            "asset_link_kind": article.asset_link_kind,
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
    "EMPLOYEE_PAYOUT_ARTICLE_CODES",
    "FLOW_BY_ARTICLE_CODE",
    "FLOW_PERMISSIONS",
    "NEW_PAYMENT_PERMISSION_CODES",
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
