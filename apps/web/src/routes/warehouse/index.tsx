import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/ui-app/PageHeader";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

import { getInvoices, syncInvoices } from "../counterparties/api";
import { CounterpartyCard } from "../counterparties/CounterpartyCard";
import { CounterpartyRegistryModule } from "../counterparties/CounterpartyRegistryModule";
import { MetricCard, formatRub, isOverdue } from "../counterparties/shared";
import { InboxTab } from "../counterparties/tabs/inbox";
import { syncProducts } from "./api";

export type WarehouseTab = "normal" | "barter" | "registry";

const WAREHOUSE_TABS: Array<{ value: WarehouseTab; label: string; path: string }> = [
  { value: "normal", label: "Обычные накладные", path: "/warehouse/invoices" },
  { value: "barter", label: "Бартер", path: "/warehouse/barter" },
  { value: "registry", label: "Контрагенты", path: "/warehouse/registry" },
];

export function warehouseTabPath(tab: WarehouseTab): string {
  return WAREHOUSE_TABS.find((item) => item.value === tab)?.path ?? "/warehouse/invoices";
}

type Props = {
  activeTab: WarehouseTab;
  onNavigate: (path: string) => void;
  // embedded = встроено в другую страницу (например «Касса»): без своего заголовка и
  // без собственной полосы вкладок — рендерим только обычные накладные.
  embedded?: boolean;
};

export function WarehouseInvoicesRoute({ activeTab, onNavigate, embedded = false }: Props) {
  const permissions = usePermissions();
  const canOperate = permissions.canPerformAction("counterparties.operate");
  const canAdmin = permissions.canPerformAction("counterparties.admin");
  const canCreateNormal = permissions.canPerformAction("invoices.normal.create");
  const canPayNormal = permissions.canPerformAction("invoices.normal.pay");
  const canCreateBarter = permissions.canPerformAction("invoices.barter.create");
  const canPayBarter = permissions.canPerformAction("invoices.barter.pay");
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

  const queryClient = useQueryClient();
  const syncMutation = useMutation({
    mutationFn: syncProducts,
    onSuccess: (r) => toast.success(`Номенклатура обновлена: ${r.goods_count} товаров (GOODS)`),
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось синхронизировать номенклатуру")),
  });
  const invoiceSyncMutation = useMutation({
    mutationFn: syncInvoices,
    onSuccess: async (r) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(
        `Синхронизация iiko: новых ${r.invoices_created ?? 0}, обновлено ${r.invoices_updated ?? 0}`,
      );
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Синхронизация с iiko не удалась")),
  });

  const effectiveTab: WarehouseTab = embedded ? "normal" : activeTab;

  return (
    <div className="space-y-6">
      {embedded ? null : (
        <PageHeader
          title="Накладные"
          description="Накладные из iiko и созданные вручную: оплата, отправка в банк, бартер, персонал-разнесение и реестр контрагентов."
        />
      )}

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
        {embedded ? (
          <span />
        ) : (
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
        )}
        {canOperate ? (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={invoiceSyncMutation.isPending}
              onClick={() => invoiceSyncMutation.mutate()}
            >
              {invoiceSyncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <RefreshCw size={15} aria-hidden="true" />
              )}
              Синхронизировать iiko
            </Button>
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
          </div>
        ) : null}
      </div>

      {effectiveTab === "normal" ? (
        <InboxTab
          kind="normal"
          splitPay
          canOperate={canOperate}
          canCreate={canCreateNormal}
          canPay={canPayNormal}
          onOpenCounterparty={setOpenId}
        />
      ) : null}
      {effectiveTab === "barter" ? (
        <InboxTab
          kind="barter"
          canOperate={canOperate}
          canCreate={canCreateBarter}
          canPay={canPayBarter}
          onOpenCounterparty={setOpenId}
        />
      ) : null}
      {effectiveTab === "registry" ? <CounterpartyRegistryModule /> : null}

      {effectiveTab === "registry" ? null : (
        <CounterpartyCard
          counterpartyId={openId}
          canOperate={canOperate}
          canAdmin={canAdmin}
          onClose={() => setOpenId(null)}
        />
      )}
    </div>
  );
}
