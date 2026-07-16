import { api } from "@/lib/api";

// Одна нормализованная строка платежа из агрегатора (см. finance_payments.py).
export type PaymentRow = {
  id: string;
  source: "invoice" | "draft" | "reserve" | "payroll_draft" | "deposit_draft" | "advance_draft";
  kind: string;
  ref_id: string;
  title: string;
  counterparty_id: string | null;
  counterparty_name: string | null;
  amount: number;
  amount_paid: number | null;
  article_id: string | null;
  article_name: string | null;
  method: "bank" | "cash";
  bank_channel: string | null;
  state: string;
  state_label: string;
  bucket: string | null;
  bucket_label: string | null;
  created_at: string;
  can_edit: boolean;
  can_send_to_bank: boolean;
  can_pay: boolean;
  can_cancel: boolean;
  extra: Record<string, unknown>;
};

export type BucketMeta = { key: string; label: string; count: number };

export type PaymentsResponse = {
  scope: "active" | "all";
  buckets: BucketMeta[];
  items: PaymentRow[];
};

export async function getPayments(scope: "active" | "all" = "active"): Promise<PaymentsResponse> {
  const response = await api.get<PaymentsResponse>("/finance/payments", { params: { scope } });
  return response.data;
}

export const BUCKET_ORDER = [
  "to_review",
  "to_pay",
  "bank_ready",
  "reserved_safe",
  "reserved_kassa",
] as const;
