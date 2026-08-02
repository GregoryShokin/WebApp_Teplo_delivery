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
  /** Режим признания услуг: 'per_invoice' — расход принесёт УПД, период спрашивать незачем. */
  service_billing_mode: string | null;
  // Что распознано в самом PDF. Правки оператора сюда не пишутся — они в reviewed_requisites.
  requisites: Record<string, string>;
  // Правки оператора с прошлого разбора этого счёта: приоритетный источник для формы.
  reviewed_requisites: Record<string, string>;
  requisites_verified: boolean;
  invoice_payment_status: string | null;
  invoice_in_draft: boolean;
  invoice_dds_article_id: string | null;
  default_dds_article_id: string | null;
  has_pdf: boolean;
  /** Тип вложения: у почты PDF, у коммунальной платёжки — фотография. */
  attachment_mime: string | null;
  // Коммунальная платёжка. Поток несёт то, чего нет в самой квитанции: помещение, получателя
  // денег и статью — в бумаге стоят реквизиты ресурсника, а платим мы арендодателю.
  utility_account_id: string | null;
  utility_kind: string | null;
  utility_kind_label: string | null;
  /** 'actual' — акт за факт (несёт расход), 'advance' — авансовый (только платится). */
  utility_act_kind: string | null;
  /** Расход периода целиком. У электричества больше суммы к оплате на зачтённый аванс. */
  utility_expense_amount: string | null;
  utility_payable_amount: string | null;
  utility_period_label: string | null;
  /** Подсказки («ждём парный акт») — не мешают платить, объясняют, чего ждать. */
  utility_hints: string[];
  /** Из-за чего строка ждёт рук человека. Пусто — разбор прошёл. */
  utility_blocking: string[];
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

// Ручное проведение коммунальной платёжки: суммы две, потому что в бумаге их две. У воды они
// совпадают, у акта за факт нет — из расхода вычтен внесённый аванс. Пустой expense_amount
// означает авансовый документ: он платится, но расхода не несёт.
export type ConfirmUtilityPayload = {
  utility_account_id: string;
  period_start: string;
  period_end: string;
  expense_amount?: string | null;
  payable_amount: string;
};

export type CounterpartyOption = { value: string; label: string };

// Реквизиты карточки контрагента — то, чем платёж реально уйдёт в банк. Окно разбора берёт
// их, когда в счёте реквизиты не распознались, и перечитывает при смене контрагента.
export type CounterpartyRequisites = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  requisites: Record<string, string>;
  requisites_verified: boolean;
};

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

export async function getCounterpartyRequisites(
  counterpartyId: string,
): Promise<CounterpartyRequisites> {
  const response = await api.get<CounterpartyRequisites>(
    `${BASE}/counterparties/${counterpartyId}/requisites`,
  );
  return response.data;
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

// Принести документ руками — третий источник страницы рядом с почтой и ЭДО. Ответ СПИСКОМ:
// один файл несёт столько документов, сколько в нём есть (у энергетика за визит их два).
export async function uploadIntake(
  file: File,
  utilityAccountId?: string | null,
): Promise<PaymentIntake[]> {
  const form = new FormData();
  form.append("file", file);
  if (utilityAccountId) form.append("utility_account_id", utilityAccountId);
  // Content-Type не ставим руками: браузер сам допишет boundary, без которого сервер не
  // разберёт тело.
  const response = await api.post<PaymentIntake[]>(`${BASE}/intakes/upload`, form);
  return response.data;
}

// Провести коммунальную платёжку вручную, когда разбор не справился (газ приходит без бумаги).
export async function confirmUtilityIntake(
  id: string,
  payload: ConfirmUtilityPayload,
): Promise<PaymentIntake> {
  const response = await api.post<PaymentIntake>(
    `${BASE}/intakes/${id}/confirm-utility`,
    payload,
  );
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

// Несколько счетов ОДНИМ платежом: энергетик привозит два акта, а перевод человек делает один.
export async function sendManyToBank(intakeIds: string[]): Promise<PaymentIntake[]> {
  const response = await api.post<PaymentIntake[]>(`${BASE}/intakes/send-to-bank`, {
    intake_ids: intakeIds,
  });
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

// Исходный документ под авторизацией — тянем blob (прямую ссылку без токена API не отдаст) и
// возвращаем object-URL. Это уже не всегда PDF: коммунальная платёжка приходит фотографией, и
// показывать её надо картинкой. Открытие/скачивание делает вызывающий (окно надо открыть
// синхронно по клику, иначе попап блокируется браузером после await).
export async function fetchIntakeFileUrl(id: string): Promise<string> {
  const response = await api.get(`${BASE}/intakes/${id}/pdf`, { responseType: "blob" });
  return URL.createObjectURL(response.data as Blob);
}
