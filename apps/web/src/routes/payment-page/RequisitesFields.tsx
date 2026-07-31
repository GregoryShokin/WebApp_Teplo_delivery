import { useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { usePermissions } from "@/lib/permissions";
import { RequisitesHistoryButton } from "@/routes/counterparties/RequisitesHistoryButton";

import {
  REQUISITE_SOURCE_LABELS,
  type RequisiteKey,
  type RequisiteSource,
  type RequisiteSources,
  type RequisiteValues,
} from "./requisites";

function SourceHint({ source }: { source: RequisiteSource }) {
  if (!source) return null;
  return (
    <span className="text-[11px] text-muted-foreground">{REQUISITE_SOURCE_LABELS[source]}</span>
  );
}

function RequisiteField({
  label,
  value,
  source,
  onChange,
  missing,
}: {
  label: string;
  value: string;
  source: RequisiteSource;
  onChange: (v: string) => void;
  missing?: boolean;
}) {
  const id = useId();
  return (
    <div className="grid gap-1">
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={id} className="text-xs text-muted-foreground">
          {label}
        </Label>
        <SourceHint source={source} />
      </div>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={missing ? "border-amber-400" : undefined}
      />
    </div>
  );
}

/** Блок реквизитов получателя — общий для окон «Разбор счёта» и «Отправить в банк».
 *
 *  Поля предзаполняются по цепочке «правки оператора → счёт → карточка контрагента»
 *  (см. buildPrefill), и под каждым подписано, откуда значение взялось: платит бизнес
 *  по этим цифрам, поэтому подменять источник молча нельзя.
 */
export function RequisitesFields({
  values,
  sources,
  onChange,
  mismatch,
  onPickFromHistory,
  searchQuery,
  counterpartyId,
  highlightMissing = false,
  loading = false,
}: {
  values: RequisiteValues;
  sources: RequisiteSources;
  onChange: (key: RequisiteKey, value: string) => void;
  mismatch: { invoice: string; card: string } | null;
  onPickFromHistory: (requisites: Record<string, string>) => void;
  searchQuery: string;
  counterpartyId?: string | null;
  /** Окно отправки в банк подсвечивает недостающие для платёжки поля. */
  highlightMissing?: boolean;
  loading?: boolean;
}) {
  const { hasAnyPermission } = usePermissions();
  // Поиск по истории платежей — админское право контрагентов; оператору без него
  // кнопку не показываем, чтобы не ловить 403 по клику.
  const canSearchHistory = hasAnyPermission(["counterparties.admin", "finance.counterparties.edit"]);

  return (
    <div className="grid gap-2 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium uppercase text-muted-foreground">Реквизиты</div>
        {loading ? <span className="text-[11px] text-muted-foreground">карточка…</span> : null}
      </div>

      {mismatch ? (
        <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">
          В счёте расчётный счёт {mismatch.invoice}, а в карточке контрагента {mismatch.card}.
          Так выглядит и смена банка поставщиком, и подмена реквизитов в письме — сверьте с
          поставщиком по известному вам контакту, прежде чем платить.
        </p>
      ) : null}

      <RequisiteField
        label="Получатель"
        value={values.recipientName}
        source={sources.recipientName}
        onChange={(v) => onChange("recipientName", v)}
        missing={highlightMissing && !values.recipientName.trim()}
      />
      <div className="grid gap-2 sm:grid-cols-2">
        <RequisiteField
          label="ИНН"
          value={values.inn}
          source={sources.inn}
          onChange={(v) => onChange("inn", v)}
          missing={highlightMissing && !values.inn.trim()}
        />
        <RequisiteField
          label="КПП"
          value={values.kpp}
          source={sources.kpp}
          onChange={(v) => onChange("kpp", v)}
        />
      </div>
      <RequisiteField
        label="Расчётный счёт"
        value={values.bankAcnt}
        source={sources.bankAcnt}
        onChange={(v) => onChange("bankAcnt", v)}
        missing={highlightMissing && !values.bankAcnt.trim()}
      />
      <div className="grid gap-2 sm:grid-cols-2">
        <RequisiteField
          label="БИК"
          value={values.bankBik}
          source={sources.bankBik}
          onChange={(v) => onChange("bankBik", v)}
          missing={highlightMissing && !values.bankBik.trim()}
        />
        <RequisiteField
          label="Корр. счёт"
          value={values.recipientCorrAccountNumber}
          source={sources.recipientCorrAccountNumber}
          onChange={(v) => onChange("recipientCorrAccountNumber", v)}
          missing={highlightMissing && !values.recipientCorrAccountNumber.trim()}
        />
      </div>

      {canSearchHistory ? (
        <div className="pt-1">
          <RequisitesHistoryButton
            query={searchQuery}
            onPick={onPickFromHistory}
            ignoreCounterpartyId={counterpartyId ?? undefined}
            label="Найти реквизиты в истории"
          />
        </div>
      ) : null}
    </div>
  );
}
