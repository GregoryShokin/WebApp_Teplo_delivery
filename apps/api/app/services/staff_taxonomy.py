from __future__ import annotations

from app.services import position_registry

# Исторический канон должностей. Источник истины теперь — реестр должностей
# (таблица `position`, см. position_registry); эти кортежи остаются как сид
# и fallback до загрузки реестра из БД.
CANONICAL_POSITIONS = (
    "Кассир",
    "Повар",
    "Управляющий",
    "Системный администратор",
    "Курьер",
    "Менеджер",
    "Уборщица",
    "Посудомойка",
)
CREATE_POSITIONS = ("Кассир", "Менеджер", "Повар", "Управляющий", "Курьер")
AUXILIARY_POSITIONS = ("Уборщица", "Посудомойка")

PAYROLL_ROLE_LABELS = {
    "administrator": "Администратор",
    "sushi": "Сушист",
    "pizza": "Пиццерист",
    "shawarma": "Шаурмист",
    "prep": "Заготовщик",
}

PAYROLL_ROLE_POSITION = {
    "administrator": "Кассир",
    "sushi": "Повар",
    "pizza": "Повар",
    "shawarma": "Повар",
    "prep": "Повар",
}

COOK_PAYROLL_ROLES = frozenset({"sushi", "pizza", "shawarma", "prep"})
CASHIER_PAYROLL_ROLES = frozenset({"administrator"})

PAYROLL_ROLE_TO_TARGET_POSITION = {
    **{role: "Повар" for role in COOK_PAYROLL_ROLES},
    **{role: "Кассир" for role in CASHIER_PAYROLL_ROLES},
}

POSITION_PAYROLL_ROLES = {
    "Кассир": ("administrator",),
    "Повар": ("sushi", "pizza", "shawarma", "prep"),
    "Управляющий": (),
    "Системный администратор": (),
    "Курьер": (),
    "Менеджер": (),
    "Уборщица": (),
    "Посудомойка": (),
}

ROLE_CATEGORIES = {
    "administrator": ("category_2", "category_3", "category_4", "intern"),
    "sushi": ("category_1", "category_2", "category_3", "intern"),
    "pizza": ("category_1", "category_2", "category_3", "intern"),
    "shawarma": ("category_3", "category_4", "intern"),
    "prep": ("category_3", "intern"),
}

EMPLOYEE_CATEGORIES = (
    "category_1",
    "category_2",
    "category_3",
    "category_4",
    "intern",
    "freelancer",
)
DEPRECATED_EMPLOYEE_CATEGORIES = ("freelancer",)

COOKING_STATIONS = ("sushi", "pizza", "shawarma")

PREMIUM_APPLICABILITY = {
    "Кассир": {"is_senior": True, "is_deputy_senior": True},
    "Повар": {"is_senior": True, "is_deputy_senior": True},
    # Старшинство курьера вынесено в отдельную должность «Старший курьер»
    # (а не галочку is_senior), поэтому надбавка на курьере больше не применима.
    "Курьер": {"is_senior": False, "is_deputy_senior": False},
    "Управляющий": {"is_senior": False, "is_deputy_senior": False},
    "Системный администратор": {"is_senior": False, "is_deputy_senior": False},
    "Менеджер": {"is_senior": False, "is_deputy_senior": False},
    "Уборщица": {"is_senior": False, "is_deputy_senior": False},
    "Посудомойка": {"is_senior": False, "is_deputy_senior": False},
}

DEFAULT_STATION_BY_PAYROLL_ROLE = {
    "administrator": "Касса",
    "sushi": "Роллы",
    "pizza": "Пицца",
    "shawarma": "Горячий цех",
    "prep": None,
}

PAYROLL_ROLE_ALIASES = {
    **{label: role for role, label in PAYROLL_ROLE_LABELS.items()},
    "Касса": "administrator",
}


def normalize_position(position: str | None) -> str:
    return _normalize_position(position)


def canonical_position_name(position: str | None) -> str | None:
    return position_registry.canonical_position_name(position)


def is_canonical_position(position: str | None) -> bool:
    return canonical_position_name(position) is not None


def is_create_position(position: str | None) -> bool:
    """Должность доступна для выбора при создании/назначении (активна в реестре)."""
    return position_registry.is_active_position(position)


def position_requires_pin(position: str | None) -> bool:
    info = position_registry.position_info(position)
    return info is not None and info.permission_group != "auxiliary"


def payroll_roles_for_position(position: str | None) -> tuple[str, ...]:
    canonical = canonical_position_name(position)
    if canonical is None:
        return ()
    return POSITION_PAYROLL_ROLES.get(canonical, ())


def position_allows_payroll_role(position: str | None, payroll_role: str) -> bool:
    return payroll_role in payroll_roles_for_position(position)


def categories_for_payroll_role(payroll_role: str | None) -> tuple[str, ...]:
    if payroll_role is None:
        return ()
    return ROLE_CATEGORIES.get(payroll_role, ())


def payroll_role_allows_category(payroll_role: str, category: str | None) -> bool:
    return category in categories_for_payroll_role(payroll_role)


def target_position_for_payroll_role(payroll_role: str) -> str:
    return PAYROLL_ROLE_TO_TARGET_POSITION[payroll_role]


def default_station_for_payroll_role(role: str | None) -> str | None:
    if role is None:
        return None
    payroll_role = PAYROLL_ROLE_ALIASES.get(role, role)
    return DEFAULT_STATION_BY_PAYROLL_ROLE.get(payroll_role)


def premium_applicability(position: str | None) -> dict[str, bool]:
    canonical = canonical_position_name(position)
    if canonical is None:
        return {"is_senior": False, "is_deputy_senior": False}
    return PREMIUM_APPLICABILITY.get(
        canonical, {"is_senior": False, "is_deputy_senior": False}
    )


def reset_inapplicable_premiums(
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
) -> tuple[bool, bool]:
    applicability = premium_applicability(position)
    return (
        is_senior if applicability["is_senior"] else False,
        is_deputy_senior if applicability["is_deputy_senior"] else False,
    )


def validate_premiums(
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
) -> None:
    applicability = premium_applicability(position)
    if is_senior and not applicability["is_senior"]:
        raise ValueError("Надбавка «Старший» недоступна для этой должности")
    if is_deputy_senior and not applicability["is_deputy_senior"]:
        raise ValueError("Надбавка «Зам старшего» недоступна для этой должности")


def _normalize_position(position: str | None) -> str:
    return position_registry.normalize_position_name(position)
