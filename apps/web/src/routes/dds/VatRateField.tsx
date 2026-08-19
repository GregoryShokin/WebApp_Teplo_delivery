import { cn } from "@/lib/utils";

/**
 * НДС платежа в окне «Новый платёж».
 *
 * Назначение платежа обязано называть налог («в т.ч. НДС 22% — 1 439,90») или прямо говорить
 * «Без НДС»: это читают банк и налоговая. У платёжки по СЧЁТУ налог берётся с накладной, а
 * здесь счёта нет — ставку называет человек, и по ней налог ВЫДЕЛЯЕТСЯ из итога платежа.
 * Выделяется, а не начисляется сверху: сумму платежа согласовали с получателем, менять её
 * из-за галки нельзя.
 *
 * Поле показывает готовую строку назначения, а не только цифры: сверять надо результат,
 * который увидит банк, — тем же приёмом, что и разбор счёта (``payment-page/VatFields``).
 */

/** Ставки, из которых даём выбрать. Ровно те же, что принимает бэк (``app/services/vat.py``). */
export const VAT_RATES = ["22", "20", "10", "7", "5"] as const;

/** Налог «в том числе» из итоговой суммы: сумма × ставка / (100 + ставка). */
export function vatAmountFor(total: number, rate: string): number {
  const percent = Number(rate);
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(percent) || percent <= 0) return 0;
  return Math.round(((total * percent) / (100 + percent)) * 100) / 100;
}

/** Строка НДС ровно в том виде, в каком её соберёт бэк (``counterparty_payments._vat_suffix``). */
export function vatSuffixForRate(total: number, rate: string): string {
  const amount = vatAmountFor(total, rate);
  if (amount <= 0) return "Без НДС.";
  return `В т.ч. НДС: ${rate}% - ${amount.toFixed(2).replace(".", ",")} руб.`;
}

export function VatRateField({
  total,
  value,
  onChange,
  hint,
}: {
  /** Итоговая сумма платежа — из неё выделяется налог. */
  total: number;
  /** Ставка голыми цифрами («22») или «» — платёж без НДС. */
  value: string;
  onChange: (rate: string) => void;
  /** Чем закончится назначение до НДС-хвоста — чтобы человек видел строку целиком. */
  hint?: string;
}) {
  const suffix = vatSuffixForRate(total, value);
  // Ставка выбрана, а суммы ещё нет: налог посчитать не из чего. Молчать нельзя — человек
  // уже сказал «с НДС», и пустая строка «Без НДС.» под ней выглядела бы как отказ.
  const awaitingAmount = value !== "" && total <= 0;
  const options: Array<{ key: string; label: string }> = [
    { key: "", label: "Без НДС" },
    ...VAT_RATES.map((rate) => ({ key: rate, label: `${rate}%` })),
  ];

  return (
    <div>
      <span className="text-sm font-medium">НДС</span>
      <div className="mt-1.5 inline-flex items-center gap-0.5 rounded-md bg-muted p-0.5">
        {options.map((option) => (
          <button
            key={option.key || "none"}
            type="button"
            aria-pressed={value === option.key}
            onClick={() => onChange(option.key)}
            className={cn(
              "rounded px-3 py-1 text-sm transition-colors",
              value === option.key
                ? "border bg-background font-medium shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        {awaitingAmount ? (
          "Налог посчитается, как только появится сумма платежа."
        ) : (
          <>
            В назначение платежа уйдёт:{" "}
            {hint ? <span className="text-muted-foreground">{hint} </span> : null}
            <span className="font-medium text-foreground">{suffix}</span>
          </>
        )}
      </p>
    </div>
  );
}
