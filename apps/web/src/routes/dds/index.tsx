import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { getDdsOwnerReview } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { ArticlesTab } from "@/routes/dds/tabs/articles";
import { CounterpartiesTab } from "@/routes/dds/tabs/counterparties";
import { CredentialsTab } from "@/routes/dds/tabs/credentials";
import { LedgerTab } from "@/routes/dds/tabs/ledger";
import { OperationsTab } from "@/routes/dds/tabs/operations";
import { OwnerReviewTab } from "@/routes/dds/tabs/owner-review";
import { RulesTab } from "@/routes/dds/tabs/rules";
import { TodayTab } from "@/routes/dds/tabs/today";
import {
  DDS_ACTIVE_TAB_STORAGE_KEY,
  DDS_TABS,
  ddsTabPath,
  isDdsTab,
  type DdsActiveTab,
} from "@/routes/dds/shared";

type DdsRouteProps = {
  activeTab: DdsActiveTab;
  invalidPath?: boolean;
  onNavigate: (path: string) => void;
  useStoredTab?: boolean;
};

export function DdsRoute({
  activeTab,
  invalidPath = false,
  onNavigate,
  useStoredTab = false,
}: DdsRouteProps) {
  const permissions = usePermissions();
  const visibleTabs = DDS_TABS.filter((tab) => canOpenDdsTab(tab.value, permissions));
  const [isResolvingStoredTab, setIsResolvingStoredTab] = useState(useStoredTab);
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", "badge"],
    queryFn: () => getDdsOwnerReview({ limit: 1, offset: 0 }),
    enabled: visibleTabs.some((tab) => tab.value === "owner-review"),
  });

  useEffect(() => {
    if (!useStoredTab) {
      setIsResolvingStoredTab(false);
      return;
    }
    const storedTab = readStoredDdsTab();
    if (storedTab && storedTab !== activeTab) {
      onNavigate(ddsTabPath(storedTab));
      return;
    }
    setIsResolvingStoredTab(false);
  }, [activeTab, onNavigate, useStoredTab]);

  useEffect(() => {
    if (isResolvingStoredTab) {
      return;
    }
    window.localStorage.setItem(DDS_ACTIVE_TAB_STORAGE_KEY, activeTab);
  }, [activeTab, isResolvingStoredTab]);

  useEffect(() => {
    if (
      !isResolvingStoredTab &&
      visibleTabs.length > 0 &&
      !visibleTabs.some((tab) => tab.value === activeTab)
    ) {
      onNavigate(ddsTabPath(visibleTabs[0].value));
    }
  }, [activeTab, isResolvingStoredTab, onNavigate, visibleTabs]);

  if (isResolvingStoredTab) {
    return null;
  }

  function handleTabChange(value: string) {
    if (!isDdsTab(value)) {
      return;
    }
    window.localStorage.setItem(DDS_ACTIVE_TAB_STORAGE_KEY, value);
    onNavigate(ddsTabPath(value));
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="ДДС"
        description={
          invalidPath
            ? "Неизвестная вкладка. Открыт основной раздел движения денежных средств."
            : "Деньги, банковские операции, журнал и правила классификации."
        }
      />

      <Tabs value={activeTab} onValueChange={handleTabChange} className="space-y-5">
        <TabsList className="h-auto flex-wrap justify-start">
          {visibleTabs.map((tab) => (
            <TabsTrigger className="gap-2" key={tab.value} value={tab.value}>
              {tab.label}
              {tab.value === "owner-review" && ownerReviewQuery.data?.total ? (
                <Badge className="px-1.5 py-0 text-[11px]" variant="secondary">
                  {ownerReviewQuery.data.total}
                </Badge>
              ) : null}
            </TabsTrigger>
          ))}
        </TabsList>
        {visibleTabs.length === 0 ? (
          <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
            Нет доступных вкладок финансов для текущего профиля.
          </div>
        ) : (
          renderTab(activeTab, onNavigate, permissions)
        )}
      </Tabs>
    </div>
  );
}

function renderTab(
  activeTab: DdsActiveTab,
  onNavigate: (path: string) => void,
  permissions: ReturnType<typeof usePermissions>,
) {
  if (activeTab === "operations") {
    return <OperationsTab />;
  }
  if (activeTab === "ledger") {
    return <LedgerTab />;
  }
  if (activeTab === "owner-review") {
    return <OwnerReviewTab canClassify={permissions.canPerformAction("finance.cashflow.classify")} />;
  }
  if (activeTab === "counterparties") {
    return <CounterpartiesTab canEdit={permissions.canPerformAction("finance.counterparties.edit")} />;
  }
  if (activeTab === "articles") {
    return <ArticlesTab canEdit={permissions.canPerformAction("finance.cashflow.classify")} />;
  }
  if (activeTab === "rules") {
    return <RulesTab canManage={permissions.canPerformAction("finance.rules.manage")} />;
  }
  if (activeTab === "credentials") {
    return <CredentialsTab canManage={permissions.canPerformAction("finance.cashflow.sync")} />;
  }
  return <TodayTab onNavigate={onNavigate} />;
}

function canOpenDdsTab(tab: DdsActiveTab, permissions: ReturnType<typeof usePermissions>) {
  if (tab === "today") {
    return permissions.hasAnyPermission([
      "finance.wallets.read",
      "finance.store_cash.read",
      "finance.cashflow.read",
    ]);
  }
  if (tab === "operations" || tab === "ledger" || tab === "articles") {
    return permissions.hasPermission("finance.cashflow.read");
  }
  if (tab === "owner-review") {
    return permissions.hasAnyPermission([
      "finance.owner_review.prepare",
      "finance.cashflow.classify",
    ]);
  }
  if (tab === "counterparties") {
    return permissions.hasPermission("finance.counterparties.read");
  }
  if (tab === "rules") {
    return permissions.canPerformAction("finance.rules.manage");
  }
  return permissions.canPerformAction("finance.cashflow.sync");
}

function readStoredDdsTab() {
  const value = window.localStorage.getItem(DDS_ACTIVE_TAB_STORAGE_KEY);
  return isDdsTab(value) ? value : null;
}
