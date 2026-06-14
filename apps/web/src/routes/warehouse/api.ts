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

export async function pushInvoiceToIiko(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}/push`);
  return response.data;
}

export type OpenLoan = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  number: string | null;
  issued_at: string | null;
  we_lend: boolean;
  amount: number;
  returned: number;
  remaining: number;
  status: string | null;
};

export type ReturnableLine = {
  id: string;
  name: string;
  unit: string | null;
  price: number;
  quantity: number;
  remaining_qty: number;
};

export type LoanReturnable = {
  id: string;
  number: string | null;
  we_lend: boolean;
  amount: number;
  remaining: number;
  lines: ReturnableLine[];
};

export type ReturnPayload = {
  loan_id: string;
  issued_at: string;
  number?: string | null;
  returns: Array<{ amount: number; loan_line_item_id?: string | null; quantity?: number | null }>;
};

export async function getOpenLoans(counterpartyId?: string): Promise<OpenLoan[]> {
  const response = await api.get<OpenLoan[]>(`${BASE}/loans`, {
    params: counterpartyId ? { counterparty_id: counterpartyId } : undefined,
  });
  return response.data;
}

export async function getLoanReturnable(loanId: string): Promise<LoanReturnable> {
  const response = await api.get<LoanReturnable>(`${BASE}/loans/${loanId}/returnable`);
  return response.data;
}

export async function createBarterReturn(
  payload: ReturnPayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/return`, payload);
  return response.data;
}
