import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

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

import { SbisCounterpartiesTab } from "@/routes/counterparties/tabs/sbis-counterparties";
import { SbisTab } from "@/routes/counterparties/tabs/sbis";
import { PaymentIntakesTab } from "@/routes/payment-page";
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

/** Вкладки страницы «Платежи». Живут в URL, а не в состоянии: без этого нельзя дать
 *  ссылку «вот здесь новые контрагенты», а редирект со старого /warehouse/sbis
 *  приземлял человека на «Активные» вместо реестра ЭДО, ради которого он и шёл. */
export type PaymentsTab = "active" | "history" | "edo" | "edo-counterparties";

const TAB_PATHS: Record<PaymentsTab, string> = {
  active: "/finance/payments",
  history: "/finance/payments/history",
  edo: "/finance/payments/edo",
  "edo-counterparties": "/finance/payments/edo-counterparties",
};

export function FinancePaymentsRoute({
  activeTab = "active",
  onNavigate,
}: {
  activeTab?: PaymentsTab;
  onNavigate?: (path: string) => void;
}) {
  const permissions = usePermissions();
  const canOperate = permissions.canPerformAction("counterparties.operate");
  // Разбор карточки контрагента — правка профиля, а она под ADMIN: те же два права,
  // что и в реестре контрагентов.
  const canAdmin =
    permissions.canPerformAction("counterparties.admin") ||
    permissions.canPerformAction("finance.counterparties.edit");
  // Агрегатор нужен ровно одной вкладке: «Активные» показывают очередь на оплату
  // (журнал входящих счетов), а не сводку платёжного контура.
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["finance-payments", "all"],
    queryFn: () => getPayments("all"),
    enabled: activeTab === "history",
  });

  const items = data?.items ?? [];

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Платежи</h1>
        <p className="text-sm text-muted-foreground">
          Счета на оплату из почты, ЭДО и от телеграм-бота — и вся история исходящих платежей
          контура: свободные траты, предоплаты, резервы Сейфа и Кассы.
        </p>
      </div>

      <div className="flex items-center justify-between gap-3">
        <Tabs
          value={activeTab}
          onValueChange={(v) => onNavigate?.(TAB_PATHS[v as PaymentsTab])}
        >
          <TabsList>
            <TabsTrigger value="active">Активные</TabsTrigger>
            <TabsTrigger value="history">Вся история</TabsTrigger>
            <TabsTrigger value="edo">ЭДО (СБИС)</TabsTrigger>
            <TabsTrigger value="edo-counterparties">Новые контрагенты</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {activeTab === "active" ? (
        <PaymentIntakesTab />
      ) : activeTab === "edo-counterparties" ? (
        <SbisCounterpartiesTab canOperate={canOperate} canAdmin={canAdmin} />
      ) : activeTab === "edo" ? (
        <SbisTab canOperate={canOperate} canAdmin={canAdmin} />
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
