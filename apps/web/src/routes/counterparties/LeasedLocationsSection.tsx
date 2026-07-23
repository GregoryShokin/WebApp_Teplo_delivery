import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { getCounterpartyLeases } from "@/lib/api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

/**
 * Зеркало блока аренды из карточки помещения: что этот контрагент нам сдаёт.
 * Источник один — строки аренды помещения, поэтому смена арендодателя там сразу
 * отражается здесь, а редактировать условия можно только в карточке помещения.
 */
export function LeasedLocationsSection({ counterpartyId }: { counterpartyId: string }) {
  const leasesQuery = useQuery({
    queryKey: ["counterparty-leases", counterpartyId],
    queryFn: () => getCounterpartyLeases(counterpartyId),
  });

  const leases = leasesQuery.data ?? [];
  if (leases.length === 0) {
    return null;
  }

  const active = leases.filter((lease) => lease.is_active);
  const history = leases.filter((lease) => !lease.is_active);

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">Сдаёт нам в аренду</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Условия задаются в карточке помещения — «Настройки → Помещения».
        </p>
      </div>
      <div className="space-y-2">
        {active.map((lease) => (
          <div
            key={lease.id}
            className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm"
          >
            <span className="font-medium">{lease.location_name}</span>
            <Badge variant="secondary">{money.format(lease.monthly_amount)}/мес</Badge>
            <Badge variant="outline">
              {lease.documents_mode === "official" ? "с документами" : "без документов"}
            </Badge>
            <span className="text-xs text-muted-foreground">с {lease.started_on}</span>
            {lease.deposit_amount > 0 ? (
              <span className="text-xs text-muted-foreground">
                залог {money.format(lease.deposit_amount)}
              </span>
            ) : null}
          </div>
        ))}
        {history.map((lease) => (
          <div
            key={lease.id}
            className="flex flex-wrap items-center gap-2 px-3 text-xs text-muted-foreground"
          >
            <span>{lease.location_name}</span>
            <span>
              {lease.started_on} — {lease.ended_on}
            </span>
            <span>сдавал {money.format(lease.monthly_amount)}/мес</span>
          </div>
        ))}
      </div>
    </section>
  );
}
