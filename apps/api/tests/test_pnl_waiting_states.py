"""Состояние ожидания документа в ОПиУ: тревога — только просрочка.

До 25.09.2026 каждое «оплачено, документа нет» становилось предупреждением. За сентябрь их
набралось восемь строк на 334 459,84 ₽ — и ни одной настоящей: период ещё шёл, документы
АЙКО, ЧОО, СПЕЦАВТО и ЭкоЦентра уже лежали отложенными до 01.10, а аренду начисляет договор.
Единственная настоящая пропажа того же отчёта — 100 ₽ ЛИКАРДа, оплаченные 19.08 без
документа, — выглядела бы в этом списке ровно так же, как законные ожидания.

Правило состояния проверяется чистыми функциями (даты, а не фикстуры); сборка из ДЗ/КЗ —
на базе, потому что вся ошибка там в том, ЧТО считать «документ уже получен».
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

sys.path.append(str(Path(__file__).parent / "counterparties"))

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    DdsArticle,
    Location,
    Organization,
    PnlArticleRule,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    UtilityAccount,
)
from app.services.pnl import projector
from app.services.pnl.sources import waiting as waiting_source
from app.services.pnl.sources.waiting import (
    STATE_AWAITING,
    STATE_DOCUMENT_PENDING,
    STATE_OVERDUE,
    STATE_PERIOD_RUNNING,
    WaitingItem,
    WaitingLayer,
    waiting_state,
)
from app.services.pnl.types import Component, LineStatus, LineValue

SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
#: День разбора прода — 25.09.2026.
TODAY = date(2026, 9, 25)


class TestWaitingState:
    """Какое из четырёх состояний у ожидания. Даты — с прода сентября 2026."""

    def test_running_period_is_not_overdue(self) -> None:
        # Реклама О.О за сентябрь, 25.09: услуга ещё оказывается, документа быть не может.
        assert waiting_state(
            period_end=date(2026, 9, 30),
            incoming_on=None,
            deadline=date(2026, 10, 10),
            today=TODAY,
        ) == (STATE_PERIOD_RUNNING, date(2026, 9, 30), 0)

    def test_received_document_wins_over_running_period(self) -> None:
        # Акт АЙКО за сентябрь датирован 01.09 и лежит отложенным: «вступит 01.10» говорит
        # владельцу больше, чем «период идёт».
        assert waiting_state(
            period_end=date(2026, 9, 30),
            incoming_on=date(2026, 10, 1),
            deadline=date(2026, 10, 10),
            today=TODAY,
        ) == (STATE_DOCUMENT_PENDING, date(2026, 10, 1), 0)

    def test_after_period_but_before_deadline_is_awaiting(self) -> None:
        assert waiting_state(
            period_end=date(2026, 9, 30),
            incoming_on=None,
            deadline=date(2026, 10, 10),
            today=date(2026, 10, 10),
        ) == (STATE_AWAITING, date(2026, 10, 10), 0)

    def test_deadline_day_itself_is_still_awaiting(self) -> None:
        # «Присылает 10-го» и «просрочил 10-го» — не одно и то же: краснеет со следующего дня.
        state, _on, days = waiting_state(
            period_end=date(2026, 8, 31),
            incoming_on=None,
            deadline=date(2026, 9, 10),
            today=date(2026, 9, 11),
        )
        assert (state, days) == (STATE_OVERDUE, 1)

    def test_likard_august_is_overdue_by_fifteen_days(self) -> None:
        # ЛИКАРД: 100 ₽ 19.08 без периода → август, срок 10.09, на 25.09 — 15 дней.
        assert waiting_state(
            period_end=date(2026, 8, 31),
            incoming_on=None,
            deadline=date(2026, 9, 10),
            today=TODAY,
        ) == (STATE_OVERDUE, date(2026, 9, 10), 15)


class TestStateLabel:
    """Одни и те же слова в строке отчёта, расшифровке и реестре."""

    def _item(self, **kwargs) -> WaitingItem:
        defaults = {
            "prepayment_id": uuid.uuid4(),
            "counterparty_id": uuid.uuid4(),
            "article_id": None,
            "amount": Decimal("100.00"),
            "paid_on": None,
            "period_start": None,
            "period_end": None,
            "period_known": False,
        }
        return WaitingItem(**{**defaults, **kwargs})

    def test_rent_is_not_called_a_received_document(self) -> None:
        # Арендодатель документов не выставляет — «документ получен» об аренде был бы неправдой.
        item = self._item(
            state=STATE_DOCUMENT_PENDING,
            state_date=date(2026, 10, 1),
            basis=waiting_source.BASIS_AGREEMENT,
        )
        assert waiting_source.state_label(item) == "начислится по договору 01.10"

    def test_received_document(self) -> None:
        item = self._item(
            state=STATE_DOCUMENT_PENDING,
            state_date=date(2026, 10, 1),
            basis=waiting_source.BASIS_DOCUMENT,
        )
        assert waiting_source.state_label(item) == "документ получен, вступит 01.10"

    def test_overdue_names_days_and_deadline(self) -> None:
        item = self._item(state=STATE_OVERDUE, state_date=date(2026, 9, 10), overdue_days=15)
        assert waiting_source.state_label(item) == "документ просрочен на 15 дн. (ждали до 10.09)"


def _line(code: str) -> LineValue:
    return LineValue(
        code=code,
        title=f"Строка {code}",
        block="test",
        kind="source",
        level=1,
        sort_order=1,
        sign_role=-1,
        month_basis="document",
        amount=None,
        status=LineStatus.NOT_CONFIGURED,
    )


def _waiting(article_id: uuid.UUID, amount: str, state: str, **kwargs) -> WaitingItem:
    return WaitingItem(
        prepayment_id=uuid.uuid4(),
        counterparty_id=uuid.uuid4(),
        article_id=article_id,
        amount=Decimal(amount),
        paid_on=None,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        period_known=True,
        state=state,
        **kwargs,
    )


class TestWaitingOnTheLine:
    """Ожидание — пометка на строке по состояниям, тревога — только просрочка."""

    ARTICLE = uuid.uuid4()

    def _layer(self, *items: WaitingItem) -> WaitingLayer:
        layer = WaitingLayer()
        layer.items.extend(items)
        return layer

    def test_automation_september_is_two_notes_not_one_alarm(self) -> None:
        # «Оплата систем автоматизации», сентябрь: акты АЙКО получены (20 690 ₽), по
        # ДоксИнБоксу и Леме идёт период (20 530 ₽). Одна сумма на строку соврала бы про половину.
        lines = {"automation": _line("automation")}
        layer = self._layer(
            _waiting(
                self.ARTICLE,
                "16430.00",
                STATE_DOCUMENT_PENDING,
                state_date=date(2026, 10, 1),
                basis=waiting_source.BASIS_DOCUMENT,
            ),
            _waiting(
                self.ARTICLE,
                "4260.00",
                STATE_DOCUMENT_PENDING,
                state_date=date(2026, 10, 1),
                basis=waiting_source.BASIS_DOCUMENT,
            ),
            _waiting(self.ARTICLE, "16830.00", STATE_PERIOD_RUNNING, state_date=date(2026, 9, 30)),
            _waiting(self.ARTICLE, "3700.00", STATE_PERIOD_RUNNING, state_date=date(2026, 9, 30)),
        )
        article_lines = {self.ARTICLE: "automation"}

        projector._apply_waiting(lines, layer, article_lines)

        notes = {
            component.note: component.unrecognized_paid
            for component in lines["automation"].components
        }
        assert notes == {
            "документ получен, вступит 01.10": Decimal("20690.00"),
            "период идёт до 30.09": Decimal("20530.00"),
        }
        assert all(
            component.status is LineStatus.WAITING_DOCUMENT
            for component in lines["automation"].components
        )
        assert projector._overdue_document_warnings(layer, article_lines, lines) == []

    def test_only_overdue_raises_warning_and_names_counterparty(self) -> None:
        lines = {"shop_maintenance": _line("shop_maintenance")}
        layer = self._layer(
            _waiting(
                self.ARTICLE,
                "100.00",
                STATE_OVERDUE,
                state_date=date(2026, 9, 10),
                overdue_days=15,
                counterparty_name="ООО «ЛИКАРД»",
            ),
            _waiting(
                self.ARTICLE,
                "2300.00",
                STATE_DOCUMENT_PENDING,
                state_date=date(2026, 10, 1),
                basis=waiting_source.BASIS_DOCUMENT,
            ),
        )
        article_lines = {self.ARTICLE: "shop_maintenance"}

        projector._apply_waiting(lines, layer, article_lines)
        warnings = projector._overdue_document_warnings(layer, article_lines, lines)

        assert [warning.code for warning in warnings] == ["overdue_document"]
        assert warnings[0].amount == Decimal("100.00")
        assert "ООО «ЛИКАРД» — 100,00 ₽ (ждали до 10.09, просрочка 15 дн.)" in warnings[0].message
        overdue = [
            component
            for component in lines["shop_maintenance"].components
            if component.waiting_state == STATE_OVERDUE
        ]
        assert [component.status for component in overdue] == [LineStatus.OVERDUE_DOCUMENT]

    def test_overdue_notes_of_one_line_merge_under_the_oldest_deadline(self) -> None:
        lines = {"telecom": _line("telecom")}
        layer = self._layer(
            _waiting(
                self.ARTICLE, "500.00", STATE_OVERDUE, state_date=date(2026, 9, 10), overdue_days=3
            ),
            _waiting(
                self.ARTICLE, "700.00", STATE_OVERDUE, state_date=date(2026, 8, 10), overdue_days=34
            ),
        )

        projector._apply_waiting(lines, layer, {self.ARTICLE: "telecom"})

        [component] = lines["telecom"].components
        assert component.unrecognized_paid == Decimal("1200.00")
        assert component.note == "документ просрочен на 34 дн. (ждали до 10.08)"

    def test_zero_line_waiting_for_overdue_document_says_so(self) -> None:
        line = _line("telecom")
        line.components.append(
            Component(
                stream="cashflow", component="main", amount=Decimal("0.00"), status=LineStatus.OK
            )
        )
        layer = self._layer(
            _waiting(
                self.ARTICLE, "100.00", STATE_OVERDUE, state_date=date(2026, 9, 10), overdue_days=1
            ),
            _waiting(self.ARTICLE, "900.00", STATE_PERIOD_RUNNING, state_date=date(2026, 9, 30)),
        )
        lines = {"telecom": line}

        projector._apply_waiting(lines, layer, {self.ARTICLE: "telecom"})
        projector._collapse(line)

        assert line.status is LineStatus.OVERDUE_DOCUMENT


# --- Сборка из ДЗ/КЗ ---------------------------------------------------------------------


async def _article(session, line_code: str) -> DdsArticle:
    article = DdsArticle(
        code=f"test_wait_{uuid.uuid4().hex[:8]}",
        name=f"Статья {line_code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code=line_code,
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


async def _prepaid(
    session,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID,
    amount: str,
    period: tuple[date, date],
) -> SupplierPrepayment:
    """Оплаченный счёт с известным периодом — так на проде живут АЙКО, ЧОО, реклама."""
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="prepaid_bill",
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        article_id=article_id,
        service_period_start=period[0],
        service_period_end=period[1],
        service_period_status="ready",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def _advance(
    session, counterparty_id: uuid.UUID, article_id: uuid.UUID, amount: str, paid_on: date
) -> SupplierPrepayment:
    """Аванс без периода с денежной проводкой — так живут аренда, Манго, ЛИКАРД."""
    from cp_helpers import make_wallet

    wallet = await make_wallet(session, code=f"wait-{uuid.uuid4().hex[:6]}")
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=paid_on,
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind="bank_feed",
        payment_purpose="Оплата",
        quality_status="manual_override",
    )
    session.add(transaction)
    await session.flush()
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="subscription",
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        article_id=article_id,
        cashflow_transaction_id=transaction.id,
        service_period_status="missing",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def _pending_closing(
    session,
    counterparty_id: uuid.UUID,
    amount: str,
    *,
    invoice_date: date,
    period: tuple[date, date],
    source: str = "email",
    article_id: uuid.UUID | None = None,
    with_accrual: bool = True,
) -> SupplierInvoice:
    """Закрывающий, пришедший раньше конца услуги: ``activation_status='pending'``."""
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source=source,
        doc_kind="closing",
        activation_status="pending",
        number=f"УТ-{uuid.uuid4().hex[:4]}",
        amount=Decimal(amount),
        invoice_date=invoice_date,
        service_period_start=period[0],
        service_period_end=period[1],
        service_period_status="ready",
        dds_article_id=article_id,
    )
    session.add(invoice)
    await session.flush()
    if with_accrual:
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=counterparty_id,
                invoice_id=invoice.id,
                article_id=article_id,
                amount=Decimal(amount),
                status="scheduled",
                service_period_start=period[0],
                service_period_end=period[1],
            )
        )
        await session.flush()
    return invoice


async def _utility_account(
    session, counterparty_id: uuid.UUID, article_id: uuid.UUID, expected_day: int
) -> None:
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    location = Location(id=uuid.uuid4(), organization_id=organization_id, name="Черникова")
    session.add(location)
    await session.flush()
    session.add(
        UtilityAccount(
            location_id=location.id,
            counterparty_id=counterparty_id,
            kind="electricity",
            dds_article_id=article_id,
            expected_day=expected_day,
            started_on=date(2026, 1, 1),
        )
    )
    await session.flush()


async def _layer(session, month: tuple[date, date], today: date) -> WaitingLayer:
    return await waiting_source.build_waiting_layer(session, *month, today=today)


def _by_counterparty(layer: WaitingLayer) -> dict[str, WaitingItem]:
    return {item.counterparty_name: item for item in layer.items}


def test_pending_document_reads_as_received(async_session_factory) -> None:
    """Акт АЙКО за сентябрь датирован 01.09 и лежит отложенным — это не «документа нет».

    У акта нет ни своей статьи, ни статьи в карточке: такой документ закрывает аванс своего
    контрагента по любой статье — именно его и гасит лестница адресности 01.10.
    """
    from cp_helpers import make_counterparty

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, "automation")
            aiko = await make_counterparty(session, name="АО «АЙКО»", inn="7705840000")
            await _prepaid(session, aiko.id, article.id, "16430.00", SEPTEMBER)
            await _pending_closing(
                session, aiko.id, "16430.00", invoice_date=date(2026, 9, 1), period=SEPTEMBER
            )
            await session.commit()

            item = _by_counterparty(await _layer(session, SEPTEMBER, TODAY))["АО «АЙКО»"]

            assert (item.state, item.state_date, item.basis) == (
                STATE_DOCUMENT_PENDING,
                date(2026, 10, 1),
                waiting_source.BASIS_DOCUMENT,
            )
            assert waiting_source.state_label(item) == "документ получен, вступит 01.10"

    asyncio.run(scenario())


def test_rent_is_accrued_by_lease_and_does_not_cover_utilities(async_session_factory) -> None:
    """Аренду начислит договор; она же не вправе объявить полученным документ по коммуналке.

    Арендодатель Виталий: аренда 50 000 ₽ 01.09 без периода, «Аренда 09.2026» лежит
    отложенной до 01.10. Рядом — 70 000 ₽ коммуналки за сентябрь, документ по которой приходит
    к 20-му числу следующего месяца. Щит по контрагенту целиком уже ломался дважды (сверка
    непризнанного расхода, ``month_share``) — здесь он ломался бы в третий раз.
    """
    from cp_helpers import make_counterparty

    async def scenario() -> None:
        async with async_session_factory() as session:
            rent = await _article(session, "rent_chernikova")
            utilities = await _article(session, "utilities_chernikova")
            landlord = await make_counterparty(session, name="Виталий", cp_type="individual")
            await _advance(session, landlord.id, rent.id, "50000.00", date(2026, 9, 1))
            await _prepaid(session, landlord.id, utilities.id, "70000.00", SEPTEMBER)
            await _pending_closing(
                session,
                landlord.id,
                "50000.00",
                invoice_date=date(2026, 9, 30),
                period=SEPTEMBER,
                source="lease",
                article_id=rent.id,
            )
            await _utility_account(session, landlord.id, utilities.id, expected_day=20)
            await session.commit()

            items = {
                item.article_id: item for item in (await _layer(session, SEPTEMBER, TODAY)).items
            }
            assert waiting_source.state_label(items[rent.id]) == "начислится по договору 01.10"
            assert items[utilities.id].state == STATE_PERIOD_RUNNING

            # После конца периода срок коммуналки — 20-е число потока, а не общие 10 дней.
            october_15 = {
                item.article_id: item
                for item in (await _layer(session, SEPTEMBER, date(2026, 10, 15))).items
            }
            assert (october_15[utilities.id].state, october_15[utilities.id].state_date) == (
                STATE_AWAITING,
                date(2026, 10, 20),
            )
            october_25 = {
                item.article_id: item
                for item in (await _layer(session, SEPTEMBER, date(2026, 10, 25))).items
            }
            assert (october_25[utilities.id].state, october_25[utilities.id].overdue_days) == (
                STATE_OVERDUE,
                5,
            )

    asyncio.run(scenario())


def test_document_of_another_month_does_not_close_waiting(async_session_factory) -> None:
    """Отложенный октябрьский акт не отвечает за сентябрь — период должен пересекаться."""
    from cp_helpers import make_counterparty

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, "seo")
            vendor = await make_counterparty(session, name="Синапсис", inn="7705840001")
            await _prepaid(session, vendor.id, article.id, "13000.00", SEPTEMBER)
            await _pending_closing(
                session,
                vendor.id,
                "13000.00",
                invoice_date=date(2026, 10, 31),
                period=(date(2026, 10, 1), date(2026, 10, 31)),
            )
            await session.commit()

            [item] = (await _layer(session, SEPTEMBER, date(2026, 10, 12))).items
            assert (item.state, item.overdue_days) == (STATE_OVERDUE, 2)

    asyncio.run(scenario())


def test_counterparty_expected_day_moves_the_deadline(async_session_factory) -> None:
    """Контрагент, который присылает документ 25-го, не краснеет с 11-го."""
    from cp_helpers import make_counterparty

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, "telecom")
            vendor = await make_counterparty(session, name="Микроэл", inn="7705840002")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == vendor.id
                )
            )
            profile.closing_doc_expected_day = 25
            await _prepaid(session, vendor.id, article.id, "3230.00", AUGUST)
            await session.commit()

            [item] = (await _layer(session, AUGUST, TODAY)).items
            assert (item.state, item.state_date) == (STATE_AWAITING, date(2026, 9, 25))
            [item] = (await _layer(session, AUGUST, TODAY + timedelta(days=1))).items
            assert (item.state, item.overdue_days) == (STATE_OVERDUE, 1)

    asyncio.run(scenario())


def test_report_raises_only_the_overdue_document(async_session_factory) -> None:
    """Сверка с прода: август — единственная тревога ЛИКАРД, сентябрь — ни одной.

    ЛИКАРД заплачен 19.08 без периода: ожидание относится к августу, срок — 10.09, на 25.09
    документа нет. АЙКО за сентябрь уже прислал акт, он вступит 01.10. Прежний отчёт
    называл оба «закрывающего документа ещё нет» одинаково.
    """
    from cp_helpers import make_counterparty

    async def scenario() -> None:
        async with async_session_factory() as session:
            shop = await _article(session, "shop_maintenance")
            automation = await _article(session, "automation")
            likard = await make_counterparty(session, name="ООО «ЛИКАРД»", inn="7705840003")
            aiko = await make_counterparty(session, name="АО «АЙКО»", inn="7705840004")
            await _advance(session, likard.id, shop.id, "100.00", date(2026, 8, 19))
            await _prepaid(session, aiko.id, automation.id, "4260.00", SEPTEMBER)
            await _pending_closing(
                session, aiko.id, "4260.00", invoice_date=date(2026, 9, 1), period=SEPTEMBER
            )
            await session.commit()

            august = await projector.build_report(session, date(2026, 8, 1), today=TODAY)
            september = await projector.build_report(session, date(2026, 9, 1), today=TODAY)

            overdue = [w for w in august.warnings if w.code == "overdue_document"]
            assert len(overdue) == 1
            assert overdue[0].line_code == "shop_maintenance"
            assert overdue[0].amount == Decimal("100.00")
            assert "ООО «ЛИКАРД»" in overdue[0].message
            assert "просрочка 15 дн." in overdue[0].message
            # Сентябрь не тревожит ничем, а старого «закрывающего документа ещё нет» не
            # осталось ни в одном месяце.
            assert not [
                w for w in september.warnings if w.code in {"waiting_document", "overdue_document"}
            ]
            assert not [w for w in august.warnings if w.code == "waiting_document"]

            automation_line = next(line for line in september.lines if line.code == "automation")
            [note] = [c for c in automation_line.components if c.unrecognized_paid > 0]
            assert (note.waiting_state, note.note) == (
                STATE_DOCUMENT_PENDING,
                "документ получен, вступит 01.10",
            )

    asyncio.run(scenario())
