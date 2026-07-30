"""Налоговый факт → проводки ДДС (журнал «Деньги»).

Зачем. Платёж в бюджет создаётся в модуле «Налоги» (``TaxBankDraft`` → Т-Банк), а из выписки
возвращается обычной строкой ``bank_operations``. Факт-слой (``bank_facts``) её узнаёт и
раскладывает по видам налога, но в ДДС до сих пор не писал ничего: связь была односторонней,
и деньги оседали в «Требует разбора» — операции 29.07.2026 на 493 378,30 ₽ висели там при
живом черновике со статусом ``paid``. Здесь — вторая сторона связи.

Как. Декларативно и от СОСТОЯНИЯ, а не от события: каждый прогон вычисляет желаемый разнос
из строк ``tax_payment`` и приводит к нему фактический разнос операции. Маркера «обработано»
нет — отбор ``_operations_out_of_sync`` сам находит расхождение. Поэтому одним кодом
закрываются пять сценариев: первичный разнос, дозревание ЕНП (оборотка пришла позже —
строки факта пересобраны, ссылки пусты), повтор после сбоя, ручное удаление проводки
(FK ``ondelete='SET NULL'`` возвращает операцию в отбор) и бэкфилл истории.

Одна строка факта — одна проводка ДДС (решение владельца 30.07.2026). Зарплатный ЕНП
14 902,30 ₽ виден в журнале двумя строками — «НДФЛ 6 331» и «Страховые взносы за работников
8 571,30» — хотя обе ведут в статью «Налоги с з/п»: владельцу нужен состав платежа на экране,
а не только в налоговом модуле.

Границы. Разнос — это АНАЛИТИКА: баланс банковского кошелька считается от выписки, поэтому
проводки денег не двигают. Контрагент у налоговой проводки не ставится намеренно — иначе
правило 1 завело бы дебиторку на ФНС и принялось гасить ею чужую кредиторку. Ручную разметку
владельца автомат не перебивает никогда: увидев чужую или доработанную строку, проектор
отступает и фиксирует это в отчёте.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import BankOperation, CashflowTransaction, DdsArticle, ReconciliationCase
from app.models.tax import TaxPayment

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

# Статьи каталога ДДС, в которые ложатся налоги.
TAX_ARTICLE_CODE = "tax_payment"  # «Налоги»
PAYROLL_TAX_ARTICLE_CODE = "nalogi_s_z_p"  # «Налоги с з/п»

# Зарплатный контур: НДФЛ и взносы за наёмный персонал.
PAYROLL_KINDS = frozenset({"ndfl", "contrib_employees", "contrib_injury", "enp_payroll"})
# Собственные налоги ИП. Взносы «за себя» владелец отнёс к «Налогам» (решение 30.07.2026):
# «Налоги с з/п» остаются строго про наёмный персонал.
OWN_TAX_KINDS = frozenset({"usn_advance", "contrib_fixed", "contrib_extra_1pct"})

# Метка проводок проектора. Не 'auto': авто-строки выбирает поглотитель платежей поставщикам
# (``absorb_auto_classified_counterparty_payment``), и налоговая строка попала бы под него.
PROJECTOR_QUALITY = "final"
# Чьи строки автомат вправе пересобрать. Всё остальное (ручной разбор, исключение,
# плейсхолдер пендинг-чека) принадлежит человеку или другому контуру.
REWRITABLE_QUALITY = frozenset({PROJECTOR_QUALITY, "auto"})

# Подписи видов для журнала ДДС — человек должен читать строку без словаря.
KIND_TITLES = {
    "usn_advance": "УСН",
    "contrib_fixed": "Взносы ИП «за себя»",
    "contrib_extra_1pct": "Взносы ИП 1 % с превышения",
    "ndfl": "НДФЛ",
    "contrib_employees": "Страховые взносы за работников",
    "contrib_injury": "Взносы на травматизм",
    "enp_payroll": "Зарплатный ЕНП",
    "penalty": "Пени и штрафы",
    "other": "Налоговый платёж",
}


def article_code_for(kind: str, recipient: str | None) -> str:
    """Статья ДДС по виду налога. Неизвестный вид — в родовую «Налоги», не падаем."""
    if kind in PAYROLL_KINDS:
        return PAYROLL_TAX_ARTICLE_CODE
    if kind in OWN_TAX_KINDS:
        return TAX_ARTICLE_CODE
    if kind == "penalty" and recipient == "sfr":
        # Пени наследуют контур своего платежа: в СФР их платят по зарплатным взносам.
        return PAYROLL_TAX_ARTICLE_CODE
    return TAX_ARTICLE_CODE


def kind_title(kind: str) -> str:
    return KIND_TITLES.get(kind, "Налоговый платёж")


@dataclass
class TaxDdsProjectionReport:
    """Итог прогона — для лога ингеста, вывода CLI и тестов."""

    operations_seen: int = 0
    projected: int = 0  # разнесено или пересобрано
    unchanged: int = 0  # разнос уже верен, поправлены только ссылки
    skipped: int = 0  # чужая разметка, нет статьи/кошелька, сумма не сходится
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "operations_seen": self.operations_seen,
            "projected": self.projected,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "details": self.details,
        }


@dataclass(frozen=True)
class _Article:
    id: UUID
    location_required: bool


@dataclass(frozen=True)
class _DesiredLine:
    """Желаемая доля разноса: статья, сумма и строки факта, которые она представляет."""

    article_id: UUID
    amount: Decimal
    comment: str
    fact_ids: tuple[UUID, ...]


async def project_tax_facts_to_dds(session: AsyncSession) -> TaxDdsProjectionReport:
    """Привести разнос ДДС налоговых операций в соответствие со строками фактов.

    Не коммитит: вызывается внутри чужой транзакции (приём выписки, минутный поллинг, CLI).
    Каждая операция обрабатывается в своём SAVEPOINT — сбой на одной не отравляет
    транзакцию приёма выписки (иначе ``commit`` вебхука упал бы 500-й, и банк отключил
    бы endpoint — инцидент 30.06.2026).
    """
    report = TaxDdsProjectionReport()
    settings = get_settings()
    if not settings.tax_dds_projection_enabled:
        return report

    # Сериализуем проекцию: ингест, минутный добор и CLI могут идти одновременно.
    # Лок именно НЕблокирующий. Он живёт до конца чужой транзакции (приём выписки), а
    # ждать её нельзя: вебхук банка уже держит `bank_ingest:<провайдер>`, таймаутов на
    # соединении нет, и блокирующий лок (а) заставил бы вебхук Т-Банка ждать сберовский
    # часовой синк, (б) давал бы цикл блокировок с минутным добором, который берёт строку
    # операции под FOR UPDATE. Проиграть гонку не страшно: контур работает от состояния,
    # и следующий прогон (минутный или очередной вебхук) доберёт всё сам.
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext('tax_dds_projection'))")
    )
    if not acquired:
        return report

    operations = await _operations_out_of_sync(session, since=settings.tax_dds_projection_since)
    report.operations_seen = len(operations)
    if not operations:
        return report

    articles = await _article_index(session)
    for operation in operations:
        try:
            async with session.begin_nested():
                await _project_one(session, operation, articles, report)
        except Exception:  # noqa: BLE001 — приём выписки важнее доводки аналитики
            logger.warning(
                "проекция налогов в ДДС: операция %s не разнесена", operation.id, exc_info=True
            )
            report.skipped += 1
            report.details.append(
                f"{operation.operation_date} {operation.amount} ₽: ошибка разноса"
            )
    await session.flush()
    return report


async def _operations_out_of_sync(
    session: AsyncSession, *, since: date | None
) -> list[BankOperation]:
    """Операции с налоговыми фактами, чей разнос в ДДС отстал от этих фактов.

    Три критерия — ровно то, что делает контур самозалечивающимся:
      (а) есть строка факта без своей проводки — первичный разнос, дозревание ЕНП
          (``_ripen_review_bundles`` пересоздаёт строки с пустой ссылкой) и возврат
          операции после ручного удаления проводки (FK ``SET NULL``);
      (б) операция ещё не ``classified`` — пришла вебхуком раньше реквизитов получателя и
          осела в ``needs_review``, а факты по ней появились позже, с выпиской;
      (в) сумма фактов разъехалась с суммой операции — банк уточнил списание рефайром.
    """
    facts = (
        select(
            TaxPayment.bank_operation_id.label("operation_id"),
            func.sum(TaxPayment.amount).label("fact_sum"),
            func.count()
            .filter(TaxPayment.cashflow_transaction_id.is_(None))
            .label("unlinked"),
        )
        .where(
            TaxPayment.source_kind == "bank_statement",
            TaxPayment.bank_operation_id.isnot(None),
        )
        .group_by(TaxPayment.bank_operation_id)
        .subquery()
    )
    query = (
        select(BankOperation)
        .join(facts, facts.c.operation_id == BankOperation.id)
        .where(
            BankOperation.direction == "out",
            # Исключённые и транзитные операции размечает человек/контур переводов.
            BankOperation.classification_status.notin_(("excluded", "internal_transfer")),
            or_(
                facts.c.unlinked > 0,
                BankOperation.classification_status != "classified",
                facts.c.fact_sum != BankOperation.amount,
            ),
        )
        .order_by(BankOperation.operation_date, BankOperation.id)
    )
    if since is not None:
        query = query.where(BankOperation.operation_date >= since)
    return list((await session.scalars(query)).all())


async def _article_index(session: AsyncSession) -> dict[str, _Article]:
    """Налоговые статьи каталога по коду. Каталог правит владелец — резолвим мягко."""
    rows = (
        await session.scalars(
            select(DdsArticle).where(
                DdsArticle.code.in_((TAX_ARTICLE_CODE, PAYROLL_TAX_ARTICLE_CODE))
            )
        )
    ).all()
    return {
        row.code: _Article(id=row.id, location_required=bool(row.location_required))
        for row in rows
        if row.is_active
    }


async def _project_one(
    session: AsyncSession,
    operation: BankOperation,
    articles: dict[str, _Article],
    report: TaxDdsProjectionReport,
) -> None:
    from app.services.banking.classifier import (
        OperationSplitLine,
        _cashflow_row_has_dependents,
        _wallet_for_operation,
        apply_operation_split,
        close_reconciliation_case,
    )
    from app.services.taxes.concurrency import lock_row

    await lock_row(session, operation)
    facts = list(
        (
            await session.scalars(
                select(TaxPayment)
                .where(
                    TaxPayment.bank_operation_id == operation.id,
                    TaxPayment.source_kind == "bank_statement",
                )
                .order_by(TaxPayment.kind, TaxPayment.amount, TaxPayment.id)
            )
        ).all()
    )
    if not facts:
        return

    label = f"{operation.operation_date} {operation.amount} ₽"

    # --- инварианты до любой записи (validate-first, как в apply_operation_split) ---
    if sum((Decimal(str(f.amount)) for f in facts), ZERO) != Decimal(str(operation.amount)):
        report.skipped += 1
        report.details.append(f"{label}: сумма фактов не равна сумме операции")
        return
    if await _wallet_for_operation(session, operation) is None:
        report.skipped += 1
        report.details.append(f"{label}: не найден кошелёк операции")
        return

    desired = await _desired_lines(session, operation, facts, articles)
    if desired is None:
        report.skipped += 1
        report.details.append(f"{label}: статья для авторазноса недоступна")
        return

    # --- владение: автомат не перебивает человека ---
    actual = list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation.id,
                )
            )
        ).all()
    )
    # Проверяем КАЖДУЮ строку, в том числе ту, на которую уже ссылается факт: исключение
    # «своя — значит можно» опасно, потому что ссылку факта мог поставить предыдущий прогон,
    # отступивший перед разметкой владельца. Собственные строки проектора исключения не
    # требуют — они помечены `final` и проходят проверку по общему правилу.
    for transaction in actual:
        if (
            transaction.quality_status not in REWRITABLE_QUALITY
            or transaction.counterparty_id is not None
            or await _cashflow_row_has_dependents(session, transaction.id)
        ):
            # Строку держит человек или другой контур. Ссылки фактов уводим на якорь
            # операции, иначе она вечно возвращалась бы в отбор как «без проводки».
            _link_all(facts, operation.cashflow_transaction_id)
            report.skipped += 1
            report.details.append(f"{label}: разнос владельца сохранён")
            return

    if operation.cashflow_transaction_id is not None and operation.cashflow_transaction_id not in {
        transaction.id for transaction in actual
    }:
        # Операция уже привязана к чужой проводке (prebooked-платёж, доставшийся ей по FIFO
        # до того, как реквизиты дозалились) — отбирать её назад не наше дело.
        _link_all(facts, operation.cashflow_transaction_id)
        report.skipped += 1
        report.details.append(f"{label}: операция привязана к чужой проводке")
        return

    # --- разнос уже верен: чиним только ссылки и кейс ---
    if operation.classification_status == "classified" and _signature(actual) == _signature_of(
        desired
    ):
        _link_by_article(facts, actual, desired)
        await _close_case(session, operation, facts, close_reconciliation_case)
        report.unchanged += 1
        return

    await apply_operation_split(
        session,
        operation,
        splits=[
            OperationSplitLine(article_id=line.article_id, amount=line.amount, comment=line.comment)
            for line in desired
        ],
        counterparty_id=None,
        quality_status=PROJECTOR_QUALITY,
        actor_user_id=None,
    )
    created = list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation.id,
                )
            )
        ).all()
    )
    _link_by_article(facts, created, desired)
    await _close_case(session, operation, facts, close_reconciliation_case)
    report.projected += 1
    report.details.append(
        f"{label}: ДДС — {len(desired)} строк(а) ({', '.join(line.comment for line in desired)})"
    )


async def _desired_lines(
    session: AsyncSession,
    operation: BankOperation,
    facts: list[TaxPayment],
    articles: dict[str, _Article],
) -> list[_DesiredLine] | None:
    """Желаемый разнос: по одной доле на строку факта. None — статья недоступна."""
    immature = all(
        fact.kind == "other" and fact.quality_status == "requires_review" for fact in facts
    )
    if immature:
        # Состав платежа ещё неизвестен (оборотка бухгалтера не пришла), но получатель
        # определён по owner-locked реквизитам, то есть «деньги ушли в бюджет» — факт.
        # Держать достоверный расход в «Требует разбора» — ровно та ошибка, из-за которой
        # платежи и потерялись. Подсказку о виде даёт наш же черновик: он на этот момент
        # ещё in_bank (факт-слой не гасит черновик, пока не разнесёт).
        from app.services.taxes.bank_facts import _match_draft, _recipient_for_operation

        recipient = _recipient_for_operation(operation)
        draft = None
        if recipient is not None:
            draft = await _match_draft(
                session,
                amount=Decimal(str(operation.amount)),
                recipient=recipient,
                op_date=operation.operation_date,
                provider=operation.provider,
                document_number=operation.document_number,
            )
        hint_kind = draft.tax_kind if draft is not None else "other"
        article = articles.get(article_code_for(hint_kind, recipient))
        if article is None or article.location_required:
            return None
        comment = (
            f"{kind_title(hint_kind)}: состав уточнится по документам"
            if draft is not None
            else "Налоговый платёж: состав не определён"
        )
        return [
            _DesiredLine(
                article_id=article.id,
                amount=Decimal(str(operation.amount)),
                comment=comment,
                fact_ids=tuple(fact.id for fact in facts),
            )
        ]

    lines: list[_DesiredLine] = []
    for fact in facts:
        article = articles.get(article_code_for(fact.kind, fact.recipient))
        if article is None or article.location_required:
            return None
        lines.append(
            _DesiredLine(
                article_id=article.id,
                amount=Decimal(str(fact.amount)),
                comment=kind_title(fact.kind),
                fact_ids=(fact.id,),
            )
        )
    return lines


def _signature(transactions: list[CashflowTransaction]) -> list[tuple[str, str, str]]:
    # Комментарий — часть подписи: платёж, разнесённый до прихода оборотки, подписан
    # «состав уточнится по документам», и когда состав становится известен, статья с суммой
    # могут не измениться (обе части ЕНП ведут в «Налоги с з/п»). Без комментария в подписи
    # такой платёж навсегда остался бы в журнале с устаревшим пояснением.
    return sorted(
        (str(t.article_id), str(Decimal(str(t.amount))), t.comment or "") for t in transactions
    )


def _signature_of(lines: list[_DesiredLine]) -> list[tuple[str, str, str]]:
    return sorted((str(line.article_id), str(line.amount), line.comment) for line in lines)


def _link_all(facts: list[TaxPayment], transaction_id: UUID | None) -> None:
    for fact in facts:
        fact.cashflow_transaction_id = transaction_id


def _link_by_article(
    facts: list[TaxPayment],
    transactions: list[CashflowTransaction],
    desired: list[_DesiredLine],
) -> None:
    """Связать каждую строку факта с ЕЁ проводкой — повидово, а не якорем операции.

    Матчим по (статья, сумма): проводки только что созданы из этих же долей, поэтому пара
    находится однозначно; израсходованные проводки исключаем, чтобы две одинаковые доли
    (например травматизм двух месяцев одной суммы) не сели на одну строку.
    """
    pool = list(transactions)
    for line in desired:
        match = next(
            (
                transaction
                for transaction in pool
                if transaction.article_id == line.article_id
                and Decimal(str(transaction.amount)) == line.amount
            ),
            None,
        )
        if match is None:
            continue
        pool.remove(match)
        for fact in facts:
            if fact.id in line.fact_ids:
                fact.cashflow_transaction_id = match.id


async def _close_case(
    session: AsyncSession,
    operation: BankOperation,
    facts: list[TaxPayment],
    close_reconciliation_case,
) -> None:
    """Закрыть кейс «требует разбора» этой операции — иначе он висит в owner-review."""
    case = await session.scalar(
        select(ReconciliationCase).where(
            ReconciliationCase.bank_operation_id == operation.id,
            ReconciliationCase.kind == "unclassified_operation",
            ReconciliationCase.status == "pending",
        )
    )
    if case is None:
        return
    await close_reconciliation_case(
        session,
        case,
        status="resolved",
        resolution_payload={
            "action": "tax_fact_projection",
            "kinds": sorted({fact.kind for fact in facts}),
            "tax_payment_ids": [str(fact.id) for fact in facts],
        },
    )


async def sync_tax_bank_pipeline(session: AsyncSession) -> dict:
    """Факт-слой + проекция в ДДС одним вызовом (ингест, минутный добор, CLI).

    Ни один шаг не поднимает исключение наружу: приём выписки важнее доводки — недоделанное
    доберёт следующий прогон, оба сервиса идемпотентны и работают от состояния.
    """
    result: dict = {"tax_facts": None, "tax_dds": None}
    # SAVEPOINT вокруг КАЖДОГО шага обязателен, а не только except. Ошибка уровня БД
    # переводит транзакцию в aborted, и голый except лишь проглотил бы исключение — упал бы
    # следующий flush вызывающего, то есть приём выписки: вебхук ответил бы 500, а банк
    # после серии отказов отключает endpoint (инцидент 30.06.2026). Откат до савпоинта
    # оставляет транзакцию приёма живой.
    try:
        from app.services.taxes.bank_facts import sync_tax_facts_from_bank

        async with session.begin_nested():
            result["tax_facts"] = (await sync_tax_facts_from_bank(session)).as_dict()
    except Exception:  # noqa: BLE001
        logger.warning("bank ingest: налоговый факт-слой не отработал", exc_info=True)
    try:
        async with session.begin_nested():
            result["tax_dds"] = (await project_tax_facts_to_dds(session)).as_dict()
    except Exception:  # noqa: BLE001
        logger.warning("bank ingest: проекция налогов в ДДС не отработала", exc_info=True)
    return result
