import { useEffect, useState, type ReactNode } from "react";

import { AppLayout } from "@/components/layout/AppLayout";
import { EmptyModule } from "@/components/layout/EmptyModule";
import { Toaster } from "@/components/ui/sonner";
import { restoreSession } from "@/lib/api";
import { getAuthSnapshot } from "@/lib/auth";
import { DashboardPage } from "@/pages/DashboardPage";
import { CourierDepositsRoute } from "@/routes/couriers/deposits";
import { PayrollRoute } from "@/routes/payroll";
import { DdsRoute } from "@/routes/dds";
import { LoginRoute } from "@/routes/login";
import { AccessControlRoute } from "@/routes/access-control";
import { CourierEvaluationsRoute } from "@/routes/couriers/evaluations";
import { CourierScheduleRoute } from "@/routes/couriers/schedule";
import { CourierStatisticsRoute } from "@/routes/couriers/statistics";
import { PayrollAdminRunDetailRoute } from "@/routes/payroll/admin-run-detail";
import { PayrollConfigurationRoute } from "@/routes/payroll/configuration";
import { PayrollRunDetailRoute } from "@/routes/payroll/run-detail";
import { ScheduleRoute } from "@/routes/schedule";
import { SettingsRoute } from "@/routes/settings";
import { StaffRoute } from "@/routes/staff";

type Navigate = (path: string) => void;

type RouteContext = {
  navigate: Navigate;
  params: Record<string, string>;
};

type AppRoute = {
  path: string;
  layout?: boolean;
  render?: (context: RouteContext) => ReactNode;
  children?: AppRoute[];
};

type MatchedRoute = {
  element: ReactNode;
  layout: boolean;
};

