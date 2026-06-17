import { useEffect, useState } from "react";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { usePermissions } from "@/lib/permissions";
import { WarehouseInvoicesRoute } from "@/routes/warehouse";
import {
  KASSA_ACTIVE_TAB_STORAGE_KEY,
  KASSA_TABS,
  isKassaTab,
  kassaTabPath,
  type KassaActiveTab,
} from "@/routes/kassa/shared";
import { ReceiptsTab } from "@/routes/kassa/tabs/receipts";
import { ShiftCloseTab } from "@/routes/kassa/tabs/shift-close";

type KassaRouteProps = {
  activeTab: KassaActiveTab;
  invalidPath?: boolean;
  onNavigate: (path: string) => void;
  useStoredTab?: boolean;
};

export function KassaRoute({
  activeTab,
  invalidPath = false,
  onNavigate,
  useStoredTab = false,
}: KassaRouteProps) {
  const permissions = usePermissions();
  const visibleTabs = KASSA_TABS.filter((tab) => canOpenKassaTab(tab.value, permissions));
  const [isResolvingStoredTab, setIsResolvingStoredTab] = useState(useStoredTab);

  useEffect(() => {
    if (!useStoredTab) {
      setIsResolvingStoredTab(false);
      return;
    }
    const stored = readStoredKassaTab();
    if (stored && stored !== activeTab) {
      onNavigate(kassaTabPath(stored));
      return;
    }
    setIsResolvingStoredTab(false);
  }, [activeTab, onNavigate, useStoredTab]);

  useEffect(() => {
    if (isResolvingStoredTab) {
      return;
    }
    window.localStorage.setItem(KASSA_ACTIVE_TAB_STORAGE_KEY, activeTab);
  }, [activeTab, isResolvingStoredTab]);

  useEffect(() => {
    if (
      !isResolvingStoredTab &&
      visibleTabs.length > 0 &&
      !visibleTabs.some((tab) => tab.value === activeTab)
    ) {
      onNavigate(kassaTabPath(visibleTabs[0].value));
    }
  }, [activeTab, isResolvingStoredTab, onNavigate, visibleTabs]);

  if (isResolvingStoredTab) {
    return null;
  }

  function handleTabChange(value: string) {
    if (!isKassaTab(value)) {
      return;
    }
    window.localStorage.setItem(KASSA_ACTIVE_TAB_STORAGE_KEY, value);
    onNavigate(kassaTabPath(value));
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Касса"
        description={
          invalidPath
            ? "Неизвестная вкладка. Открыт основной раздел кассы."
            : "Закрытие смены iiko, накладные и чеки (оплаты картой)."
        }
      />

      <Tabs value={activeTab} onValueChange={handleTabChange} className="space-y-5">
        <TabsList className="h-auto flex-wrap justify-start">
          {visibleTabs.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {visibleTabs.length === 0 ? (
          <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
            Нет доступных вкладок кассы для текущего профиля.
          </div>
        ) : (
          renderTab(activeTab, onNavigate, permissions)
        )}
      </Tabs>
    </div>
  );
}

function renderTab(
  activeTab: KassaActiveTab,
  onNavigate: (path: string) => void,
  permissions: ReturnType<typeof usePermissions>,
) {
  if (activeTab === "invoices") {
    return <WarehouseInvoicesRoute activeTab="normal" onNavigate={onNavigate} embedded />;
  }
  if (activeTab === "receipts") {
    return <ReceiptsTab canCreate={permissions.canPerformAction("kassa.cheques.create")} />;
  }
  return (
    <ShiftCloseTab
      canSync={permissions.canPerformAction("kassa.shifts.sync")}
      canPost={permissions.canPerformAction("kassa.shifts.post")}
    />
  );
}

function canOpenKassaTab(tab: KassaActiveTab, permissions: ReturnType<typeof usePermissions>) {
  if (tab === "invoices") {
    return permissions.canOpenSection("kassa.invoices");
  }
  if (tab === "receipts") {
    return permissions.canOpenSection("kassa.receipts");
  }
  return permissions.canOpenSection("kassa.shift-close");
}

function readStoredKassaTab() {
  const value = window.localStorage.getItem(KASSA_ACTIVE_TAB_STORAGE_KEY);
  return isKassaTab(value) ? value : null;
}
