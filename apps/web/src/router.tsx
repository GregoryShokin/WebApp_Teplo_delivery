import { useEffect, useState, type ReactNode } from "react";

import { AppLayout } from "@/components/layout/AppLayout";
import { EmptyModule } from "@/components/layout/EmptyModule";
import { Toaster } from "@/components/ui/sonner";
import { restoreSession } from "@/lib/api";
import { getAuthSnapshot } from "@/lib/auth";
import { DashboardPage } from "@/pages/DashboardPage";
import { LoginRoute } from "@/routes/login";
import { PayrollConfigurationRoute } from "@/routes/payroll/configuration";
import { PayrollDailyLedgerRoute } from "@/routes/payroll/daily-ledger";
import { PayrollRunDetailRoute } from "@/routes/payroll/run-detail";
import { PayrollRunsRoute } from "@/routes/payroll/runs";
import { PayrollAdjustmentsRoute } from "@/routes/payroll-adjustments";
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
        path: "runs",
        render: ({ navigate }) => <PayrollRunsRoute onNavigate={navigate} />,
      },
      {
        path: "runs/:id",
        render: ({ navigate, params }) => (
          <PayrollRunDetailRoute runId={params.id ?? ""} onNavigate={navigate} />
        ),
      },
      {
        path: "daily-ledger",
        render: () => <PayrollDailyLedgerRoute />,
      },
      {
        path: "adjustments",
        render: ({ navigate }) => <PayrollAdjustmentsRoute onNavigate={navigate} />,
      },
      {
        path: "configuration",
        render: ({ navigate }) => <PayrollConfigurationRoute onNavigate={navigate} />,
      },
    ],
  },
  {
    path: "/schedule",
    render: () => <ScheduleRoute />,
  },
  {
    path: "/dds",
    render: () => <EmptyModule name="ДДС" />,
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
    render: () => <SettingsRoute />,
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
