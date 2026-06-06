from __future__ import annotations

from collections.abc import Iterable

PermissionCatalogItem = tuple[str, str, str]

ACCESS_ROLE_CODES: tuple[str, ...] = ("owner", "manager", "office_manager", "cashier")
EDITABLE_ROLE_CODES = frozenset({"manager", "office_manager", "cashier"})
FULL_ACCESS_ROLE_CODES = frozenset({"owner", "admin"})

MODULE_ORDER: tuple[str, ...] = (
    "Штат",
    "Курьеры",
    "Зарплата",
    "Ревизии",
    "Исходные данные",
    "Финансы",
    "Учёт",
    "Настройки и доступы",
)

VISIBLE_PERMISSION_CATALOG: tuple[PermissionCatalogItem, ...] = (
    ("staff.administration.read", "Штат", "Смотреть администрацию"),
    ("staff.administration.create", "Штат", "Создавать сотрудников администрации"),
    ("staff.administration.edit", "Штат", "Редактировать администрацию"),
    ("staff.administration.dismiss", "Штат", "Увольнять администрацию"),
    ("staff.administration.reinstate", "Штат", "Восстанавливать администрацию"),
    (
        "staff.administration.history.read",
        "Штат",
        "Смотреть историю изменений администрации",
    ),
    ("staff.cooks.read", "Штат", "Смотреть поваров"),
    ("staff.cooks.create", "Штат", "Создавать поваров"),
    ("staff.cooks.edit", "Штат", "Редактировать поваров"),
    ("staff.cooks.dismiss", "Штат", "Увольнять поваров"),
    ("staff.cooks.reinstate", "Штат", "Восстанавливать поваров"),
    ("staff.cooks.history.read", "Штат", "Смотреть историю изменений поваров"),
    ("staff.cashiers.read", "Штат", "Смотреть кассиров"),
    ("staff.cashiers.create", "Штат", "Создавать кассиров"),
    ("staff.cashiers.edit", "Штат", "Редактировать кассиров"),
    ("staff.cashiers.dismiss", "Штат", "Увольнять кассиров"),
    ("staff.cashiers.reinstate", "Штат", "Восстанавливать кассиров"),
    ("staff.cashiers.history.read", "Штат", "Смотреть историю изменений кассиров"),
    ("staff.auxiliary.read", "Штат", "Смотреть вспомогательный персонал"),
    ("staff.auxiliary.edit", "Штат", "Редактировать вспомогательный персонал"),
    ("staff.auxiliary.dismiss", "Штат", "Увольнять вспомогательный персонал"),
    ("staff.auxiliary.reinstate", "Штат", "Восстанавливать вспомогательный персонал"),
    (
        "staff.auxiliary.history.read",
        "Штат",
        "Смотреть историю изменений вспомогательного персонала",
    ),
    ("staff.couriers.read", "Штат", "Смотреть курьеров в Штате"),
    ("staff.couriers.create", "Штат", "Создавать карточки курьеров"),
    ("staff.couriers.edit", "Штат", "Редактировать карточки курьеров"),
    ("staff.couriers.dismiss", "Штат", "Увольнять курьеров"),
    ("staff.couriers.reinstate", "Штат", "Восстанавливать курьеров"),
    (
        "staff.couriers.history.read",
        "Штат",
        "Смотреть историю изменений курьеров в Штате",
    ),
    (
        "staff.cooks.assign_roles_categories",
        "Штат",
        "Назначать роли и категории поваров",
    ),
    (
        "staff.cashiers.assign_roles_categories",
        "Штат",
        "Назначать роли и категории кассиров",
    ),
    ("couriers.list.read", "Курьеры", "Смотреть список курьеров"),
    ("couriers.statistics.read", "Курьеры", "Смотреть статистику курьеров"),
    ("couriers.schedule.read", "Курьеры", "Смотреть график курьеров"),
    ("couriers.schedule.edit", "Курьеры", "Редактировать график курьеров"),
    ("couriers.shifts.sync", "Курьеры", "Загружать смены курьеров из внешней системы"),
    ("couriers.evaluations.read", "Курьеры", "Смотреть оценки курьеров"),
    ("couriers.evaluations.edit", "Курьеры", "Добавлять/редактировать оценки курьеров"),
    ("couriers.deposits.read", "Курьеры", "Смотреть депозиты курьеров"),
    ("couriers.deposits.edit", "Курьеры", "Редактировать депозиты курьеров"),
    ("couriers.deposits.configure", "Курьеры", "Настраивать депозиты курьеров"),
    ("payroll.runs.read", "Зарплата", "Смотреть расчёты зарплаты"),
    ("payroll.runs.start", "Зарплата", "Запускать расчёт зарплаты"),
    ("payroll.runs.recalculate", "Зарплата", "Пересчитывать зарплату"),
    ("payroll.runs.finalize", "Зарплата", "Финализировать расчёт зарплаты"),
    ("payroll.runs.reopen", "Зарплата", "Возвращать расчёт зарплаты в работу"),
    ("payroll.accruals.read", "Зарплата", "Смотреть начисления сотрудников"),
    ("payroll.personal_reports.read", "Зарплата", "Смотреть персональные отчёты сотрудников"),
    ("payroll.adjustments.edit", "Зарплата", "Редактировать корректировки зарплаты"),
    ("payroll.bonuses.add", "Зарплата", "Начислять премии сотрудникам"),
    ("payroll.penalties.add", "Зарплата", "Начислять штрафы сотрудникам"),
    ("payroll.vacations.read", "Зарплата", "Смотреть отпуска сотрудников"),
    ("payroll.vacations.edit", "Зарплата", "Редактировать отпуска сотрудников"),
    ("payroll.fund.read", "Зарплата", "Смотреть фонд накопления"),
    ("payroll.fund.edit", "Зарплата", "Редактировать фонд накопления"),
    (
        "payroll.production_deposits.read",
        "Зарплата",
        "Смотреть депозиты производственного персонала",
    ),
    (
        "payroll.production_deposits.edit",
        "Зарплата",
        "Редактировать депозиты производственного персонала",
    ),
    ("revisions.read", "Ревизии", "Смотреть ревизии"),
    ("revisions.import", "Ревизии", "Импортировать ревизию"),
    ("revisions.create", "Ревизии", "Создавать ревизию вручную"),
    ("revisions.drafts.edit", "Ревизии", "Редактировать черновик ревизии"),
    ("revisions.items.exclude", "Ревизии", "Исключать позиции из ревизии"),
    (
        "revisions.employees.exclude",
        "Ревизии",
        "Исключать сотрудников из распределения",
    ),
    ("revisions.finalize", "Ревизии", "Финализировать ревизию"),
    ("revisions.cancel", "Ревизии", "Отменять ревизию"),
    ("revisions.restore_draft", "Ревизии", "Возвращать ревизию в черновик"),
    (
        "revisions.deferrals.read",
        "Ревизии",
        "Смотреть распределение списаний ревизий",
    ),
    (
        "revisions.deferrals.manage",
        "Ревизии",
        "Распределять списание по нескольким зарплатам",
    ),
    (
        "revisions.rules.manage",
        "Ревизии",
        "Управлять правилами распределения ревизий",
    ),
    ("source.rates.read", "Исходные данные", "Смотреть ставки"),
    ("source.rates.edit", "Исходные данные", "Редактировать ставки"),
    ("source.revenue_percent.read", "Исходные данные", "Смотреть проценты от выручки"),
    (
        "source.revenue_percent.edit",
        "Исходные данные",
        "Редактировать проценты от выручки",
    ),
    ("source.deductions.read", "Исходные данные", "Смотреть удержания"),
    ("source.deductions.edit", "Исходные данные", "Редактировать удержания"),
    (
        "source.dismissal_reasons.read",
        "Исходные данные",
        "Смотреть причины увольнения",
    ),
    (
        "source.dismissal_reasons.edit",
        "Исходные данные",
        "Редактировать причины увольнения",
    ),
    ("source.allowances.read", "Исходные данные", "Смотреть надбавки"),
    ("source.allowances.edit", "Исходные данные", "Редактировать надбавки"),
    (
        "source.fund_settings.read",
        "Исходные данные",
        "Смотреть настройки накопительного фонда",
    ),
    (
        "source.fund_settings.edit",
        "Исходные данные",
        "Редактировать настройки накопительного фонда",
    ),
    ("source.deposit_settings.read", "Исходные данные", "Смотреть настройки депозитов"),
    (
        "source.deposit_settings.edit",
        "Исходные данные",
        "Редактировать настройки депозитов",
    ),
    ("source.revision_settings.read", "Исходные данные", "Смотреть настройки ревизий"),
    (
        "source.revision_settings.edit",
        "Исходные данные",
        "Редактировать настройки ревизий",
    ),
    ("source.schedule.read", "Исходные данные", "Смотреть график смен"),
    ("source.schedule.edit", "Исходные данные", "Редактировать график смен"),
    ("source.shift_ledger.read", "Исходные данные", "Смотреть учёт смен и выручки"),
    ("source.shift_ledger.input", "Исходные данные", "Вводить учёт смен и выручки"),
    ("source.shift_ledger.correct", "Исходные данные", "Исправлять учёт смен и выручки"),
    ("source.revenue.read", "Исходные данные", "Смотреть исходные данные выручки"),
    ("source.revenue.edit", "Исходные данные", "Редактировать исходные данные выручки"),
    ("source.import.run", "Исходные данные", "Загружать данные из внешних систем"),
    (
        "source.import_errors.review",
        "Исходные данные",
        "Разбирать ошибки загрузки исходных данных",
    ),
    ("finance.cashflow.read", "Финансы", "Смотреть движение денег"),
    ("finance.cashflow.edit", "Финансы", "Редактировать движение денег"),
    ("finance.cashflow.classify", "Финансы", "Классифицировать движение денег"),
    (
        "finance.classification_rules.manage",
        "Финансы",
        "Управлять правилами классификации денег",
    ),
    (
        "finance.cashflow.integrations.manage",
        "Финансы",
        "Настраивать подключения и API движения денег",
    ),
    ("finance.balance.read", "Финансы", "Смотреть баланс"),
    ("finance.balance.edit", "Финансы", "Редактировать баланс"),
    ("finance.wallets.read", "Финансы", "Смотреть кошельки и остатки"),
    ("finance.wallets.edit", "Финансы", "Редактировать кошельки и остатки"),
    ("finance.store_cash.read", "Финансы", "Смотреть торговую кассу"),
    ("finance.store_cash.enter", "Финансы", "Вносить операции торговой кассы"),
    ("finance.payment_calendar.read", "Финансы", "Смотреть платёжный календарь"),
    ("finance.payment_calendar.edit", "Финансы", "Редактировать платёжный календарь"),
    ("finance.counterparties.read", "Финансы", "Смотреть контрагентов"),
    ("finance.counterparties.edit", "Финансы", "Редактировать контрагентов"),
    (
        "finance.owner_review.prepare",
        "Финансы",
        "Разбирать операции для проверки собственником",
    ),
    ("accounting.fixed_assets.read", "Учёт", "Смотреть основные средства"),
    ("accounting.fixed_assets.edit", "Учёт", "Редактировать основные средства"),
    ("accounting.suppliers.read", "Учёт", "Смотреть взаиморасчёты с поставщиками"),
    ("accounting.suppliers.edit", "Учёт", "Редактировать взаиморасчёты с поставщиками"),
    ("accounting.periods.close", "Учёт", "Закрывать учётный период"),
    ("settings.general.read", "Настройки и доступы", "Смотреть настройки"),
    ("settings.general.edit", "Настройки и доступы", "Редактировать настройки"),
    ("settings.integrations.configure", "Настройки и доступы", "Настраивать обмен данными"),
    ("settings.users.read", "Настройки и доступы", "Смотреть пользователей"),
    ("settings.users.create", "Настройки и доступы", "Создавать пользователей"),
    ("settings.users.edit", "Настройки и доступы", "Редактировать пользователей"),
    ("settings.access.assign", "Настройки и доступы", "Назначать доступы пользователям"),
    ("settings.roles.edit", "Настройки и доступы", "Редактировать права должностей"),
    (
        "settings.access_audit.read",
        "Настройки и доступы",
        "Смотреть журнал изменения доступов",
    ),
    ("settings.action_audit.read", "Настройки и доступы", "Смотреть журнал действий пользователей"),
)

