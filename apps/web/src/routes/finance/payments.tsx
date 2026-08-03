import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

import { SbisTab } from "@/routes/counterparties/tabs/sbis";
import { getPayments, type PaymentRow } from "./payments-api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});
const dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });

function stateVariant(row: PaymentRow): "default" | "secondary" | "outline" {
  if (row.state === "paid") return "secondary";
  if (row.bucket === "to_pay" || row.bucket === "bank_ready") return "default";
  if (row.bucket === "reserved_safe" || row.bucket === "reserved_kassa") return "secondary";
  return "outline";
}

function methodLabel(row: PaymentRow): string {
  if (row.method === "cash") return "Наличные / Сейф";
  return row.bank_channel === "sber" ? "Сбербанк" : "Т-Банк";
}

export function FinancePaymentsRoute(_props: { onNavigate?: (path: string) => void }) {
  const [scope, setScope] = useState<"active" | "all" | "edo">("active");
  const permissions = usePermissions();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["finance-payments", scope],
    queryFn: () => getPayments(scope === "edo" ? "active" : scope),
    enabled: scope !== "edo",
  });

  const items = data?.items ?? [];
  const buckets = data?.buckets ?? [];

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Платежи</h1>
        <p className="text-sm text-muted-foreground">
          Все исходящие платежи платёжного контура: счета на оплату, свободные траты и
          предоплаты, резервы Сейфа и Кассы.
        </p>
      </div>

      <div className="flex items-center justify-between gap-3">
        <Tabs value={scope} onValueChange={(v) => setScope(v as "active" | "all" | "edo")}>
          <TabsList>
            <TabsTrigger value="active">Активные</TabsTrigger>
            <TabsTrigger value="all">Вся история</TabsTrigger>
            <TabsTrigger value="edo">ЭДО (СБИС)</TabsTrigger>
          </TabsList>
        </Tabs>
        {scope === "active" ? (
          <div className="flex flex-wrap gap-2">
            {buckets.map((b) => (
              <span
                key={b.key}
                className="rounded-full border px-3 py-1 text-xs text-muted-foreground"
              >
                {b.label}: <span className="font-semibold text-foreground">{b.count}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {scope === "edo" ? (
        <SbisTab
          canOperate={permissions.canPerformAction("counterparties.operate")}
          // Разбор карточки нового контрагента — правка профиля, а она под ADMIN:
          // те же два права, что и в реестре контрагентов.
          canAdmin={
            permissions.canPerformAction("counterparties.admin") ||
            permissions.canPerformAction("finance.counterparties.edit")
          }
        />
      ) : (
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Получатель / назначение</TableHead>
              <TableHead>Статья</TableHead>
              <TableHead>Способ</TableHead>
              <TableHead>Статус</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead className="text-right">Дата</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="py-10 text-center text-muted-foreground">
                  <Loader2 className="mr-2 inline animate-spin" size={16} /> Загрузка…
                </TableCell>
              </TableRow>
            ) : isError ? (
              // Ошибка запроса (403/500/сеть) — не выдаём за «платежей нет».
              <TableRow>
                <TableCell colSpan={6} className="py-10 text-center text-destructive">
                  Не удалось загрузить платежи: {apiErrorMessage(error, "ошибка запроса")}.{" "}
                  <button className="underline" onClick={() => refetch()} type="button">
                    Повторить
                  </button>
                </TableCell>
              </TableRow>
            ) : items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="py-10 text-center text-muted-foreground">
                  Платежей нет.
                </TableCell>
              </TableRow>
            ) : (
              items.map((row) => (
                <TableRow key={row.id}>
                  <TableCell className="max-w-[280px]">
                    <div className="truncate font-medium">{row.title}</div>
                    {row.counterparty_name && row.counterparty_name !== row.title ? (
                      <div className="truncate text-xs text-muted-foreground">
                        {row.counterparty_name}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {row.article_name ?? "—"}
                  </TableCell>
                  <TableCell className="text-sm">{methodLabel(row)}</TableCell>
                  <TableCell>
                    <Badge variant={stateVariant(row)}>{row.state_label}</Badge>
                  </TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {money.format(row.amount)}
                  </TableCell>
                  <TableCell className="text-right text-sm text-muted-foreground">
                    {dateFmt.format(new Date(row.created_at))}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      )}
    </div>
  );
}
