# Administrative payroll spec

Дата фиксации: 2026-05-29.
Источник: administrative-contour части бывшего гибридного payroll-документа.

## 1. Контур административной зарплаты

Администрация живёт на отдельном листе `Зарплата Администрации` и не связана напрямую с категорийной логикой смен production payroll.

Роли административного контура:

- `Директор по развитию`;
- `Управляющий`;
- `Руководитель службы доставки`;
- `Менеджер`;
- `Помощник менеджера`.

Целевая сущность: `admin_payroll_profile` с fixed amount, position, employee link, effective dates и source_reference.

## 2. Правила MVP

- Административные начисления не участвуют в production revenue-share denominator.
- Фиксированные суммы должны быть versioned по effective date.
- Выплаты администрации идут через `payroll_payment` как обязательство payroll-модуля; денежный факт закрывается через DDS.
- Риск двойного учета между production и administrative payroll закрывается явным `role_group` / profile type.