LEGACY_PERMISSION_CATALOG: tuple[PermissionCatalogItem, ...] = (
    ("dds.read", "Финансы", "Смотреть движение денег"),
    ("dds.classify", "Финансы", "Классифицировать движение денег"),
    ("dds.write", "Финансы", "Редактировать движение денег"),
    ("dds.rules.write", "Финансы", "Управлять правилами классификации денег"),
    ("payroll.read", "Зарплата", "Смотреть персональные отчёты сотрудников"),
    ("payroll.runs.write", "Зарплата", "Запускать расчёт зарплаты"),
    ("payroll.source_data.read", "Исходные данные", "Смотреть исходные данные зарплаты"),
    ("payroll.source_data.edit", "Исходные данные", "Редактировать исходные данные зарплаты"),
    ("payroll.rules.configure", "Исходные данные", "Настраивать правила зарплаты"),
    ("payroll.revisions.read", "Ревизии", "Смотреть распределение списаний ревизий"),
    ("payroll.revision_penalties.add", "Ревизии", "Распределять списание по нескольким зарплатам"),
    ("payroll.adjustments.write", "Зарплата", "Редактировать корректировки зарплаты"),
    ("schedule.read", "Исходные данные", "Смотреть график смен"),
    ("schedule.write", "Исходные данные", "Редактировать график смен"),
    ("shifts.read", "Исходные данные", "Смотреть учёт смен и выручки"),
    ("shifts.write", "Исходные данные", "Вводить учёт смен и выручки"),
    ("inventory.read", "Учёт", "Смотреть инвентаризации"),
    ("inventory.write", "Учёт", "Редактировать справочник инвентаризации"),
    ("inventory.audits.write", "Учёт", "Проводить инвентаризацию"),
    ("staff.read", "Штат", "Смотреть администрацию"),
    ("staff.write", "Штат", "Редактировать администрацию"),
    ("staff.dismiss", "Штат", "Увольнять администрацию"),
    ("staff.reinstate", "Штат", "Восстанавливать администрацию"),
    ("couriers.read", "Курьеры", "Смотреть курьерский список"),
    ("couriers.metrics.read", "Курьеры", "Смотреть показатели курьеров"),
    ("couriers.schedule.write", "Курьеры", "Редактировать график курьеров"),
    ("couriers.assess.write", "Курьеры", "Редактировать оценки курьеров"),
    ("couriers.deposits.withhold", "Курьеры", "Вносить удержания депозитов курьеров"),
    ("couriers.deposits.write", "Курьеры", "Редактировать депозиты курьеров"),
    ("couriers.deposits.return", "Курьеры", "Возвращать депозиты курьеров"),
    ("deposits.read", "Зарплата", "Смотреть депозиты производственного персонала"),
    ("deposits.write", "Зарплата", "Редактировать депозиты производственного персонала"),
    ("vacations.read", "Зарплата", "Смотреть персональные отчёты сотрудников"),
    ("vacations.write", "Зарплата", "Редактировать отпуска сотрудников"),
    ("accumulation_fund.read", "Зарплата", "Смотреть фонд накопления"),
    ("accumulation_fund.write", "Зарплата", "Редактировать фонд накопления"),
    ("settings.read", "Настройки и доступы", "Смотреть настройки"),
    ("settings.write", "Настройки и доступы", "Редактировать настройки"),
    ("access_control.users.write", "Настройки и доступы", "Назначать доступы пользователям"),
    ("access_control.roles.write", "Настройки и доступы", "Редактировать права должностей"),
)

