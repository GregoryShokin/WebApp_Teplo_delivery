import { useEffect, useState } from "react";

import { LoginRoute } from "./routes/login";
import { PayrollRunDetailRoute } from "./routes/payroll/run-detail";
import { PayrollRunsRoute } from "./routes/payroll/runs";
import { SettingsRoute } from "./routes/settings";
import { StaffRoute } from "./routes/staff";

function currentPath() {
  if (window.location.pathname === "/login") {
    return "/login";
  }
  if (window.location.pathname === "/staff") {
    return "/staff";
  }
  if (window.location.pathname === "/payroll/runs") {
    return "/payroll/runs";
  }
  if (window.location.pathname.startsWith("/payroll/runs/")) {
    return window.location.pathname;
  }
  return "/settings";
}

export function App() {
  const [path, setPath] = useState(currentPath);

  useEffect(() => {
    const handlePopState = () => setPath(currentPath());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function navigate(nextPath: string) {
    window.history.pushState({}, "", nextPath);
    setPath(currentPath());
  }

  if (path === "/login") {
    return <LoginRoute onNavigate={navigate} />;
  }
  if (path === "/staff") {
    return <StaffRoute />;
  }
  if (path === "/payroll/runs") {
    return <PayrollRunsRoute onNavigate={navigate} />;
  }
  if (path.startsWith("/payroll/runs/")) {
    return <PayrollRunDetailRoute runId={path.split("/").pop() ?? ""} onNavigate={navigate} />;
  }

  return <SettingsRoute onNavigate={navigate} />;
}
