import { api } from "@/lib/api";

const BASE = "/warehouse";

export type WarehouseProduct = {
  id: string;
  iiko_id: string;
  name: string;
  code: string | null;
  unit: string | null;
  type: string;
};

export type WarehouseInvoiceSummary = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  source: string;
  direction: string;
  number: string | null;
  issued_at: string | null;
  invoice_date: string | null;
  amount: number;
  staff_amount: number;
  payment_status: string;
  iiko_push_status: string;
};

export type WarehouseInvoiceLine = {
  id: string;
  name: string;
  article: string | null;
  unit: string | null;
  quantity: number;
  price: number;
  sum: number;
  vat_percent: number | null;
  is_staff: boolean;
};

export type WarehouseInvoiceDetail = WarehouseInvoiceSummary & {
  due_date: string | null;
  iiko_push_error: string | null;
  lines: WarehouseInvoiceLine[];
};

export type LinePayload = {
  name: string;
  quantity: number;
  price: number;
  iiko_product_id?: string | null;
  vat_percent?: number | null;
  is_staff: boolean;
};

export type CreateInvoicePayload = {
  counterparty_id: string;
  issued_at: string;
  mode?: "normal" | "loan";
  we_lend?: boolean;
  number?: string | null;
  due_date?: string | null;
  lines: LinePayload[];
};

export async function getProducts(params?: {
  q?: string;
  type?: string;
  include_deleted?: boolean;
  limit?: number;
}): Promise<WarehouseProduct[]> {
  const response = await api.get<WarehouseProduct[]>(`${BASE}/products`, { params });
  return response.data;
}

export async function syncProducts(): Promise<{
  seen: number;
  created: number;
  updated: number;
  goods_count: number;
  needs_unit_mapping: string[];
}> {
  const response = await api.post(`${BASE}/products/sync`);
  return response.data;
}

export async function getWarehouseInvoices(params?: {
  status?: string;
  has_staff?: boolean;
}): Promise<WarehouseInvoiceSummary[]> {
  const response = await api.get<WarehouseInvoiceSummary[]>(`${BASE}/invoices`, { params });
  return response.data;
}

export async function getWarehouseInvoice(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.get<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}`);
  return response.data;
}

export async function createWarehouseInvoice(
  payload: CreateInvoicePayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices`, payload);
  return response.data;
}

export async function getNextInvoiceNumber(): Promise<string> {
  const response = await api.get<{ number: string }>(`${BASE}/invoices/next-number`);
  return response.data.number;
}
