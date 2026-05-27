import { useEffect, useState } from "react";

import { LoginRoute } from "./routes/login";
import { SettingsRoute } from "./routes/settings";
import { StaffRoute } from "./routes/staff";

function currentPath() {
  if (window.location.pathname === "/login") {
    return "/login";
  }
  if (window.location.pathname === "/staff") {
    return "/staff";
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

  return <SettingsRoute onNavigate={navigate} />;
}
