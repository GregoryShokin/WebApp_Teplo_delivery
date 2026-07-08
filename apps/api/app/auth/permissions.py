from __future__ import annotations

from collections.abc import Iterable

PermissionCatalogItem = tuple[str, str, str]

ACCESS_ROLE_CODES: tuple[str, ...] = (
    "owner",
    "manager",
    "office_manager",
    "cashier",
    "senior_courier",
    "kitchen",
)
EDITABLE_ROLE_CODES = frozenset(
    {"manager", "office_manager", "cashier", "senior_courier", "kitchen"}
)
FULL_ACCESS_ROLE_CODES = frozenset({"owner", "admin"})

MODULE_ORDER: tuple[str, ...] = (
    "Штат",
    "Курьеры",
    "Кухня",
    "Зарплата",
    "Ревизии",
    "Исходные данные",
    "Финансы",
    "Контрагенты",
    "Накладные",
    "Касса",
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
    ("staff.freelancer_pin.read", "Штат", "Смотреть ПИН внештатника"),
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
    ("staff.auxiliary.create", "Штат", "Создавать вспомогательный персонал"),
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
    ("couriers.senior.assign", "Курьеры", "Назначать старшего курьера"),
    ("couriers.shifts.sync", "Курьеры", "Загружать смены курьеров из внешней системы"),
    ("couriers.evaluations.read", "Курьеры", "Смотреть оценки курьеров"),
    ("couriers.evaluations.edit", "Курьеры", "Добавлять/редактировать оценки курьеров"),
    ("couriers.deposits.read", "Курьеры", "Смотреть депозиты курьеров"),
    ("couriers.deposits.top_up", "Курьеры", "Пополнять депозиты курьеров"),
    ("couriers.deposits.return", "Курьеры", "Возвращать депозиты курьеров"),
    ("couriers.deposits.forfeit", "Курьеры", "Списывать депозиты курьеров"),
    ("couriers.deposits.configure", "Курьеры", "Настраивать депозиты курьеров"),
    ("kds.queue.read", "Кухня", "Смотреть очередь упаковки (планшет)"),
    ("kds.queue.write", "Кухня", "Отмечать статусы упаковки и откаты"),
    ("kitchen.speed.read", "Кухня", "Смотреть дашборд скорости кухни"),
    ("payroll.runs.read", "Зарплата", "Смотреть расчёты зарплаты"),
    ("payroll.runs.start", "Зарплата", "Запускать расчёт зарплаты"),
    ("payroll.runs.recalculate", "Зарплата", "Пересчитывать зарплату"),
    ("payroll.runs.finalize", "Зарплата", "Финализировать расчёт зарплаты"),
    ("payroll.runs.reopen", "Зарплата", "Возвращать расчёт зарплаты в работу"),
    (
        "payroll.runs.admin.start",
        "Зарплата",
        "Создавать ведомости администрации (Администрация)",
    ),
    (
        "payroll.runs.admin.recalculate",
        "Зарплата",
        "Пересчитывать ведомости администрации",
    ),
    (
        "payroll.runs.mark_paid",
        "Зарплата",
        "Отмечать выплаты по ведомости",
    ),
    (
        "payroll.runs.bank_draft",
        "Зарплата",
        "Формировать банковские черновики выплат",
    ),
    ("payroll.accruals.read", "Зарплата", "Смотреть начисления сотрудников"),
    ("payroll.personal_reports.read", "Зарплата", "Смотреть персональные отчёты сотрудников"),
    ("payroll.adjustments.edit", "Зарплата", "Редактировать корректировки зарплаты"),
    ("payroll.bonuses.add", "Зарплата", "Начислять премии сотрудникам"),
    ("payroll.penalties.add", "Зарплата", "Начислять штрафы сотрудникам"),
    (
        "payroll.bonuses.admin.add",
        "Зарплата",
        "Начислять премии административному персоналу",
    ),
    (
        "payroll.penalties.admin.add",
        "Зарплата",
        "Начислять штрафы административному персоналу",
    ),
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
        "payroll.production_deposits.payout",
        "Зарплата",
        "Выдавать депозиты производственного персонала",
    ),
    (
        "payroll.production_deposits.write_off",
        "Зарплата",
        "Списывать депозиты производственного персонала",
    ),
    (
        "payroll.production_deposits.edit",
        "Зарплата",
        "Настраивать депозиты производственного персонала (исключения, начальный баланс)",
    ),
    ("payroll.advances.read", "Зарплата", "Смотреть авансы и займы сотрудников"),
    (
        "payroll.advances.production.issue",
        "Зарплата",
        "Выдавать авансы производственному персоналу",
    ),
    (
        "payroll.advances.admin.issue",
        "Зарплата",
        "Выдавать авансы административному персоналу",
    ),
    ("payroll.loans.issue", "Зарплата", "Выдавать займы (сверх заработанного)"),
    (
        "payroll.advances.backdate",
        "Зарплата",
        "Проводить авансы/займы задним числом (прошлой датой)",
    ),
    (
        "payroll.employee_payouts.create",
        "Зарплата",
        "Создавать выплаты сотрудникам (ЗП собственника «по востребованию», разовые)",
    ),
    ("revisions.read", "Ревизии", "Смотреть ревизии"),
    ("revisions.import", "Ревизии", "Импортировать ревизию"),
    ("revisions.create", "Ревизии", "Создавать ревизию вручную"),
    ("revisions.drafts.edit", "Ревизии", "Редактировать черновик ревизии"),
    ("revisions.items.exclude", "Ревизии", "Исключать позиции из ревизии"),
    (
        "revisions.items.adjust",
        "Ревизии",
        "Корректировать сумму недостачи по позиции",
    ),
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
    (
        "source.schedule.dishwashers.edit",
        "Исходные данные",
        "Редактировать график мойщиц",
    ),
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
    ("finance.safe.allocate", "Финансы", "Резервировать средства Сейфа"),
    ("finance.safe.confirm_paid", "Финансы", "Подтверждать оплаты с Сейфа"),
    (
        "finance.payout_channel.cash_tk",
        "Финансы",
        "Выдавать с торговой кассы Черникова",
    ),
    ("finance.payout_channel.safe", "Финансы", "Выдавать с Сейфа"),
    (
        "finance.payout_channel.bank_draft",
        "Финансы",
        "Формировать банковские черновики (Т-Банк)",
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
    ("counterparties.read", "Контрагенты", "Смотреть контрагентов и счета к оплате"),
    (
        "counterparties.operate",
        "Контрагенты",
        "Отправлять платежи в банк и сводить с выписками",
    ),
    (
        "counterparties.admin",
        "Контрагенты",
        "Редактировать реквизиты, реестр и справочники контрагентов",
    ),
    ("invoices.normal.create", "Накладные", "Создавать обычные накладные"),
    ("invoices.normal.edit", "Накладные", "Редактировать обычные накладные"),
    (
        "invoices.normal.pay",
        "Накладные",
        "Оплачивать обычные накладные (в т. ч. отправлять в банк)",
    ),
    (
        "invoices.barter.create",
        "Накладные",
        "Создавать бартерные накладные (займы и возвраты)",
    ),
    ("invoices.barter.edit", "Накладные", "Редактировать бартерные накладные"),
    ("invoices.barter.pay", "Накладные", "Оплачивать бартерные накладные"),
    ("kassa.shifts.read", "Касса", "Смотреть закрытие кассовых смен"),
    ("kassa.shifts.sync", "Касса", "Загружать кассовые смены из iiko"),
    ("kassa.shifts.post", "Касса", "Проводить наличный контур смены в ДДС"),
    ("kassa.cheques.read", "Касса", "Смотреть чеки (оплаты картой)"),
    ("kassa.cheques.create", "Касса", "Создавать чеки (оплаты картой)"),
    ("kassa.invoices.create", "Касса", "Создавать накладные из Кассы"),
    ("kassa.invoices.push", "Касса", "Отправлять накладные из Кассы в iiko"),
    ("kassa.adjustments.create", "Касса", "Создавать корректировки кассы"),
    ("kassa.penalty.waive", "Касса", "Отменять авто-штрафы кассирам за недостачу смены"),
    (
        "kassa.refs.read",
        "Касса",
        "Смотреть справочники Кассы (статьи ДДС, счета, контрагенты)",
    ),
    (
        "kassa.journal.read",
        "Касса",
        "Смотреть кассовый журнал (движения по ТК Черникова)",
    ),
    (
        "kassa.payouts.create",
        "Касса",
        "Выдавать наличные из кассы по разрешённым статьям",
    ),
    ("settings.general.read", "Настройки и доступы", "Смотреть настройки"),
    ("settings.general.edit", "Настройки и доступы", "Редактировать настройки"),
    ("settings.positions.read", "Настройки и доступы", "Смотреть реестр должностей"),
    ("settings.positions.edit", "Настройки и доступы", "Редактировать реестр должностей"),
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
    ("couriers.deposits.edit", "Курьеры", "Редактировать депозиты курьеров"),
    ("couriers.deposits.withhold", "Курьеры", "Вносить удержания депозитов курьеров"),
    ("couriers.deposits.write", "Курьеры", "Редактировать депозиты курьеров"),
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
    "payroll.runs.finalize": frozenset(
        {"payroll.runs.finalize", "payroll.runs.mark_paid", "payroll.runs.bank_draft"}
    ),
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
    "staff.read": frozenset({"staff.administration.read", "staff.administration.history.read"}),
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
    # Старое единое право правки курьерских депозитов → 3 гранулярных операции.
    "couriers.deposits.edit": frozenset(
        {
            "couriers.deposits.top_up",
            "couriers.deposits.return",
            "couriers.deposits.forfeit",
        }
    ),
    "couriers.deposits.write": frozenset(
        {
            "couriers.deposits.top_up",
            "couriers.deposits.return",
            "couriers.deposits.forfeit",
            "couriers.deposits.configure",
        }
    ),
    "couriers.metrics.read": frozenset({"couriers.statistics.read"}),
    "couriers.deposits.withhold": frozenset({"couriers.deposits.forfeit"}),
    "deposits.read": frozenset({"payroll.production_deposits.read"}),
    "deposits.write": frozenset(
        {
            "payroll.production_deposits.edit",
            "payroll.production_deposits.payout",
            "payroll.production_deposits.write_off",
        }
    ),
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
    "access_control.roles.write": frozenset({"settings.roles.edit", "settings.access_audit.read"}),
}

