import type {
  CookingStation,
  EmployeeCategory,
  EmployeeChangeSource,
  EmployeeChangeStatus,
  EmployeeStatus,
  PayrollRole,
} from "@/lib/api";

export const EMPLOYEE_STATUS_LABELS: Record<EmployeeStatus, string> = {
  active: "Активен",
  requires_setup: "Требует проверки",
  dismissing: "В увольнении",
  inactive: "Уволен",
};

export const EMPLOYEE_STATUS_COLORS: Record<EmployeeStatus, "green" | "yellow" | "zinc"> = {
  active: "green",
  requires_setup: "yellow",
  dismissing: "yellow",
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

export type EmployeePinState = "iiko_assumed" | "local" | "missing";

export const EMPLOYEE_PIN_BADGE_LABELS: Record<EmployeePinState, string> = {
  iiko_assumed: "ПИН задан в iiko",
  local: "Локальный ПИН",
  missing: "ПИН не установлен",
};

export const EMPLOYEE_PIN_BUTTON_LABELS: Record<EmployeePinState, string> = {
  iiko_assumed: "Установить локальный ПИН",
  local: "Сменить ПИН",
  missing: "Установить ПИН",
};

export const EMPLOYEE_CHANGE_SOURCE_LABELS: Record<EmployeeChangeSource, string> = {
  app: "Приложение",
  iiko_sync: "Синхронизация IIko",
  system_migration: "Системная миграция",
};

export const EMPLOYEE_CHANGE_STATUS_LABELS: Record<EmployeeChangeStatus, string> = {
  success: "Успешно",
  error: "Ошибка",
  requires_review: "Требует проверки",
  skipped: "Пропуск",
};

export const EMPLOYEE_CHANGE_TYPE_LABELS: Record<string, string> = {
  create_employee: "Создание сотрудника",
  update_full_name: "Изменение ФИО",
  update_position: "Изменение должности",
  employee_update: "Изменение карточки",
  change_role: "Изменение роли",
  assign_role: "Назначение роли",
  close_role: "Закрытие роли",
  change_category: "Изменение категории",
  set_senior: "Назначен Старший",
  unset_senior: "Снят Старший",
  set_deputy_senior: "Назначен Зам старшего",
  unset_deputy_senior: "Снят Зам старшего",
  set_hire_date: "Установка даты приёма",
  change_pin: "Смена ПИН",
  notice_given: "Уведомление об уходе",
  notice_cancelled: "Отмена уведомления об уходе",
  dismiss: "Увольнение",
  cancel_dismissal: "Отмена увольнения",
  reinstate: "Восстановление",
  iiko_sync_create: "Создание из IIko",
  iiko_sync_update: "Обновление из IIko",
  iiko_sync_deactivate: "Деактивация из IIko",
  iiko_sync_skipped: "Пропуск IIko",
  iiko_sync_error: "Ошибка IIko",
};

export const EMPLOYEE_CHANGE_FIELD_LABELS: Record<string, string> = {
  full_name: "ФИО",
  position: "Должность",
  payroll_role: "Роль",
  roles: "Роли",
  category: "Категория",
  default_cooking_station: "Цех",
  is_primary: "Основная роль",
  is_senior: "Старший",
  is_deputy_senior: "Зам старшего",
  status: "Статус",
  hire_date: "Дата приёма",
  fire_date: "Дата увольнения",
  fire_reason: "Причина увольнения",
  effective_from: "Дата действия с",
  effective_to: "Дата действия по",
  pin_changed: "ПИН",
  reason: "Причина",
  comment: "Комментарий",
};
