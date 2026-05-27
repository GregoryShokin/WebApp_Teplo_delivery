import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Database, FileCheck2, RefreshCw, ShieldCheck, Workflow } from "lucide-react";

import { Button } from "../components/ui/button";
import { getHealth, getIntegrationDefinitions } from "../lib/api";

const modules = [
  { name: "Зарплата", status: "first module", detail: "employees, shifts, ledger" },
  { name: "Финансы", status: "core", detail: "DDS, P&L, balance" },
  { name: "Интеграции", status: "audit-ready", detail: "iiko, banks, mail, OCR" },
  { name: "Склад", status: "later", detail: "tech cards, inventory" },
];

export function DashboardPage() {
  const healthQuery = useQuery({ queryKey: ["health"], queryFn: getHealth });
  const integrationsQuery = useQuery({
    queryKey: ["integration-definitions"],
    queryFn: getIntegrationDefinitions,
  });

  return (
    <main className="min-h-screen">
      <div className="mx-auto grid min-h-screen max-w-7xl grid-cols-1 lg:grid-cols-[240px_1fr]">
        <aside className="border-b border-border bg-white px-5 py-5 lg:border-b-0 lg:border-r">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Database size={18} aria-hidden="true" />
            </div>
            <div>
              <div className="text-base font-semibold">Тепло</div>
              <div className="text-xs text-muted-foreground">management app</div>
            </div>
          </div>
          <nav className="mt-8 grid gap-1 text-sm">
            {["Обзор", "Штат", "Зарплата", "ДДС", "ОПиУ", "Интеграции", "Аудит"].map((item) => (
              <a
                className="rounded-md px-3 py-2 text-left text-muted-foreground hover:bg-muted hover:text-foreground"
                href={item === "Штат" ? "/staff" : "/"}
                key={item}
              >
                {item}
              </a>
            ))}
          </nav>
        </aside>

        <section className="px-5 py-5 sm:px-8">
          <header className="flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h1 className="text-2xl font-semibold tracking-normal">Операционный контур</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Первый экран для self-hosted учета, интеграций и источников.
              </p>
            </div>
            <Button
              onClick={() => {
                void healthQuery.refetch();
                void integrationsQuery.refetch();
              }}
              variant="outline"
            >
              <RefreshCw size={16} aria-hidden="true" />
              Обновить
            </Button>
          </header>

          <div className="mt-6 grid gap-4 md:grid-cols-3">
            <StatusTile
              icon={<ShieldCheck size={20} aria-hidden="true" />}
              label="API"
              value={healthQuery.data?.status ?? (healthQuery.isError ? "offline" : "checking")}
            />
            <StatusTile
              icon={<Workflow size={20} aria-hidden="true" />}
              label="Источники"
              value={`${integrationsQuery.data?.length ?? 0} adapters`}
            />
            <StatusTile
              icon={<FileCheck2 size={20} aria-hidden="true" />}
              label="Audit"
              value="source_reference"
            />
          </div>

          <div className="mt-6 grid gap-6 xl:grid-cols-[1fr_380px]">
            <section className="rounded-lg border border-border bg-white">
              <div className="border-b border-border px-4 py-3">
                <h2 className="text-sm font-semibold uppercase text-muted-foreground">Модули</h2>
              </div>
              <div className="divide-y divide-border">
                {modules.map((module) => (
                  <div className="grid gap-2 px-4 py-4 sm:grid-cols-[160px_140px_1fr]" key={module.name}>
                    <div className="font-medium">{module.name}</div>
                    <div className="text-sm text-primary">{module.status}</div>
                    <div className="text-sm text-muted-foreground">{module.detail}</div>
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded-lg border border-border bg-white">
              <div className="border-b border-border px-4 py-3">
                <h2 className="text-sm font-semibold uppercase text-muted-foreground">Интеграции</h2>
              </div>
              <div className="divide-y divide-border">
                {(integrationsQuery.data ?? []).map((source) => (
                  <div className="px-4 py-3" key={source.code}>
                    <div className="flex items-center justify-between gap-3">
                      <div className="font-medium">{source.name}</div>
                      <div className="rounded-sm bg-muted px-2 py-1 text-xs text-muted-foreground">
                        {source.pattern}
                      </div>
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">{source.script_path}</div>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </section>
      </div>
    </main>
  );
}

function StatusTile({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-muted-foreground">{label}</div>
        <div className="text-primary">{icon}</div>
      </div>
      <div className="mt-3 text-xl font-semibold">{value}</div>
    </div>
  );
}
