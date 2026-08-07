import { useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * НДС счёта — единственное поле разбора, которое уходит НЕ в учёт, а в текст назначения
 * платежа: банк и налоговая читают именно его. Поэтому окно показывает не только цифры, но и
 * готовую строку, которая встанет в платёжку, — сверять надо результат, а не исходники.
 *
 * Три состояния распознавания различаются намеренно. «В счёте написано „без НДС“» и «налог не
 * распознан» дают в платёжке одинаковое «Без НДС.», но человеку это разные новости: во втором
 * случае цифру нужно перебить с бумаги, иначе банк получит утверждение, которого никто не
 * проверял.
 */
export type VatValue = { amount: string; rate: string };

/** Строка НДС ровно в том виде, в каком её соберёт бэк (``_vat_suffix``). */
export function vatSuffixPreview({ amount, rate }: VatValue): string {
  const parsed = Number((amount || "").replace(",", ".").replace(/\s/g, ""));
  if (!amount.trim() || !Number.isFinite(parsed) || parsed <= 0) return "Без НДС.";
  const money = parsed.toFixed(2).replace(".", ",");
  const cleanRate = rate.replace(/\D/g, "");
  return `В т.ч. НДС: ${cleanRate ? `${cleanRate}% - ` : ""}${money} руб.`;
}

function SourceBadge({ mode }: { mode: string }) {
  if (mode === "included") {
    return (
      <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700">
        из счёта
      </span>
    );
  }
  if (mode === "none") {
    return (
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
        в счёте «без НДС»
      </span>
    );
  }
  return (
    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-800">
      не распознан
    </span>
  );
}

export function VatFields({
  mode,
  value,
  onChange,
  invoiceAmount,
}: {
  /** vat_mode из распознавания: 'included' | 'none' | '' (не распознан). */
  mode: string;
  value: VatValue;
  onChange: (next: VatValue) => void;
  /** Сумма счёта — для проверки «налог не больше платежа» (её же делает бэк). */
  invoiceAmount?: string | null;
}) {
  // Метки связаны с полями явно (htmlFor/id): иначе к ним не добраться ни скринридеру,
  // ни тесту — в соседнем блоке реквизитов сделано так же.
  const amountId = useId();
  const rateId = useId();
  const parsed = Number((value.amount || "").replace(",", ".").replace(/\s/g, ""));
  const total = Number((invoiceAmount || "").replace(",", ".").replace(/\s/g, ""));
  const filled = value.amount.trim() !== "" && Number.isFinite(parsed) && parsed > 0;
  const tooBig = filled && Number.isFinite(total) && total > 0 && parsed >= total;
  // Предупреждаем только там, где молчание обманет: налог не распознан, а платёжка при этом
  // уверенно скажет «Без НДС.». Явное «без НДС» из счёта — не повод дёргать человека.
  const silentlyClaimsNoVat = !filled && mode !== "none";

  return (
    <div className="grid gap-2 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium uppercase text-muted-foreground">НДС в платёжке</div>
        <SourceBadge mode={mode} />
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="grid gap-1">
          <Label htmlFor={amountId} className="text-xs text-muted-foreground">
            Сумма НДС
          </Label>
          <Input
            id={amountId}
            value={value.amount}
            placeholder="нет налога"
            onChange={(e) => onChange({ ...value, amount: e.target.value })}
            className={silentlyClaimsNoVat || tooBig ? "border-amber-400" : undefined}
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor={rateId} className="text-xs text-muted-foreground">
            Ставка, %
          </Label>
          <Input
            id={rateId}
            value={value.rate}
            placeholder="если указана"
            onChange={(e) => onChange({ ...value, rate: e.target.value })}
          />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        В назначение платежа уйдёт:{" "}
        <span className="font-medium text-foreground">{vatSuffixPreview(value)}</span>
      </p>
      {tooBig ? (
        <p className="text-xs text-amber-600">
          НДС не может быть больше суммы счёта — проверьте, не попал ли в поле итог.
        </p>
      ) : silentlyClaimsNoVat ? (
        <p className="text-xs text-amber-600">
          Налог из счёта не распознан. Если он там есть — впишите сумму с бумаги: иначе банк
          получит платёжку с «Без НДС.».
        </p>
      ) : null}
    </div>
  );
}