PERMISSION_CATALOG: tuple[PermissionCatalogItem, ...] = (
    *VISIBLE_PERMISSION_CATALOG,
    *LEGACY_PERMISSION_CATALOG,
)

VISIBLE_PERMISSION_CODES = frozenset(
    code for code, _module, _description in VISIBLE_PERMISSION_CATALOG
)
LEGACY_PERMISSION_CODES = frozenset(
    code for code, _module, _description in LEGACY_PERMISSION_CATALOG
)
ALL_PERMISSION_CODES = frozenset(code for code, _module, _description in PERMISSION_CATALOG)
PERMISSION_CODE_ORDER = {
    code: index for index, (code, _module, _description) in enumerate(PERMISSION_CATALOG)
}
MODULE_ORDER_INDEX = {module: index for index, module in enumerate(MODULE_ORDER)}

SOURCE_DATA_TAB_READ_PERMISSION_CODES = frozenset(
    {
        "source.rates.read",
        "source.revenue_percent.read",
        "source.deductions.read",
        "source.dismissal_reasons.read",
        "source.allowances.read",
        "source.fund_settings.read",
        "source.deposit_settings.read",
        "source.revision_settings.read",
    }
)
SOURCE_DATA_TAB_EDIT_PERMISSION_CODES = frozenset(
    {
        "source.rates.edit",
        "source.revenue_percent.edit",
        "source.deductions.edit",
        "source.dismissal_reasons.edit",
        "source.allowances.edit",
        "source.fund_settings.edit",
        "source.deposit_settings.edit",
        "source.revision_settings.edit",
    }
)

