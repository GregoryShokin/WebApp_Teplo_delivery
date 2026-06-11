import { api } from "@/lib/api";

export type LedgerCategory = {
  id: string;
  code: string;
  name: string;
  sort_order: number;
  is_active: boolean;
};

export type CounterpartyInvoice = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  ledger_category_id: string | null;
  source: string;
  number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  amount: number;
  vat_total: number;
  vat_breakdown: Record<string, string>;
  allocated: number;
  remaining: number;
  payment_status: string;
  draft_id: string | null;
};

export type RegistryItem = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  status: string;
  ledger_category_id: string | null;
  brand_group: string | null;
  internal_name: string | null;
  payment_delay_days: number | null;
  requisites_verified: boolean;
  unpaid_count: number;
  unpaid_remaining: number;
};

export type CollectionSource = {
  id: string;
  kind: string;
  value: string | null;
  is_active: boolean;
  note: string | null;
};

export type CounterpartyProfile = {
  ledger_category_id: string | null;
  brand_group: string | null;
  internal_name: string | null;
  payment_delay_days: number | null;
  payment_due_day_of_month: number | null;
  manager_name: string | null;
  manager_phone: string | null;
  requisites: Record<string, unknown>;
  requisites_verified: boolean;
  status: string;
} | null;

export type CounterpartyCard = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  type: string;
  status: string;
  profile: CounterpartyProfile;
  aliases: Array<{ alias: string; source: string | null }>;
  collection_sources: CollectionSource[];
  routing_rules: Array<{
    id: string;
    prefix: string;
    target_counterparty_id: string;
    target_name: string;
  }>;
  invoices: CounterpartyInvoice[];
  drafts: Array<{
    id: string;
    amount: number;
    status: string;
    provider_ref: string | null;
    created_at: string;
  }>;
};

export type PaymentDraft = {
  id: string;
  counterparty_id: string;
  document_id: string;
  amount: number;
  status: string;
  provider_ref: string | null;
  last_error: string | null;
  created_at: string;
};

export type NeedsSetup = {
  count: number;
  items: Array<{
    counterparty_id: string;
    name: string;
    inn: string | null;
    created_at: string;
  }>;
};

const BASE = "/counterparties";

export async function getLedgerCategories(): Promise<LedgerCategory[]> {
  const response = await api.get<LedgerCategory[]>(`${BASE}/categories`);
  return response.data;
}

export async function getInvoices(params?: {
  status?: string;
  counterparty_id?: string;
  category_id?: string;
  in_draft?: boolean;
}): Promise<CounterpartyInvoice[]> {
  const response = await api.get<CounterpartyInvoice[]>(`${BASE}/invoices`, { params });
  return response.data;
}

export type ManualInvoicePayload = {
  counterparty_id: string;
  amount: number;
  number?: string | null;
  invoice_date?: string | null;
  due_date?: string | null;
  note?: string | null;
  vat_breakdown?: Record<string, number> | null;
};

export async function createManualInvoice(
  payload: ManualInvoicePayload,
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(`${BASE}/invoices`, payload);
  return response.data;
}

export async function voidInvoice(id: string): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(`${BASE}/invoices/${id}/void`);
  return response.data;
}

export async function allocateCash(
  id: string,
  payload: { amount: number; cashflow_transaction_id?: string | null },
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(
    `${BASE}/invoices/${id}/allocate-cash`,
    payload,
  );
  return response.data;
}

export async function getRegistry(params?: {
  category_id?: string;
  include_archived?: boolean;
}): Promise<RegistryItem[]> {
  const response = await api.get<RegistryItem[]>(`${BASE}/registry`, { params });
  return response.data;
}

export async function getNeedsSetup(): Promise<NeedsSetup> {
  const response = await api.get<NeedsSetup>(`${BASE}/needs-setup`);
  return response.data;
}

export async function getCounterpartyCard(id: string): Promise<CounterpartyCard> {
  const response = await api.get<CounterpartyCard>(`${BASE}/${id}`);
  return response.data;
}

export type CounterpartyCreatePayload = {
  name: string;
  inn?: string | null;
  type?: string;
  internal_name?: string | null;
  ledger_category_id?: string | null;
  brand_group?: string | null;
  payment_delay_days?: number | null;
  payment_due_day_of_month?: number | null;
  manager_name?: string | null;
  manager_phone?: string | null;
};