MANAGER_DEFAULT_PERMISSIONS = frozenset(
    {
        "staff.freelancer_pin.read",
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
        "staff.auxiliary.create",
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
        "couriers.senior.assign",
        "couriers.shifts.sync",
        "couriers.statistics.read",
        "couriers.evaluations.read",
        "couriers.evaluations.edit",
        "couriers.deposits.read",
        "couriers.deposits.top_up",
        "couriers.deposits.return",
        "couriers.deposits.forfeit",
        "couriers.deposits.configure",
        "kds.queue.read",
        "kds.queue.write",
        "kitchen.speed.read",
        "payroll.runs.read",
        "payroll.runs.mark_paid",
        "payroll.runs.bank_draft",
        "payroll.runs.admin.start",
        "payroll.runs.admin.recalculate",
        # Управляющий ведёт ЗП (выплаты сотрудникам): выдано миграцией 0153 —
        # держим дефолт кода в синхроне с сидом БД.
        "payroll.employee_payouts.create",
        "payroll.accruals.read",
        "payroll.adjustments.edit",
        "payroll.bonuses.add",
        "payroll.penalties.add",
        "payroll.bonuses.admin.add",
        "payroll.penalties.admin.add",
        "payroll.vacations.read",
        "payroll.vacations.edit",
        "payroll.production_deposits.read",
        "payroll.production_deposits.payout",
        "payroll.production_deposits.write_off",
        "payroll.production_deposits.edit",
        "payroll.advances.read",
        "payroll.advances.production.issue",
        "payroll.advances.admin.issue",
        "payroll.advances.backdate",
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
        "source.schedule.dishwashers.edit",
        "source.shift_ledger.read",
        "source.shift_ledger.input",
        "source.shift_ledger.correct",
        "source.revenue.read",
        "source.revenue.edit",
        "source.import.run",
        "source.import_errors.review",
        "finance.store_cash.read",
        "finance.store_cash.enter",
        "finance.cashflow.read",
        "finance.cashflow.classify",
        "finance.safe.allocate",
        "finance.safe.confirm_paid",
        "finance.payout_channel.cash_tk",
        "finance.payout_channel.safe",
        "finance.payout_channel.bank_draft",
        "revisions.read",
        "revisions.import",
        "revisions.create",
        "revisions.drafts.edit",
        "revisions.items.exclude",
        "revisions.items.adjust",
        "revisions.employees.exclude",
        "revisions.deferrals.read",
        "counterparties.read",
        "counterparties.operate",
        "invoices.normal.create",
        "invoices.normal.edit",
        "invoices.normal.pay",
        "invoices.barter.create",
        "invoices.barter.edit",
        "invoices.barter.pay",
        "kassa.shifts.read",
        "kassa.shifts.sync",
        "kassa.shifts.post",
        "kassa.cheques.read",
        "kassa.cheques.create",
        "kassa.invoices.create",
        "kassa.invoices.push",
        "kassa.refs.read",
        "kassa.journal.read",
        "kassa.payouts.create",
        "settings.general.read",
        "settings.positions.read",
    }
)