LEGACY_PERMISSION_ALIASES: dict[str, frozenset[str]] = {
    "dds.read": frozenset(
        {
            "finance.cashflow.read",
            "finance.wallets.read",
            "finance.counterparties.read",
            "finance.owner_review.prepare",
        }
    ),
    "dds.classify": frozenset({"finance.cashflow.classify"}),
    "dds.write": frozenset(
        {
            "finance.cashflow.edit",
            "finance.counterparties.edit",
            "finance.owner_review.prepare",
            "finance.cashflow.integrations.manage",
        }
    ),
    "dds.rules.write": frozenset({"finance.classification_rules.manage"}),
    "payroll.read": frozenset(
        {
            "payroll.runs.read",
            "payroll.accruals.read",
            "payroll.personal_reports.read",
            "payroll.production_deposits.read",
            *SOURCE_DATA_TAB_READ_PERMISSION_CODES,
        }
    ),
    "payroll.source_data.read": SOURCE_DATA_TAB_READ_PERMISSION_CODES,
    "payroll.source_data.edit": SOURCE_DATA_TAB_EDIT_PERMISSION_CODES,
    "payroll.rules.configure": SOURCE_DATA_TAB_EDIT_PERMISSION_CODES,
    "payroll.revisions.read": frozenset({"revisions.deferrals.read"}),
    "payroll.revision_penalties.add": frozenset({"revisions.deferrals.manage"}),
    "payroll.runs.write": frozenset({"payroll.runs.start", "payroll.runs.recalculate"}),
    "payroll.runs.finalize": frozenset({"payroll.runs.finalize"}),
    "payroll.adjustments.write": frozenset(
        {
            "payroll.adjustments.edit",
            "payroll.bonuses.add",
            "payroll.penalties.add",
            "source.deductions.edit",
        }
    ),
    "schedule.read": frozenset({"source.schedule.read"}),
    "schedule.write": frozenset({"source.schedule.edit"}),
    "shifts.read": frozenset({"source.shift_ledger.read"}),
    "shifts.write": frozenset({"source.shift_ledger.input", "source.shift_ledger.correct"}),
    "inventory.read": frozenset({"revisions.read"}),
    "inventory.write": frozenset(
        {
            "revisions.create",
            "revisions.drafts.edit",
            "revisions.import",
        }
    ),
    "inventory.audits.write": frozenset(
        {
            "revisions.create",
            "revisions.drafts.edit",
            "revisions.import",
            "revisions.items.exclude",
            "revisions.employees.exclude",
            "revisions.finalize",
        }
    ),
    "staff.read": frozenset(
        {"staff.administration.read", "staff.administration.history.read"}
    ),
    "staff.write": frozenset({"staff.administration.edit"}),
    "staff.dismiss": frozenset({"staff.administration.dismiss"}),
    "staff.reinstate": frozenset({"staff.administration.reinstate"}),
    "couriers.read": frozenset(
        {
            "couriers.list.read",
            "couriers.statistics.read",
            "couriers.schedule.read",
            "couriers.evaluations.read",
            "couriers.deposits.read",
        }
    ),
    "couriers.schedule.write": frozenset({"couriers.schedule.edit", "couriers.shifts.sync"}),
    "couriers.assess.write": frozenset({"couriers.evaluations.edit"}),
    "couriers.deposits.write": frozenset(
        {"couriers.deposits.edit", "couriers.deposits.configure"}
    ),
    "couriers.metrics.read": frozenset({"couriers.statistics.read"}),
    "couriers.deposits.withhold": frozenset({"couriers.deposits.edit"}),
    "couriers.deposits.return": frozenset({"couriers.deposits.edit"}),
    "deposits.read": frozenset({"payroll.production_deposits.read"}),
    "deposits.write": frozenset({"payroll.production_deposits.edit"}),
    "vacations.read": frozenset({"payroll.vacations.read"}),
    "vacations.write": frozenset({"payroll.vacations.edit"}),
    "accumulation_fund.read": frozenset({"payroll.fund.read"}),
    "accumulation_fund.write": frozenset({"payroll.fund.edit"}),
    "settings.read": frozenset({"settings.general.read"}),
    "settings.write": frozenset({"settings.general.edit"}),
    "access_control.users.write": frozenset(
        {
            "settings.users.read",
            "settings.users.create",
            "settings.users.edit",
            "settings.access.assign",
        }
    ),
    "access_control.roles.write": frozenset(
        {"settings.roles.edit", "settings.access_audit.read"}
    ),
}