const routes: AppRoute[] = [
  {
    path: "/login",
    layout: false,
    render: ({ navigate }) => <LoginRoute onNavigate={navigate} />,
  },
  {
    path: "/",
    render: ({ navigate }) => <DashboardPage onNavigate={navigate} />,
  },
  {
    path: "/staff",
    render: ({ navigate }) => <StaffRoute onNavigate={navigate} />,
  },
  {
    path: "/payroll",
    children: [
      {
        path: "",
        render: ({ navigate }) => (
          <PayrollRoute activeTab="runs" onNavigate={navigate} useStoredTab />
        ),
      },
      {
        path: "adjustments",
        render: ({ navigate }) => <PayrollRoute activeTab="adjustments" onNavigate={navigate} />,
      },
      {
        path: "fund",
        render: ({ navigate }) => <PayrollRoute activeTab="fund" onNavigate={navigate} />,
      },
      {
        path: "audits",
        render: ({ navigate }) => <PayrollRoute activeTab="audits" onNavigate={navigate} />,
      },
      {
        path: "accruals",
        render: ({ navigate }) => <PayrollRoute activeTab="accruals" onNavigate={navigate} />,
      },
      {
        path: "personal",
        render: ({ navigate }) => <PayrollRoute activeTab="personal" onNavigate={navigate} />,
      },
      {
        path: "advances",
        render: ({ navigate }) => <PayrollRoute activeTab="advances" onNavigate={navigate} />,
      },
      {
        path: "admin",
        render: ({ navigate }) => <PayrollRoute activeTab="admin" onNavigate={navigate} />,
      },
      {
        path: "admin/runs/:id",
        render: ({ navigate, params }) => (
          <PayrollAdminRunDetailRoute runId={params.id ?? ""} onNavigate={navigate} />
        ),
      },
      {
        path: "runs",
        render: ({ navigate }) => (
          <RedirectRoute
            onNavigate={navigate}
            storageKey="payroll.activeTab"
            storageValue="runs"
            to="/payroll"
          />
        ),
      },
      {
        path: "runs/:id",
        render: ({ navigate, params }) => (
          <PayrollRunDetailRoute runId={params.id ?? ""} onNavigate={navigate} />
        ),
      },
      {
        path: "daily-ledger",
        render: ({ navigate }) => (
          <RedirectRoute
            onNavigate={navigate}
            storageKey="schedule.activeTab"
            storageValue="shifts-ledger"
            to="/schedule/shifts-ledger"
          />
        ),
      },
      {
        path: "configuration",
        render: ({ navigate }) => <RedirectRoute onNavigate={navigate} to="/source-data" />,
      },
      {
        path: ":unknown",
        render: ({ navigate }) => (
          <PayrollRoute activeTab="runs" invalidPath onNavigate={navigate} />
        ),
      },
    ],
  },
  {
    path: "/payroll-adjustments",
    render: ({ navigate }) => (
      <RedirectRoute onNavigate={navigate} to={withCurrentSearch("/payroll/adjustments")} />
    ),
  },
  {
    path: "/shifts",
    children: [
      {
        path: "",
        render: ({ navigate }) => (
          <RedirectRoute
            onNavigate={navigate}
            storageKey="schedule.activeTab"
            storageValue="shifts-ledger"
            to="/schedule/shifts-ledger"
          />
        ),
      },
      {
        path: "vacations",
        render: ({ navigate }) => (
          <RedirectRoute
            onNavigate={navigate}
            storageKey="schedule.activeTab"
            storageValue="vacations"
            to="/schedule/vacations"
          />
        ),
      },
      {
        path: ":unknown",
        render: ({ navigate }) => (
          <RedirectRoute
            onNavigate={navigate}
            storageKey="schedule.activeTab"
            storageValue="shifts-ledger"
            to="/schedule/shifts-ledger"
          />
        ),
      },
    ],
  },
  {
    path: "/vacations",
    render: ({ navigate }) => (
      <RedirectRoute
        onNavigate={navigate}
        storageKey="schedule.activeTab"
        storageValue="vacations"
        to="/schedule/vacations"
      />
    ),
  },
  {
    path: "/schedule",
    children: [
      {
        path: "",
        render: ({ navigate }) => (
          <ScheduleRoute activeTab="schedule" onNavigate={navigate} useStoredTab />
        ),
      },
      {
        path: "shifts-ledger",
        render: ({ navigate }) => <ScheduleRoute activeTab="shifts-ledger" onNavigate={navigate} />,
      },
      {
        path: "vacations",
        render: ({ navigate }) => <ScheduleRoute activeTab="vacations" onNavigate={navigate} />,
      },
      {
        path: "dishwashers",
        render: ({ navigate }) => <ScheduleRoute activeTab="dishwashers" onNavigate={navigate} />,
      },
      {
        path: ":unknown",
        render: ({ navigate }) => <RedirectRoute onNavigate={navigate} to="/schedule" />,
      },
    ],
  },
  {
    path: "/source-data",
    render: ({ navigate }) => <PayrollConfigurationRoute onNavigate={navigate} />,
  },
  {
    path: "/access-control",
    render: () => <AccessControlRoute />,
  },
  {
    path: "/couriers",
    children: [
      {
        path: "",
        render: ({ navigate }) => <RedirectRoute onNavigate={navigate} to="/couriers/deposits" />,
      },
      {
        path: "deposits",
        render: ({ navigate }) => <CourierDepositsRoute onNavigate={navigate} />,
      },
      {
        path: "evaluations",
        render: () => <CourierEvaluationsRoute />,
      },
      {
        path: "statistics",
        render: ({ navigate }) => <CourierStatisticsRoute onNavigate={navigate} />,
      },
      {
        path: "schedule",
        children: [
          {
            path: "",
            render: ({ navigate }) => (
              <RedirectRoute
                onNavigate={navigate}
                storageKey="couriers.schedule.activeTab"
                storageValue="grid"
                to="/couriers/schedule/grid"
              />
            ),
          },
          {
            path: "grid",
            render: ({ navigate }) => (
              <CourierScheduleRoute activeTab="grid" onNavigate={navigate} />
            ),
          },
          {
            path: "list",
            render: ({ navigate }) => (
              <CourierScheduleRoute activeTab="list" onNavigate={navigate} />
            ),
          },
          {
            path: "shifts",
            render: ({ navigate }) => (
              <CourierScheduleRoute activeTab="shifts" onNavigate={navigate} />
            ),
          },
          {
            path: "deliveries",
            render: ({ navigate }) => (
              <CourierScheduleRoute activeTab="deliveries" onNavigate={navigate} />
            ),
          },
          {
            path: ":unknown",
            render: ({ navigate }) => (
              <RedirectRoute onNavigate={navigate} to="/couriers/schedule/grid" />
            ),
          },
        ],
      },
      {
        path: ":unknown",
        render: ({ navigate }) => <RedirectRoute onNavigate={navigate} to="/couriers/deposits" />,
      },
    ],
  },
  {
    path: "/dds",
    children: [
      {
        path: "",
        render: ({ navigate }) => <DdsRoute activeTab="today" onNavigate={navigate} useStoredTab />,
      },
      {
        path: "operations",
        render: ({ navigate }) => <DdsRoute activeTab="operations" onNavigate={navigate} />,
      },
      {
        path: "ledger",
        render: ({ navigate }) => <DdsRoute activeTab="ledger" onNavigate={navigate} />,
      },
      {
        path: "owner-review",
        render: ({ navigate }) => <DdsRoute activeTab="owner-review" onNavigate={navigate} />,
      },
      {
        path: "counterparties",
        render: ({ navigate }) => <DdsRoute activeTab="counterparties" onNavigate={navigate} />,
      },
      {
        path: "articles",
        render: ({ navigate }) => <DdsRoute activeTab="articles" onNavigate={navigate} />,
      },
      {
        path: "rules",
        render: ({ navigate }) => <DdsRoute activeTab="rules" onNavigate={navigate} />,
      },
      {
        path: "credentials",
        render: ({ navigate }) => <DdsRoute activeTab="credentials" onNavigate={navigate} />,
      },
      {
        path: ":unknown",
        render: ({ navigate }) => <DdsRoute activeTab="today" invalidPath onNavigate={navigate} />,
      },
    ],
  },
  {
    path: "/payment-calendar",
    render: () => <EmptyModule name="Платёжный календарь" />,
  },
  {
    path: "/balance",
    render: () => <EmptyModule name="Баланс" />,
  },
  {
    path: "/fixed-assets",
    render: () => <EmptyModule name="Учёт ОС" />,
  },
  {
    path: "/dz-kz",
    render: () => <EmptyModule name="Учёт ДЗ/КЗ" />,
  },
  {
    path: "/settings",
    render: ({ navigate }) => <SettingsRoute onNavigate={navigate} />,
  },
];

