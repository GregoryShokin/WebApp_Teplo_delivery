// Единый кросс-модульный источник цветов ролей производственного персонала.
// Используется в Графике сотрудников, Учёте смен и Штате — чтобы роль повара/кассира
// везде подсвечивалась одинаково. Менять палитру ТОЛЬКО здесь.
//
// Сушист — синий, Пиццерист — жёлтый, Шаурмист — фиолетовый,
// Заготовщик — тёмно-зелёный, Администратор/Кассир — розовый.

export type RoleColorClasses = {
  container: string;
  primaryText: string;
  secondaryText: string;
};

const ROLE_COLOR_FALLBACK: RoleColorClasses = {
  container: "border-border bg-background",
  primaryText: "text-foreground",
  secondaryText: "text-muted-foreground",
};

const SUSHI: RoleColorClasses = {
  container: "border-blue-300 bg-blue-50",
  primaryText: "text-blue-900",
  secondaryText: "text-blue-700",
};
const PIZZA: RoleColorClasses = {
  container: "border-yellow-300 bg-yellow-50",
  primaryText: "text-yellow-950",
  secondaryText: "text-yellow-800",
};
const SHAWARMA: RoleColorClasses = {
  container: "border-purple-300 bg-purple-50",
  primaryText: "text-purple-900",
  secondaryText: "text-purple-700",
};
const PREP: RoleColorClasses = {
  container: "border-emerald-800 bg-emerald-900",
  primaryText: "text-white",
  secondaryText: "text-emerald-100",
};
const ADMINISTRATOR: RoleColorClasses = {
  container: "border-pink-300 bg-pink-50",
  primaryText: "text-pink-900",
  secondaryText: "text-pink-700",
};

// Ключи — и payroll_role коды, и русские подписи ролей/должностей,
// чтобы вызывающие модули могли передавать любой из них.
const ROLE_COLOR_CLASSES: Record<string, RoleColorClasses> = {
  sushi: SUSHI,
  pizza: PIZZA,
  shawarma: SHAWARMA,
  prep: PREP,
  administrator: ADMINISTRATOR,
  Сушист: SUSHI,
  Пиццерист: PIZZA,
  Шаурмист: SHAWARMA,
  Заготовщик: PREP,
  Администратор: ADMINISTRATOR,
  Кассир: ADMINISTRATOR,
  Касса: ADMINISTRATOR,
};

export function roleColorClasses(role: string | null | undefined): RoleColorClasses {
  if (!role) {
    return ROLE_COLOR_FALLBACK;
  }
  return ROLE_COLOR_CLASSES[role] ?? ROLE_COLOR_FALLBACK;
}
