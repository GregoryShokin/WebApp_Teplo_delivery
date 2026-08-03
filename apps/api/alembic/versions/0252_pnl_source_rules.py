"""Разметка источников ОПиУ: каждая статья ДДС получает строку отчёта либо явное «вне ОПиУ».

ЗАЧЕМ РАЗМЕЧАТЬ ВСЁ, ВКЛЮЧАЯ «ВНЕ ОПиУ». Уравнение сходимости требует, чтобы каждый рубль
оттока получил ровно один ярлык: попал в строку отчёта, был признан переводом/финансовой
операцией, либо был отдан другому слою (товар приходит фудкостом из iiko, зарплата — из
ведомости, налоги — из своего модуля). Тогда «не разнесено» обязано быть нулём, и потеря
перестаёт быть тихой. Задвоение шумит — расходятся итоги; потеря молчит, поэтому её ловят
тождеством, а не глазами.

РАЗМЕЧЕНЫ И НЕАКТИВНЫЕ СТАТЬИ. На проде две деактивированные статьи переводов между счетами
дали за июль 2026 оборот 857 860,72 и 799 360,72 ₽. Флаг ``is_active`` управляет тем, можно ли
выбрать статью в новой проводке, а не тем, есть ли по ней история. Размечать только активные
значило бы уронить сходимость на полутора миллионах.

ЧЕГО ЗДЕСЬ НЕТ. Правил для payroll, iiko, ОС, налогов и ревизий: они не привязаны к статьям
ДДС и заводятся вместе со своими адаптерами на следующем этапе. Здесь только денежный слой,
классификатор механизмов выдачи и таблица ручных чисел.

Revision ID: 0252_pnl_source_rules
Revises: 0251_pnl_lines
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0252_pnl_source_rules"
down_revision = "0251_pnl_lines"
branch_labels = None
depends_on = None


# Статья ДДС → строка ОПиУ. (code статьи, line_code | None, owner_stream | None, note)
# line_code = None означает in_pnl=false: статья никогда не расход и не доход отчёта.
IN_PNL: list[tuple[str, str, str | None]] = [
    # --- Выручка ---------------------------------------------------------------------
    ("komissiya_partneram", "partner_commission", None),
    ("vozvraty_klientam", "customer_refunds", None),
    ("customer_refund", "customer_refunds", "Устаревшая статья: история возвратов"),
    # --- Общепроизводственные --------------------------------------------------------
    ("arenda_torgovyh_tochek", "rent_chernikova", None),
    ("rent", "rent_chernikova", "Устаревшая статья аренды: история"),
    ("ekvairing", "acquiring", "Только комиссия по терминалам; онлайн-эквайринг и приём "
                               "платежей парсятся из назначений зачислений Сбера"),
    ("transportnye_uslugi", "transport", None),
    ("rashody_na_pitanie_personala", "staff_meals", None),
    ("oplaty_sistem_avtomatizacii", "automation", None),
    ("soderzhanie_torgovyh_tochek", "shop_maintenance", None),
    ("remont_oborudovaniya_0a2198", "equipment_repair", None),
    ("arenda_sklada", "warehouse_rent", None),
    ("nalogi_s_z_p", "payroll_taxes",
     "Из ДДС, а не из ведомости: взносы работодателя в ведомость не входят"),
    # --- Административные ------------------------------------------------------------
    ("kommunalnye_platezhi_0b0b4c", "utilities_chernikova", None),
    ("arenda_ofisa", "office_rent", None),
    ("soderzhanie_ofisa", "office_maintenance", None),
    ("bank_service_fee", "rko", None),
    ("bankovskie_komissi_2706ff", "bank_fees", None),
    ("uslugi_fd_i_nk", "fd_nk_services", None),
    ("rashody_na_personal", "staff_expenses", None),
    ("obuchenie_personala", "staff_training", None),
    ("telekommunikacii", "telecom", None),
    ("poisk_i_naim_personala", "recruiting", None),
    # --- Коммерческие ----------------------------------------------------------------
    ("komissiya_agregatoru", "aggregator_commission", None),
    ("targetirovannaya_reklama", "targeted_ads", None),
    ("bannernaya_reklama", "outdoor_ads",
     "Алиас: в эталоне строка называется «Наружная реклама»"),
    ("reklama_na_yandeks_kartah", "yandex_maps_ads", None),
    ("kontekstnaya_reklama", "context_ads", None),
    ("pechatnye_materialy", "printed_materials", "Строка помечена как неиспользуемая"),
    ("listovki", "flyers", None),
    ("seo_optimizaciya", "seo", None),
    ("prochie_marketingovye_uslugi", "other_marketing", None),
    ("sait_i_prilozhenie", "website_app", None),
    # --- Ниже EBITDA -----------------------------------------------------------------
    ("prochie_postupleniya", "other_income", None),
    ("prochie_postupl_ot_fin_operacii", "other_income", "Проценты на остаток по счёту"),
    ("korretirovki_kassy", "other_income", "Излишек кассы — доход месяца"),
    ("prochie_rashody", "other_expenses", None),
    ("korretirovki_kassy_2", "other_expenses", "Недостача кассы — расход месяца"),
    ("overdraft", "overdraft_fee", "Перечисление процентов по овердрафту"),
    ("fines_penalties", "fines_penalties",
     "Заводится этой же миграцией: в каталоге статьи не было"),
]

# Статьи, чей факт принадлежит другому слою либо не является доходом/расходом вовсе.
OUT_OF_PNL: list[tuple[str, str, str]] = [
    # (code, owner_stream, note)
    # --- Выручка и товар приходят из iiko -------------------------------------------
    ("postuplenie_deneg_s_torg_tochek", "iiko",
     "Инкассация выручки. Сама выручка — из OLAP, иначе задвоится"),
    ("revenue_acquiring_tbank", "iiko", "Зачисление эквайринга: выручка приходит из OLAP"),
    ("revenue_acquiring_sber", "iiko", "То же, устаревшая статья"),
    ("revenue_cash", "iiko", "То же, устаревшая статья"),
    ("payment_to_supplier", "iiko",
     "Оплата товара поставщику. В ОПиУ товар приходит фудкостом по себестоимости продаж, "
     "а не закупкой: иначе месяц закупки разъедется с месяцем продажи"),
    ("supplier_staff_payment", "supplier", "Строчная оплата поставщику, устаревшая статья"),
    ("advance_to_supplier", "supplier", "Аванс — это дебиторка, а не расход"),
    ("vozvrat_pereplaty_ot_postavschikov", "supplier", "Возврат аванса, не доход"),
    ("kurerskaya_sluzhba", "iiko",
     "Выдачи курьерам. Строка «Траты на курьерскую службу» берётся из пресета iiko целиком"),
    ("kurerskaya_sluzhba_2", "deposits", "Депозиты курьеров: приход, не доход"),
    # --- Зарплатный контур ------------------------------------------------------------
    ("zarplata_proizvodstvennogo_personala", "payroll", "Начисление берётся из ведомости"),
    ("zarplata_administrativnogo_personala", "payroll",
     "Начисление берётся из ведомости. Сюда же платятся оклад старшего курьера, уборщиц и "
     "посудомоек, и зарплата собственника — все они начислены в админ-ведомости"),
    ("payroll_payout", "payroll", "Устаревшая статья выплаты ЗП"),
    ("employee_advance", "payroll", "Аванс сотруднику — не расход месяца"),
    ("vydacha_zaymov_sotrudnikam", "payroll", "Заём сотруднику — дебиторка"),
    ("vydacha_depozita_sotrudniku", "deposits", "Депозит — не расход"),
    ("vozvrat_depozita_kurera", "deposits", "Возврат депозита — не расход"),
    # --- Налоги -----------------------------------------------------------------------
    ("tax_payment", "taxes",
     "Факт уплаты. Строка «Налоги» берёт начисление модуля «Налоги» (решение владельца "
     "03.08.2026). Налоговые пени сидят внутри этой же статьи"),
    # --- Переводы и резервы -----------------------------------------------------------
    ("internal_transfer", "transfer", None),
    ("postuplenie_perevod_mezhdu_schetami", "transfer",
     "Деактивирована, но история есть: июль 2026 — 857 860,72 ₽"),
    ("vybytie_perevod_mezhdu_schetami", "transfer",
     "Деактивирована, но история есть: июль 2026 — 799 360,72 ₽"),
    ("rezervy_postuplenie", "transfer", "Резервирование денег в Сейфе"),
    ("rezervy_vyvod", "transfer", "Снятие резерва"),
    ("pokupka_nalichnosti", "transfer", "Обмен безнала на наличные"),
    # --- Финансовая деятельность ------------------------------------------------------
    ("poluchenie_kreditov_i_zaimov", "financing", None),
    ("vozvrat_kreditov_i_zaimov", "financing", None),
    ("vydacha_kreditov_i_zaimov", "financing", None),
    ("oplaty_po_kreditam_i_zaimam", "financing",
     "Тело и проценты одной статьёй. Строка «Проценты по кредитам» заполняется вручную, "
     "пока учёта кредитов в приложении нет: взять статью целиком значило бы записать в "
     "расход возврат тела"),
    ("loan_principal_payment", "financing", "Устаревшая статья"),
    ("poluchenie_overdrafta", "financing", None),
    ("pogashenie_overdrafta", "financing", "Тело овердрафта; проценты — статья «Овердрафт»"),
    ("overdraft_fee", "financing", "Деактивированный плейсхолдер: проценты идут «Овердрафтом»"),
    ("postuplenie_deneg_ot_sobstvennikov", "financing", "Вклад собственника в капитал"),
    ("vozvrat_deneg_sobstvennikam", "financing", "Возврат вклада"),
    ("dividendy", "financing", "Распределение прибыли, а не расход"),
    # --- Инвестиционная деятельность --------------------------------------------------
    ("pokupka_os", "investing", "Стоимость объекта приходит в ОПиУ амортизацией"),
    ("remont_os", "investing", "Капитальный ремонт капитализируется в стоимость ОС"),
    ("modernizaciya_os", "investing", "То же"),
    ("prodazha_os", "investing",
     "Выручка от продажи ОС. Убыток от выбытия считает модуль «Учёт ОС» своей строкой"),
    # --- Приёмник неразмеченного ------------------------------------------------------
    ("unknown", "none",
     "Проводка без классификации. Проектор считает её «не разнесено» — эта цифра обязана "
     "быть нулём, она и есть сигнал о потере"),
]

# Механизмы выдачи, чей факт уже посчитан другим слоем.
# (source_kind, статья | None, owner_stream, note)
#
# ПАРА, А НЕ ОДИН МЕХАНИЗМ. Выплата из Сейфа и целевая выплата кассы — универсальные двери:
# через них идут и зарплата, и наличные траты администраторов. Исключить механизм целиком
# значило бы выбросить из отчёта «Содержание торговых точек» (июль 2026 — 40 037,52 ₽,
# 34 проводки) и «Расходы на питание персонала» (23 748,19 ₽, 22 проводки). Поэтому статьи
# этих механизмов исключаются точечно, а не оптом.
CASH_ORIGINS: list[tuple[str, str | None, str, str | None]] = [
    ("payroll_payout", None, "payroll", "Выплата ведомости"),
    ("payroll_bank_to_safe", None, "transfer", "Перевод под выплату ЗП"),
    ("salary_advance", None, "payroll", "Аванс сотруднику"),
    ("salary_advance_return", None, "payroll", None),
    ("employee_payout", None, "payroll", None),
    ("employee_payout_void", None, "payroll", None),
    ("production_deposit_payout", None, "deposits", None),
    ("courier_deposit_topup", None, "deposits", None),
    ("courier_deposit_return", None, "deposits", None),
    ("tax_notice", None, "taxes", "Уплата по налоговому уведомлению"),
    ("prepayment", None, "supplier", "Аванс поставщику"),
    ("supplier_prepayment", None, "supplier", None),
    ("legacy_prepayment", None, "supplier", None),
    ("internal_transfer_manual", None, "transfer", None),
    ("manual_transfer", None, "transfer", None),
]


def upgrade() -> None:
    op.create_table(
        "pnl_article_rule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dds_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "line_code",
            sa.String(64),
            sa.ForeignKey("pnl_line.code", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("in_pnl", sa.Boolean, nullable=False),
        sa.Column("owner_stream", sa.String(24), nullable=True),
        sa.Column("sign", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column(
            "applies_to", sa.String(16), nullable=False, server_default=sa.text("'both'")
        ),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("sign in (-1, 1)", name="ck_pnl_article_rule_sign"),
        sa.CheckConstraint(
            "applies_to in ('both', 'cash_only', 'accrual_only')",
            name="ck_pnl_article_rule_applies_to",
        ),
        sa.CheckConstraint(
            "(in_pnl = false and line_code is null) or (in_pnl = true and line_code is not null)",
            name="ck_pnl_article_rule_line_presence",
        ),
    )
    op.create_index(
        "uq_pnl_article_rule_active",
        "pnl_article_rule",
        ["article_id", "applies_to"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "pnl_source_rule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "line_code",
            sa.String(64),
            sa.ForeignKey("pnl_line.code", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stream", sa.String(24), nullable=False),
        sa.Column(
            "component", sa.String(32), nullable=False, server_default=sa.text("'main'")
        ),
        sa.Column("selector", postgresql.JSONB, nullable=True),
        sa.Column("sign", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column(
            "sign_policy", sa.String(16), nullable=False, server_default=sa.text("'as_is'")
        ),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default=sa.text("100")),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "stream in ('payroll', 'iiko', 'fixed_assets', 'taxes', 'inventory', "
            "'acquiring', 'manual')",
            name="ck_pnl_source_rule_stream",
        ),
        sa.CheckConstraint(
            "sign_policy in ('as_is', 'invert', 'expense_positive')",
            name="ck_pnl_source_rule_sign_policy",
        ),
        sa.CheckConstraint("sign in (-1, 1)", name="ck_pnl_source_rule_sign"),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_pnl_source_rule_active ON pnl_source_rule (
            line_code, stream, component, md5(coalesce(selector::text, ''))
        ) WHERE is_active
        """
    )

    op.create_table(
        "pnl_cash_origin",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column(
            "article_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dds_articles.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("owner_stream", sa.String(24), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_pnl_cash_origin_kind_article ON pnl_cash_origin (
            source_kind, coalesce(article_id::text, '*')
        )
        """
    )

    op.create_table(
        "pnl_manual_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date, nullable=False),
        sa.Column(
            "line_code",
            sa.String(64),
            sa.ForeignKey("pnl_line.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "component", sa.String(32), nullable=False, server_default=sa.text("'main'")
        ),
        sa.Column(
            "direction", sa.String(16), nullable=False, server_default=sa.text("'total'")
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("sign", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("sign in (-1, 1)", name="ck_pnl_manual_entry_sign"),
    )
    op.create_index(
        "uq_pnl_manual_entry_slot",
        "pnl_manual_entry",
        ["period_month", "line_code", "component", "direction"],
        unique=True,
    )

    connection = op.get_bind()

    # Статьи «Штрафы и пени» в каталоге не было, а строка эталона есть. Заводим её здесь,
    # иначе строка осталась бы пустой навсегда. Налоговые пени сюда не попадают — они
    # внутри статьи «Налоги» и учитываются строкой «Налоги».
    connection.execute(
        sa.text(
            """
            INSERT INTO dds_articles (id, code, name, movement_type, activity_type, description)
            VALUES (gen_random_uuid(), 'fines_penalties', 'Штрафы и пени', 'outflow',
                    'operating', 'Штрафы и пени, наложенные на ИП. Налоговые пени сюда не '
                    'относятся — они внутри статьи «Налоги»')
            ON CONFLICT (code) DO NOTHING
            """
        )
    )

    for code, line_code, note in IN_PNL:
        connection.execute(
            sa.text(
                """
                INSERT INTO pnl_article_rule (id, article_id, line_code, in_pnl, note)
                SELECT gen_random_uuid(), a.id, :line_code, true, :note
                FROM dds_articles a WHERE a.code = :code
                """
            ),
            {"code": code, "line_code": line_code, "note": note},
        )

    for code, owner_stream, note in OUT_OF_PNL:
        connection.execute(
            sa.text(
                """
                INSERT INTO pnl_article_rule (id, article_id, line_code, in_pnl,
                                              owner_stream, note)
                SELECT gen_random_uuid(), a.id, NULL, false, :owner_stream, :note
                FROM dds_articles a WHERE a.code = :code
                """
            ),
            {"code": code, "owner_stream": owner_stream, "note": note},
        )

    for source_kind, article_code, owner_stream, note in CASH_ORIGINS:
        connection.execute(
            sa.text(
                """
                INSERT INTO pnl_cash_origin (id, source_kind, article_id, owner_stream, note)
                VALUES (
                    gen_random_uuid(), :source_kind,
                    (SELECT id FROM dds_articles WHERE code = :article_code),
                    :owner_stream, :note
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "source_kind": source_kind,
                "article_code": article_code,
                "owner_stream": owner_stream,
                "note": note,
            },
        )


def downgrade() -> None:
    op.drop_index("uq_pnl_manual_entry_slot", table_name="pnl_manual_entry")
    op.drop_table("pnl_manual_entry")
    op.execute("DROP INDEX IF EXISTS uq_pnl_cash_origin_kind_article")
    op.drop_table("pnl_cash_origin")
    op.execute("DROP INDEX IF EXISTS uq_pnl_source_rule_active")
    op.drop_table("pnl_source_rule")
    op.drop_index("uq_pnl_article_rule_active", table_name="pnl_article_rule")
    op.drop_table("pnl_article_rule")
