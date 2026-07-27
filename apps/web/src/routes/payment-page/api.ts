import { api } from "@/lib/api";

import { getRegistry } from "../counterparties/api";

// Одна запись журнала разбора счёта из почты (email_invoice_intake).
export type PaymentIntake = {
  id: string;
  mailbox: string;
  from_addr: string | null;
  subject: string | null;
  received_at: string | null;
  attachment_filename: string | null;
  status: string;
  engine: string | null;
  confidence: number | null;
  counterparty_id: string | null;
  counterparty_name: string | null;
  invoice_id: string | null;
  // Пакет «счёт + УПД» одним файлом: закрывающий документ, заведённый вместе со счётом.
  companion_invoice_id: string | null;
  companion_amount: string | null;
  recipient_name: string | null;
  inn: string | null;
  amount: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  service_period_start: string | null;
  service_period_end: string | null;
  service_period_source: string | null;
  service_period_status: string | null;
  service_period_confidence: number | null;
  service_period_required: boolean;
  requisites: Record<string, string>;
  requisites_verified: boolean;
  invoice_payment_status: string | null;
  invoice_in_draft: boolean;
  invoice_dds_article_id: string | null;
  default_dds_article_id: string | null;
  has_pdf: boolean;
  scheduled_send_date: string | null;
  created_at: string;
};

export type ConfirmPayload = {
  counterparty_id?: string | null;
  new_counterparty_name?: string | null;
  new_counterparty_inn?: string | null;
  amount?: string | null;
  invoice_number?: string | null;
  invoice_date?: string | null;
  service_period_start?: string | null;
  service_period_end?: string | null;
  requisites?: Record<string, string | null> | null;
  apply_requisites?: boolean;
};

export type CounterpartyOption = { value: string; label: string };

const BASE = "/payment-page";

export async function listCounterpartyOptions(): Promise<CounterpartyOption[]> {
  const rows = await getRegistry();
  return rows
    .map((r) => ({
      value: r.counterparty_id,
      label: r.inn ? `${r.name} · ИНН ${r.inn}` : r.name,
    }))
    .sort((a, b) => a.label.localeCompare(b.label, "ru"));
}

export async function listIntakes(status?: string): Promise<PaymentIntake[]> {
  const response = await api.get<PaymentIntake[]>(`${BASE}/intakes`, {
    params: status ? { status } : undefined,
  });
  return response.data;
}

export async function getIntake(id: string): Promise<PaymentIntake> {
  const response = await api.get<PaymentIntake>(`${BASE}/intakes/${id}`);
  return response.data;
}

export async function confirmIntake(
  id: string,
  payload: ConfirmPayload = {},
): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/confirm`, payload);
  return response.data;
}

export async function ignoreIntake(id: string): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/ignore`);
  return response.data;
}

// Статья ДДС для оплаты + закрепление за контрагентом (общее для немедленной и плановой отправки).
export type ArticleChoice = {
  dds_article_id?: string | null;
  remember_for_counterparty?: boolean;
};

// Отправить подтверждённый счёт в банк (банк-черновик, как у накладных). Деньги не списываются.
export async function sendToBank(id: string, choice: ArticleChoice = {}): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/send-to-bank`, choice);
  return response.data;
}

// Запланировать авто-отправку в банк к дате (YYYY-MM-DD). Джоба отправит, когда дата наступит.
export async function scheduleSend(
  id: string,
  sendDate: string,
  choice: ArticleChoice = {},
): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/schedule-send`, {
    send_date: sendDate,
    ...choice,
  });
  return response.data;
}

export async function cancelSchedule(id: string): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/cancel-schedule`);
  return response.data;
}

// Исключить счёт из рабочего инбокса в корзину «Исключённые».
export async function excludeIntake(id: string): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/exclude`);
  return response.data;
}

// Вернуть счёт из «Исключённых» в рабочий инбокс.
export async function restoreIntake(id: string): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/restore`);
  return response.data;
}

// Удалить исключённый счёт навсегда (вместе с накладной, если она не в банке/оплате).
export async function deleteIntake(id: string): Promise<void> {
  await api.delete(`${BASE}/intakes/${id}`);
}

// PDF под авторизацией — тянем blob (прямую ссылку без токена API не отдаст) и возвращаем
// object-URL. Открытие/скачивание делает вызывающий (окно надо открыть синхронно по клику,
// иначе попап блокируется браузером после await).
export async function fetchIntakePdfUrl(id: string): Promise<string> {
  const response = await api.get(`${BASE}/intakes/${id}/pdf`, { responseType: "blob" });
  return URL.createObjectURL(response.data as Blob);
}