MANAGER_DEFAULT_PERMISSIONS = frozenset(
    {
        "staff.cooks.read",
        "staff.cooks.create",
        "staff.cooks.edit",
        "staff.cooks.dismiss",
        "staff.cooks.reinstate",
        "staff.cooks.history.read",
        "staff.cashiers.read",
        "staff.cashiers.create",
        "staff.cashiers.edit",
        "staff.cashiers.dismiss",
        "staff.cashiers.reinstate",
        "staff.cashiers.history.read",
        "staff.auxiliary.read",
        "staff.auxiliary.edit",
        "staff.auxiliary.dismiss",
        "staff.auxiliary.reinstate",
        "staff.auxiliary.history.read",
        "staff.couriers.read",
        "staff.couriers.create",
        "staff.couriers.edit",
        "staff.couriers.dismiss",
        "staff.couriers.reinstate",
        "staff.couriers.history.read",
        "staff.cooks.assign_roles_categories",
        "staff.cashiers.assign_roles_categories",
        "couriers.list.read",
        "couriers.schedule.read",
        "couriers.schedule.edit",
        "couriers.shifts.sync",
        "couriers.statistics.read",
        "couriers.evaluations.read",
        "couriers.evaluations.edit",
        "couriers.deposits.read",
        "couriers.deposits.edit",
        "couriers.deposits.configure",
        "payroll.runs.read",
        "payroll.accruals.read",
        "payroll.adjustments.edit",
        "payroll.bonuses.add",
        "payroll.penalties.add",
        "payroll.vacations.read",
        "payroll.vacations.edit",
        "payroll.production_deposits.read",
        "payroll.production_deposits.edit",
        "source.rates.read",
        "source.revenue_percent.read",
        "source.deductions.read",
        "source.dismissal_reasons.read",
        "source.allowances.read",
        "source.fund_settings.read",
        "source.deposit_settings.read",
        "source.revision_settings.read",
        "source.schedule.read",
        "source.schedule.edit",
        "source.shift_ledger.read",
        "source.shift_ledger.input",
        "source.shift_ledger.correct",
        "source.revenue.read",
        "source.revenue.edit",
        "source.import.run",
        "source.import_errors.review",
        "finance.store_cash.read",
        "finance.store_cash.enter",
        "revisions.read",
        "revisions.import",
        "revisions.create",
        "revisions.drafts.edit",
        "revisions.items.exclude",
        "revisions.employees.exclude",
        "revisions.deferrals.read",
        "settings.general.read",
    }
)

