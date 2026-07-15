import { PageHeader } from "@/components/ui-app/PageHeader";
import { CounterpartyRegistryModule } from "@/routes/counterparties/CounterpartyRegistryModule";

/** «Финансы → Контрагенты»: единственная страница реестра. Накладные, ДДС и платежи
 *  берут контрагентов отсюда и своих форм создания не держат. */
export function FinanceCounterpartiesRoute() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Контрагенты"
        description="Единый реестр: карточки, реквизиты, статьи ДДС и периоды оказания услуг. Накладные, ДДС и платежи берут контрагентов отсюда."
      />
      <CounterpartyRegistryModule />
    </div>
  );
}
