# Курьерский payroll — заглушка (вне MVP)

Решение от 2026-06-04: бонусы и штрафы курьерам **не входят в MVP** курьерского модуля. Прорабатываются отдельно после фиксации KPI и оценок админов.

Что зафиксировано:

- Ставка / правила оплаты курьеров — не определены, владелец не подтверждал источник.
- В Google Sheets «График курьеров» правила оплаты не найдены (см. `research/archive/couriers-sheets-discovery/04-kpi-definitions.md`).
- KPI MVP не привязан к выплатам — см. `app-spec/modules/couriers/kpi-methodology.md`.

Следующий шаг по этой спеке: после стабилизации KPI MVP и оценок админов вернуться и собрать с владельцем модель выплат (per-order / per-shift / mix / bonus / штрафы).

См. также:

- `app-spec/modules/couriers/kpi-methodology.md` — утверждённая методология KPI MVP
- `app-spec/integrations/iiko/courier-service/` — старая webhook-интеграция (заменяется polling через iikoServer Resto)
- `research/archive/couriers-sheets-discovery/` — структура текущих Google Sheets