OFFICE_MANAGER_DEFAULT_PERMISSIONS = frozenset(
    VISIBLE_PERMISSION_CODES
    - {
        "payroll.runs.reopen",
        "revisions.import",
        "revisions.create",
        "revisions.drafts.edit",
        "revisions.items.exclude",
        "revisions.employees.exclude",
        "revisions.finalize",
        "revisions.cancel",
        "revisions.restore_draft",
        "revisions.deferrals.manage",
        "revisions.rules.manage",
        "settings.users.read",
        "settings.users.create",
        "settings.users.edit",
        "settings.access.assign",
        "settings.roles.edit",
        "settings.access_audit.read",
    }
)

CASHIER_DEFAULT_PERMISSIONS = frozenset(
    {
        "couriers.list.read",
        "couriers.schedule.read",
        "couriers.statistics.read",
        "couriers.evaluations.read",
        "couriers.evaluations.edit",
        "couriers.deposits.read",
        "source.schedule.read",
        "source.shift_ledger.read",
        "source.shift_ledger.input",
        "finance.store_cash.read",
        "finance.store_cash.enter",
    }
)

DEFAULT_ROLE_PERMISSIONS = {
    "manager": MANAGER_DEFAULT_PERMISSIONS,
    "office_manager": OFFICE_MANAGER_DEFAULT_PERMISSIONS,
    "cashier": CASHIER_DEFAULT_PERMISSIONS,
}


