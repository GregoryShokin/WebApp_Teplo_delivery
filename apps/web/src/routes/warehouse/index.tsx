import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/ui-app/PageHeader";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

import { getInvoices } from "../counterparties/api";
import { CounterpartyCard } from "../counterparties/CounterpartyCard";
import { MetricCard, formatRub, isOverdue } from "../counterparties/shared";
import { DraftsTab } from "../counterparties/tabs/drafts";
import { InboxTab } from "../counterparties/tabs/inbox";
import { RegistryTab } from "../counterparties/tabs/registry";
import { syncProducts } from "./api";

export type WarehouseTab = "normal" | "barter" | "drafts" | "registry";

const WAREHOUSE_TABS: Array<{ value: WarehouseTab; label: string; path: string }> = [
  { value: "normal", label: "Обычные накладные", path: "/warehouse/invoices" },
  { value: "barter", label: "Бартер", path: "/warehouse/barter" },
  { value: "drafts", label: "Черновики и мэчинг", path: "/warehouse/drafts" },
  { value: "registry", label: "Контрагенты", path: "/warehouse/registry" },
];

export function warehouseTabPath(tab: WarehouseTab): string {
  return WAREHOUSE_TABS.find((item) => item.value === tab)?.path ?? "/warehouse/invoices";
}

type Props = {
  activeTab: WarehouseTab;
  onNavigate: (path: string) => void;
};

export function WarehouseInvoicesRoute({ activeTab, onNavigate }: Props) {
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

  const syncMutation = useMutation({
    mutationFn: syncProducts,
    onSuccess: (r) => toast.success(`Номенклатура обновлена: ${r.goods_count} товаров (GOODS)`),
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось синхронизировать номенклатуру")),
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title="Накладные"
        description="Накладные из iiko и созданные вручную: оплата, отправка в банк, бартер, персонал-разнесение и реестр контрагентов."
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

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Tabs
          value={activeTab}
          onValueChange={(value) => onNavigate(warehouseTabPath(value as WarehouseTab))}
        >
          <TabsList className="h-auto flex-wrap justify-start">
            {WAREHOUSE_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        {canOperate ? (
          <Button
            variant="outline"
            size="sm"
            disabled={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
          >
            {syncMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <RefreshCw size={15} aria-hidden="true" />
            )}
            Синхронизировать номенклатуру
          </Button>
        ) : null}
      </div>

      {activeTab === "normal" ? (
        <InboxTab kind="normal" splitPay canOperate={canOperate} onOpenCounterparty={setOpenId} />
      ) : null}
      {activeTab === "barter" ? (
        <InboxTab kind="barter" canOperate={canOperate} onOpenCounterparty={setOpenId} />
      ) : null}
      {activeTab === "drafts" ? (
        <DraftsTab canOperate={canOperate} onOpenCounterparty={setOpenId} />
      ) : null}
      {activeTab === "registry" ? (
        <RegistryTab canOperate={canOperate} canAdmin={canAdmin} onOpenCounterparty={setOpenId} />
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
