import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { getCounterpartyRequisites, type PaymentIntake } from "./api";

export const REQUISITE_KEYS = [
  "recipientName",
  "inn",
  "kpp",
  "bankAcnt",
  "bankBik",
  "recipientCorrAccountNumber",
] as const;

export type RequisiteKey = (typeof REQUISITE_KEYS)[number];
export type RequisiteValues = Record<RequisiteKey, string>;

/** Откуда в поле попало значение — показываем под ним, чтобы человек не гадал. */
export type RequisiteSource = "reviewed" | "invoice" | "card" | "history" | "manual" | null;
export type RequisiteSources = Record<RequisiteKey, RequisiteSource>;

export const REQUISITE_SOURCE_LABELS: Record<Exclude<RequisiteSource, null>, string> = {
  reviewed: "сохранено при разборе",
  invoice: "из счёта",
  card: "из карточки",
  history: "из истории",
  manual: "введено вручную",
};

// Банковский блок заполняем ЦЕЛИКОМ из одного источника. Иначе счёт из свежего PDF
// склеился бы с БИК прошлого банка из карточки — платёжка с несогласованной парой
// «счёт + БИК» банк не примет, а если примет, деньги уйдут не туда.
const BANK_KEYS: readonly RequisiteKey[] = ["bankAcnt", "bankBik", "recipientCorrAccountNumber"];

const EMPTY_VALUES: RequisiteValues = {
  recipientName: "",
  inn: "",
  kpp: "",
  bankAcnt: "",
  bankBik: "",
  recipientCorrAccountNumber: "",
};

function clean(raw: Record<string, string> | null | undefined): Partial<RequisiteValues> {
  const out: Partial<RequisiteValues> = {};
  REQUISITE_KEYS.forEach((key) => {
    const value = (raw?.[key] ?? "").trim();
    if (value) out[key] = value;
  });
  return out;
}

export type PrefillInput = {
  /** Правки оператора с прошлого разбора этого счёта. */
  reviewed?: Record<string, string> | null;
  /** Распознанное из PDF. */
  recognized?: Record<string, string> | null;
  /** Реквизиты карточки выбранного контрагента. */
  card?: Record<string, string> | null;
  /** Плоские поля распознавания — последний фолбэк для имени и ИНН. */
  fallbackName?: string | null;
  fallbackInn?: string | null;
};

/** Значения формы реквизитов и источник каждого поля.
 *
 *  Порядок: правки оператора → счёт → карточка → пусто. Ничего не применяется молча:
 *  подпись источника у поля оставляет решение человеку, а отметку «реквизиты проверены»
 *  по-прежнему ставит он.
 */
export function buildPrefill(input: PrefillInput): {
  values: RequisiteValues;
  sources: RequisiteSources;
} {
  const reviewed = clean(input.reviewed);
  const recognized = clean(input.recognized);
  const card = clean(input.card);
  const values = { ...EMPTY_VALUES };
  const sources: RequisiteSources = {
    recipientName: null,
    inn: null,
    kpp: null,
    bankAcnt: null,
    bankBik: null,
    recipientCorrAccountNumber: null,
  };

  // Разбирали этот счёт раньше — показываем ровно то, что человек тогда утвердил,
  // включая поля, которые он сознательно оставил пустыми.
  const reviewedFilled = Object.keys(reviewed).length > 0;

  // Банк берём из счёта, только если в нём есть расчётный счёт: БИК без счёта
  // сам по себе платёжку не собирает, а смешивать источники нельзя.
  const bankFromInvoice = Boolean(recognized.bankAcnt);

  REQUISITE_KEYS.forEach((key) => {
    if (reviewedFilled) {
      if (reviewed[key]) {
        values[key] = reviewed[key] as string;
        sources[key] = "reviewed";
      }
      return;
    }
    if (BANK_KEYS.includes(key)) {
      const source = bankFromInvoice ? recognized : card;
      if (source[key]) {
        values[key] = source[key] as string;
        sources[key] = bankFromInvoice ? "invoice" : "card";
      }
      return;
    }
    if (recognized[key]) {
      values[key] = recognized[key] as string;
      sources[key] = "invoice";
      return;
    }
    if (card[key]) {
      values[key] = card[key] as string;
      sources[key] = "card";
    }
  });

  if (!reviewedFilled) {
    // Имя и ИНН распознавание кладёт ещё и плоскими полями — добираем их последними.
    if (!values.recipientName && input.fallbackName?.trim()) {
      values.recipientName = input.fallbackName.trim();
      sources.recipientName = "invoice";
    }
    if (!values.inn && input.fallbackInn?.trim()) {
      values.inn = input.fallbackInn.trim();
      sources.inn = "invoice";
    }
  }

  return { values, sources };
}