export function AppRouter() {
  const [path, setPath] = useState(getCurrentPath);
  const [isAuthReady, setIsAuthReady] = useState(() => path === "/login");

  useEffect(() => {
    const handlePopState = () => setPath(getCurrentPath());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    let isCancelled = false;

    if (path === "/login" || getAuthSnapshot().accessToken) {
      setIsAuthReady(true);
      return () => {
        isCancelled = true;
      };
    }

    setIsAuthReady(false);
    void restoreSession().then((token) => {
      if (isCancelled) {
        return;
      }

      if (!token) {
        window.history.replaceState({}, "", "/login");
        setPath(getCurrentPath());
      }
      setIsAuthReady(true);
    });

    return () => {
      isCancelled = true;
    };
  }, [path]);

  function navigate(nextPath: string) {
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
    setPath(getCurrentPath());
  }

  const match = matchRouteTree(routes, path, navigate) ?? {
    element: <EmptyModule name="Раздел не найден" />,
    layout: true,
  };

  if (!match.layout) {
    return (
      <>
        {match.element}
        <Toaster closeButton richColors position="top-right" />
      </>
    );
  }

  if (!isAuthReady) {
    return <div className="min-h-screen bg-background" />;
  }

  return (
    <AppLayout currentPath={path} onNavigate={navigate}>
      {match.element}
      <Toaster closeButton richColors position="top-right" />
    </AppLayout>
  );
}

function RedirectRoute({
  onNavigate,
  storageKey,
  storageValue,
  to,
}: {
  onNavigate: Navigate;
  storageKey?: string;
  storageValue?: string;
  to: string;
}) {
  useEffect(() => {
    if (storageKey && storageValue) {
      window.localStorage.setItem(storageKey, storageValue);
    }
    onNavigate(to);
  }, [onNavigate, storageKey, storageValue, to]);

  return null;
}

function matchRouteTree(
  appRoutes: AppRoute[],
  pathname: string,
  navigate: Navigate,
  basePath = "",
): MatchedRoute | null {
  for (const route of appRoutes) {
    const fullPath = joinRoutePath(basePath, route.path);

    if (route.render) {
      const params = matchPattern(fullPath, pathname);
      if (params) {
        return {
          element: route.render({ navigate, params }),
          layout: route.layout ?? true,
        };
      }
    }

    if (route.children) {
      const childMatch = matchRouteTree(route.children, pathname, navigate, fullPath);
      if (childMatch) {
        return childMatch;
      }
    }
  }

  return null;
}

function joinRoutePath(basePath: string, path: string) {
  if (path.startsWith("/")) {
    return path;
  }
  return `${basePath.replace(/\/$/, "")}/${path}` || "/";
}

function matchPattern(pattern: string, pathname: string) {
  if (pattern === "/") {
    return pathname === "/" ? {} : null;
  }

  const patternParts = pattern.split("/").filter(Boolean);
  const pathParts = pathname.split("/").filter(Boolean);

  if (patternParts.length !== pathParts.length) {
    return null;
  }

  const params: Record<string, string> = {};

  for (let index = 0; index < patternParts.length; index += 1) {
    const patternPart = patternParts[index];
    const pathPart = pathParts[index];

    if (patternPart.startsWith(":")) {
      params[patternPart.slice(1)] = decodeURIComponent(pathPart);
      continue;
    }

    if (patternPart !== pathPart) {
      return null;
    }
  }

  return params;
}

function getCurrentPath() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path || "/";
}

function withCurrentSearch(path: string) {
  return `${path}${window.location.search}`;
}
