import type { CookingStation, EmployeeCategory, EmployeeStatus, PayrollRole } from "@/lib/api";

export const EMPLOYEE_STATUS_LABELS: Record<EmployeeStatus, string> = {
  active: "Активен",
  requires_setup: "Требует проверки",
  inactive: "Уволен",
};

export const EMPLOYEE_STATUS_COLORS: Record<EmployeeStatus, "green" | "yellow" | "zinc"> = {
  active: "green",
  requires_setup: "yellow",
  inactive: "zinc",
};

export const EMPLOYEE_CATEGORY_LABELS: Record<EmployeeCategory, string> = {
  category_1: "1-я",
  category_2: "2-я",
  category_3: "3-я",
  category_4: "4-я",
  intern: "Стажёр",
  freelancer: "Внештатный",
};

export const COOKING_STATION_LABELS: Record<CookingStation, string> = {
  sushi: "Сушист",
  pizza: "Пиццерист",
  shawarma: "Шаурмист",
};

export const PAYROLL_ROLE_LABELS: Record<PayrollRole, string> = {
  sushi: "Сушист",
  pizza: "Пиццерист",
  shawarma: "Шаурмист",
  prep: "Заготовщик",
  administrator: "Администратор",
};