OFFICE_MANAGER_DEFAULT_PERMISSIONS = frozenset(
    VISIBLE_PERMISSION_CODES
    - {
        "payroll.runs.reopen",
        "payroll.loans.issue",
        "payroll.advances.backdate",
        "revisions.import",
        "revisions.create",
        "revisions.drafts.edit",
        "revisions.items.exclude",
        "revisions.items.adjust",
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
        "counterparties.admin",
        # Корректировки кассы — точечное право, не входит в дефолт Менеджера.
        "kassa.adjustments.create",
        # Отмена авто-штрафов за недостачу — точечное право (только owner/admin).
        "kassa.penalty.waive",
        # ПИН внештатника — только у тех, кто выдаёт смены (owner/admin/управляющий).
        "staff.freelancer_pin.read",
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
        "kassa.shifts.read",
        "kassa.shifts.sync",
        "kassa.cheques.read",
        "kassa.cheques.create",
        "kassa.invoices.create",
        "kassa.invoices.push",
        "kassa.refs.read",
        "kassa.journal.read",
        "kassa.payouts.create",
    }
)

SENIOR_COURIER_DEFAULT_PERMISSIONS = frozenset(
    {
        "couriers.list.read",
        "couriers.statistics.read",
        "couriers.schedule.read",
        "couriers.evaluations.read",
        "couriers.deposits.read",
    }
)

