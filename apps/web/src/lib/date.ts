/** Календарные даты в формате `YYYY-MM-DD` — всегда по ЛОКАЛЬНОЙ зоне пользователя (МСК).
 *
 * `new Date().toISOString().slice(0, 10)` даёт дату по UTC: с 00:00 до 03:00 по Москве это
 * ВЧЕРАШНИЙ день. Для денежных дат (дата оплаты, дата операции) такая ошибка уводит платёж в
 * прошлый день, а иногда и в прошлый закрытый период — поэтому дату собираем из локальных
 * компонент, а не режем ISO-строку.
 */
export function toIsoDate(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Сегодняшняя дата пользователя в формате `YYYY-MM-DD`. */
export function todayIso(): string {
  return toIsoDate(new Date());
}
