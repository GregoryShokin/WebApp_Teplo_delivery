import { useEffect, useState } from "react";

import { PayrollRunsRoute } from "@/routes/payroll/runs";

type PayrollActiveTab =
  | "runs"
  | "admin"
  | "fund"
  | "adjustments"
  | "audits"
  | "accruals"
  | "personal";

type PayrollRouteProps = {
  activeTab: PayrollActiveTab;
  invalidPath?: boolean;
  onNavigate: (path: string) => void;
  useStoredTab?: boolean;
};

const PAYROLL_ACTIVE_TAB_STORAGE_KEY = "payroll.activeTab";

export function PayrollRoute({
  activeTab,
  invalidPath = false,
  onNavigate,
  useStoredTab = false,
}: PayrollRouteProps) {
  const [isResolvingStoredTab, setIsResolvingStoredTab] = useState(useStoredTab);

  useEffect(() => {
    if (!useStoredTab) {
      setIsResolvingStoredTab(false);
      return;
    }
    const storedTab = readStoredPayrollTab();
    if (storedTab && storedTab !== activeTab) {
      onNavigate(payrollTabPath(storedTab));
      return;
    }
    setIsResolvingStoredTab(false);
  }, [activeTab, onNavigate, useStoredTab]);

  useEffect(() => {
    if (isResolvingStoredTab) {
      return;
    }
    window.localStorage.setItem(PAYROLL_ACTIVE_TAB_STORAGE_KEY, activeTab);
  }, [activeTab, isResolvingStoredTab]);

  if (isResolvingStoredTab) {
    return null;
  }

  return (
    <PayrollRunsRoute activeTab={activeTab} invalidPath={invalidPath} onNavigate={onNavigate} />
  );
}

function payrollTabPath(tab: PayrollActiveTab) {
  if (tab === "fund") {
    return "/payroll/fund";
  }
  if (tab === "audits") {
    return "/payroll/audits";
  }
  if (tab === "accruals") {
    return "/payroll/accruals";
  }
  if (tab === "personal") {
    return "/payroll/personal";
  }
  if (tab === "admin") {
    return "/payroll/admin";
  }
  return tab === "adjustments" ? "/payroll/adjustments" : "/payroll";
}

function readStoredPayrollTab() {
  const value = window.localStorage.getItem(PAYROLL_ACTIVE_TAB_STORAGE_KEY);
  return isPayrollTab(value) ? value : null;
}

function isPayrollTab(value: unknown): value is PayrollActiveTab {
  return (
    value === "runs" ||
    value === "admin" ||
    value === "fund" ||
    value === "adjustments" ||
    value === "audits" ||
    value === "accruals" ||
    value === "personal"
  );
}
