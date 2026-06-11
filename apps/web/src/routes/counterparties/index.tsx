import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { PageHeader } from "@/components/ui-app/PageHeader";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePermissions } from "@/lib/permissions";

import { getInvoices } from "./api";
import { CounterpartyCard } from "./CounterpartyCard";
import {
  COUNTERPARTIES_TABS,
  MetricCard,
  counterpartiesTabPath,
  formatRub,
  isOverdue,
  type CounterpartiesTab,
} from "./shared";
import { DraftsTab } from "./tabs/drafts";
import { InboxTab } from "./tabs/inbox";
import { RegistryTab } from "./tabs/registry";

type Props = {
  activeTab: CounterpartiesTab;
  onNavigate: (path: string) => void;
};

export function CounterpartiesRoute({ activeTab, onNavigate }: Props) {
  const permissions = usePermissions();
  const canOperate = permissions.canPerformAction("counterparties.operate");
  const canAdmin = permissions.canPerformAction("counterparties.admin");
  const [openId, setOpenId] = useState<string | null>(null);

  const dashboardQuery = useQuery({
    queryKey: ["cp", "invoices", "dashboard"],
    queryFn: () => getInvoices({ status: "unpaid,partially_paid" }),
  });
  const invoices = dashboardQuery.data ?? [];
  const toPay = invoices.reduce((sum, item) => sum + item.remaining, 0);
  const overdue = invoices
    .filter((item) => isOverdue(item.due_date, item.payment_status))
    .reduce((sum, item) => sum + item.remaining, 0);
  const inBank = invoices
    .filter((item) => item.draft_id)
    .reduce((sum, item) => sum + item.remaining, 0);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Контрагенты"
        description="Счета к оплате, отправка платежей в банк и реестр поставщиков."
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricCard label="К оплате" value={formatRub(toPay)} />
        <MetricCard
          label="Просрочено"
          value={formatRub(overdue)}
          accent={overdue > 0 ? "danger" : undefined}
        />
        <MetricCard label="Отправлено в банк" value={formatRub(inBank)} accent="info" />
      </div>

      <Tabs
        value={activeTab}
        onValueChange={(value) => onNavigate(counterpartiesTabPath(value as CounterpartiesTab))}
      >
        <TabsList className="h-auto flex-wrap justify-start">
          {COUNTERPARTIES_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {activeTab === "inbox" ? (
        <InboxTab canOperate={canOperate} onOpenCounterparty={setOpenId} />
      ) : null}
      {activeTab === "drafts" ? (
        <DraftsTab canOperate={canOperate} onOpenCounterparty={setOpenId} />
      ) : null}
      {activeTab === "registry" ? (
        <RegistryTab
          canOperate={canOperate}
          canAdmin={canAdmin}
          onOpenCounterparty={setOpenId}
        />
      ) : null}

      <CounterpartyCard
        counterpartyId={openId}
        canOperate={canOperate}
        canAdmin={canAdmin}
        onClose={() => setOpenId(null)}
      />
    </div>
  );
}