/** Расхождение расчётного счёта: в PDF один, в карточке другой.
 *
 *  Сменивший банк поставщик и подменённые в письме реквизиты выглядят одинаково, поэтому
 *  не выбираем за человека — показываем оба счёта и просим сверить с поставщиком.
 */
export function bankAccountMismatch(
  recognized: Record<string, string> | null | undefined,
  card: Record<string, string> | null | undefined,
): { invoice: string; card: string } | null {
  const fromInvoice = (recognized?.bankAcnt ?? "").trim();
  const fromCard = (card?.bankAcnt ?? "").trim();
  if (!fromInvoice || !fromCard || fromInvoice === fromCard) return null;
  return { invoice: fromInvoice, card: fromCard };
}

function sameValues(left: RequisiteValues, right: Partial<RequisiteValues>): boolean {
  return REQUISITE_KEYS.every((key) => (left[key] ?? "") === ((right[key] ?? "") as string));
}

/** Форма реквизитов окна разбора: предзаполнение, источники, реакция на смену контрагента. */
export function useRequisitesForm(intake: PaymentIntake, counterpartyId: string | null) {
  const cardQuery = useQuery({
    queryKey: ["payment-page", "counterparty-requisites", counterpartyId],
    queryFn: () => getCounterpartyRequisites(counterpartyId as string),
    enabled: Boolean(counterpartyId),
    staleTime: 30_000,
  });

  const cardRequisites = useMemo(
    () => cardQuery.data?.requisites ?? null,
    [cardQuery.data?.requisites],
  );

  const prefill = useMemo(
    () =>
      buildPrefill({
        reviewed: intake.reviewed_requisites,
        recognized: intake.requisites,
        card: cardRequisites,
        fallbackName: intake.recipient_name,
        fallbackInn: intake.inn,
      }),
    [intake.reviewed_requisites, intake.requisites, intake.recipient_name, intake.inn, cardRequisites],
  );

  const [values, setValues] = useState<RequisiteValues>(prefill.values);
  const [sources, setSources] = useState<RequisiteSources>(prefill.sources);
  // Тронутое руками не перетираем: человек мог поправить поле до того, как доехала карточка
  // или пока переключал контрагента.
  const touched = useRef<Set<RequisiteKey>>(new Set());

  useEffect(() => {
    setValues((prev) => {
      const next = { ...prev };
      REQUISITE_KEYS.forEach((key) => {
        if (!touched.current.has(key)) next[key] = prefill.values[key];
      });
      return next;
    });
    setSources((prev) => {
      const next = { ...prev };
      REQUISITE_KEYS.forEach((key) => {
        if (!touched.current.has(key)) next[key] = prefill.sources[key];
      });
      return next;
    });
  }, [prefill]);

  const setValue = useCallback((key: RequisiteKey, value: string) => {
    touched.current.add(key);
    setValues((prev) => ({ ...prev, [key]: value }));
    setSources((prev) => ({ ...prev, [key]: "manual" }));
  }, []);

  /** Подстановка найденного в истории кандидата (кнопка «Найти реквизиты в истории»). */
  const applyCandidate = useCallback((candidate: Record<string, string>) => {
    const picked = clean(candidate);
    setValues((prev) => {
      const next = { ...prev };
      REQUISITE_KEYS.forEach((key) => {
        if (picked[key]) next[key] = picked[key] as string;
      });
      return next;
    });
    setSources((prev) => {
      const next = { ...prev };
      REQUISITE_KEYS.forEach((key) => {
        if (picked[key]) {
          next[key] = "history";
          touched.current.add(key);
        }
      });
      return next;
    });
  }, []);

  return {
    values,
    sources,
    setValue,
    applyCandidate,
    cardRequisites,
    cardLoading: cardQuery.isLoading,
    /** Есть ли что переносить в карточку: совпадающие значения переносить незачем. */
    differsFromCard: !cardRequisites || !sameValues(values, clean(cardRequisites)),
    mismatch: bankAccountMismatch(intake.requisites, cardRequisites),
  };
}
