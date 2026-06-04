import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { getDdsOwnerReview } from "@/lib/api";
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
  const [isResolvingStoredTab, setIsResolvingStoredTab] = useState(useStoredTab);
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", "badge"],
    queryFn: () => getDdsOwnerReview({ limit: 1, offset: 0 }),
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
          {DDS_TABS.map((tab) => (
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
        {renderTab(activeTab, onNavigate)}
      </Tabs>
    </div>
  );
}

function renderTab(activeTab: DdsActiveTab, onNavigate: (path: string) => void) {
  if (activeTab === "operations") {
    return <OperationsTab />;
  }
  if (activeTab === "ledger") {
    return <LedgerTab />;
  }
  if (activeTab === "owner-review") {
    return <OwnerReviewTab />;
  }
  if (activeTab === "counterparties") {
    return <CounterpartiesTab />;
  }
  if (activeTab === "articles") {
    return <ArticlesTab />;
  }
  if (activeTab === "rules") {
    return <RulesTab />;
  }
  if (activeTab === "credentials") {
    return <CredentialsTab />;
  }
  return <TodayTab onNavigate={onNavigate} />;
}

function readStoredDdsTab() {
  const value = window.localStorage.getItem(DDS_ACTIVE_TAB_STORAGE_KEY);
  return isDdsTab(value) ? value : null;
}