# Узкая роль терминала упаковки: только очередь упаковки, без остального приложения.
KITCHEN_DEFAULT_PERMISSIONS = frozenset(
    {
        "kds.queue.read",
        "kds.queue.write",
    }
)

DEFAULT_ROLE_PERMISSIONS = {
    "manager": MANAGER_DEFAULT_PERMISSIONS,
    "office_manager": OFFICE_MANAGER_DEFAULT_PERMISSIONS,
    "cashier": CASHIER_DEFAULT_PERMISSIONS,
    "senior_courier": SENIOR_COURIER_DEFAULT_PERMISSIONS,
    "kitchen": KITCHEN_DEFAULT_PERMISSIONS,
}


# Каналы выдачи денег → право на канал. Используется и для немедленной выдачи депозита
# (cash_tk/cash_safe/bank_draft), и для выбора наличного счёта/банк-черновика зарплаты
# (tk_chernikova/safe/bank_draft). Право требуется ДОПОЛНИТЕЛЬНО к операционному.
PAYOUT_CHANNEL_PERMISSIONS: dict[str, str] = {
    "cash_tk": "finance.payout_channel.cash_tk",
    "tk_chernikova": "finance.payout_channel.cash_tk",
    "cash_safe": "finance.payout_channel.safe",
    "safe": "finance.payout_channel.safe",
    "bank_draft": "finance.payout_channel.bank_draft",
    # Сбер-черновик — тот же канал-право, что и Т-Банк-черновик (по решению владельца).
    "bank_draft_sber": "finance.payout_channel.bank_draft",
}


def payout_channel_permission(channel: str | None) -> str | None:
    """Право на канал выдачи по коду счёта/метода (None — канал не распознан/не требует права)."""
    if channel is None:
        return None
    return PAYOUT_CHANNEL_PERMISSIONS.get(channel)


def expand_permission_codes(codes: Iterable[str]) -> frozenset[str]:
    expanded = set(codes)
    for legacy_code, replacement_codes in LEGACY_PERMISSION_ALIASES.items():
        if legacy_code in expanded:
            expanded.update(replacement_codes)
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
    expanded_codes = expand_permission_codes(granted_codes)
    if required_code in expanded_codes:
        return True
    replacement_codes = LEGACY_PERMISSION_ALIASES.get(required_code)
    if replacement_codes is None:
        return False
    return replacement_codes <= expanded_codes


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