def expand_permission_codes(codes: Iterable[str]) -> frozenset[str]:
    expanded = set(codes)
    for legacy_code, replacement_codes in LEGACY_PERMISSION_ALIASES.items():
        if legacy_code in expanded:
            expanded.update(replacement_codes)
        if expanded & replacement_codes:
            expanded.add(legacy_code)
    return frozenset(expanded)


def normalize_permission_codes_for_storage(codes: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for code in codes:
        if code in LEGACY_PERMISSION_ALIASES:
            normalized.update(LEGACY_PERMISSION_ALIASES[code])
        else:
            normalized.add(code)
    return frozenset(normalized)


def permission_is_granted(required_code: str, granted_codes: Iterable[str]) -> bool:
    return required_code in expand_permission_codes(granted_codes)


def _validate_catalog() -> None:
    duplicated_codes = {
        code
        for code in ALL_PERMISSION_CODES
        if sum(1 for item in PERMISSION_CATALOG if item[0] == code) > 1
    }
    if duplicated_codes:
        raise RuntimeError(f"Duplicated permission codes: {sorted(duplicated_codes)}")
    unknown_default_codes = (
        set().union(*DEFAULT_ROLE_PERMISSIONS.values()) - VISIBLE_PERMISSION_CODES
    )
    if unknown_default_codes:
        raise RuntimeError(f"Unknown default permission codes: {sorted(unknown_default_codes)}")
    unknown_alias_codes = (
        set().union(*LEGACY_PERMISSION_ALIASES.values()) - VISIBLE_PERMISSION_CODES
    )
    if unknown_alias_codes:
        raise RuntimeError(f"Unknown legacy alias permission codes: {sorted(unknown_alias_codes)}")


_validate_catalog()
