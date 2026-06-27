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
  recipient_name: string | null;
  inn: string | null;
  amount: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  requisites: Record<string, string>;
  requisites_verified: boolean;
  invoice_payment_status: string | null;
  invoice_in_draft: boolean;
  has_pdf: boolean;
  created_at: string;
};

export type ConfirmPayload = {
  counterparty_id?: string | null;
  new_counterparty_name?: string | null;
  new_counterparty_inn?: string | null;
  amount?: string | null;
  invoice_number?: string | null;
  invoice_date?: string | null;
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

// Отправить подтверждённый счёт в банк (банк-черновик, как у накладных). Деньги не списываются.
export async function sendToBank(id: string): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(`${BASE}/intakes/${id}/send-to-bank`);
  return response.data;
}

// PDF под авторизацией — тянем blob (прямую ссылку без токена API не отдаст) и возвращаем
// object-URL. Открытие/скачивание делает вызывающий (окно надо открыть синхронно по клику,
// иначе попап блокируется браузером после await).
export async function fetchIntakePdfUrl(id: string): Promise<string> {
  const response = await api.get(`${BASE}/intakes/${id}/pdf`, { responseType: "blob" });
  return URL.createObjectURL(response.data as Blob);
}