export async function createCounterparty(
  payload: CounterpartyCreatePayload,
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(BASE, payload);
  return response.data;
}

export type ProfileUpdatePayload = {
  ledger_category_id?: string | null;
  brand_group?: string | null;
  internal_name?: string | null;
  payment_delay_days?: number | null;
  payment_due_day_of_month?: number | null;
  manager_name?: string | null;
  manager_phone?: string | null;
  status?: string | null;
};

export async function updateProfile(
  id: string,
  payload: ProfileUpdatePayload,
): Promise<CounterpartyCard> {
  const response = await api.put<CounterpartyCard>(`${BASE}/${id}/profile`, payload);
  return response.data;
}

export async function setRequisites(
  id: string,
  payload: { requisites: Record<string, unknown>; verified: boolean },
): Promise<CounterpartyCard> {
  const response = await api.put<CounterpartyCard>(`${BASE}/${id}/requisites`, payload);
  return response.data;
}

export async function getRequisitesSuggestion(id: string): Promise<Record<string, unknown>> {
  const response = await api.get<Record<string, unknown>>(`${BASE}/${id}/requisites/suggestion`);
  return response.data;
}

export async function archiveCounterparty(id: string): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/archive`);
  return response.data;
}

export async function unarchiveCounterparty(id: string): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/unarchive`);
  return response.data;
}

export async function addCollectionSource(
  id: string,
  payload: { kind: string; value?: string | null; note?: string | null },
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/sources`, payload);
  return response.data;
}

export async function deleteCollectionSource(
  counterpartyId: string,
  sourceId: string,
): Promise<CounterpartyCard> {
  const response = await api.delete<CounterpartyCard>(
    `${BASE}/${counterpartyId}/sources/${sourceId}`,
  );
  return response.data;
}

export async function addRoutingRule(
  counterpartyId: string,
  payload: { prefix: string; target_counterparty_id: string },
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${counterpartyId}/routing`, payload);
  return response.data;
}

export async function deleteRoutingRule(
  counterpartyId: string,
  ruleId: string,
): Promise<CounterpartyCard> {
  const response = await api.delete<CounterpartyCard>(
    `${BASE}/${counterpartyId}/routing/${ruleId}`,
  );
  return response.data;
}

export async function getDrafts(params?: { counterparty_id?: string }): Promise<PaymentDraft[]> {
  const response = await api.get<PaymentDraft[]>(`${BASE}/drafts/list`, { params });
  return response.data;
}

export async function createDraft(invoiceIds: string[]): Promise<PaymentDraft> {
  const response = await api.post<PaymentDraft>(`${BASE}/drafts`, { invoice_ids: invoiceIds });
  return response.data;
}

export async function cancelDraft(id: string): Promise<void> {
  await api.post(`${BASE}/drafts/${id}/cancel`);
}

export async function runAutoMatch(): Promise<{ matched: number; needs_review: unknown[] }> {
  const response = await api.post<{ matched: number; needs_review: unknown[] }>(`${BASE}/match/auto`);
  return response.data;
}

export type MatchCandidate = {
  bank_operation_id: string;
  operation_date: string;
  amount: number;
  official_name: string | null;
  inn: string | null;
  requisites: Record<string, unknown>;
};

export type MatchSuggestion = {
  invoice_id: string;
  invoice_number: string | null;
  invoice_amount: number;
  counterparty_id: string;
  counterparty_name: string;
  counterparty_has_inn: boolean;
  candidates: MatchCandidate[];
  confident: boolean;
};

export async function getMatchSuggestions(): Promise<MatchSuggestion[]> {
  const response = await api.get<MatchSuggestion[]>(`${BASE}/match/suggestions`);
  return response.data;
}

export async function confirmMatch(payload: {
  invoice_id: string;
  bank_operation_id: string;
  enrich: boolean;
}): Promise<{ invoice_id: string; payment_status: string; enriched: boolean }> {
  const response = await api.post<{ invoice_id: string; payment_status: string; enriched: boolean }>(
    `${BASE}/match/confirm`,
    payload,
  );
  return response.data;
}

export async function syncInvoices(): Promise<Record<string, number>> {
  const response = await api.post<Record<string, number>>(`${BASE}/sync`);
  return response.data;
}
