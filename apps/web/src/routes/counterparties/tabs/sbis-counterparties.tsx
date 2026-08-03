import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Settings2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";

import { getSbisDocuments, rerouteSbisCounterparty, type SbisDocument } from "../api";
import { CounterpartyCard } from "../CounterpartyCard";
import { MetricCard, formatDate, formatRub } from "../shared";

/** Контрагент, пришедший из ЭДО и ждущий разбора: строка = карточка, а не документ.
 *  У одного поставщика документов бывает несколько, и после настройки карточки уходить
 *  из очереди должны все сразу — список по документам врал бы, что работы больше. */
type PendingCounterparty = {
  counterpartyId: string;
  name: string;
  inn: string | null;
  documents: SbisDocument[];
  total: number;
  firstSeen: string | null;
};

function groupByCounterparty(documents: SbisDocument[]): PendingCounterparty[] {
  const byId = new Map<string, PendingCounterparty>();
  for (const doc of documents) {
    if (doc.intake_status !== "new_counterparty" || doc.match_status === "dismissed") continue;
    if (!doc.counterparty_id) continue;
    const existing = byId.get(doc.counterparty_id);
    const amount = Number(doc.amount ?? 0);
    if (existing) {
      existing.documents.push(doc);
      existing.total += amount;
      if (doc.doc_date && (!existing.firstSeen || doc.doc_date < existing.firstSeen)) {
        existing.firstSeen = doc.doc_date;
      }
      continue;
    }
    byId.set(doc.counterparty_id, {
      counterpartyId: doc.counterparty_id,
      name: doc.counterparty_name ?? (doc.counterparty_inn ? `ИНН ${doc.counterparty_inn}` : "—"),
      inn: doc.counterparty_inn,
      documents: [doc],
      total: amount,
      firstSeen: doc.doc_date,
    });
  }
  return [...byId.values()].sort((a, b) => (a.firstSeen ?? "").localeCompare(b.firstSeen ?? ""));
}

type Props = {
  canOperate: boolean;
  canAdmin: boolean;
};

export function SbisCounterpartiesTab({ canOperate, canAdmin }: Props) {
  const [openCounterpartyId, setOpenCounterpartyId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // Тот же запрос и тот же ключ, что у вкладки ЭДО: реестр документов один, разрезов два.
  const documentsQuery = useQuery({
    queryKey: ["sbis", "documents"],
    queryFn: () => getSbisDocuments(),
  });
  const documents = useMemo(() => documentsQuery.data ?? [], [documentsQuery.data]);
  const pending = useMemo(() => groupByCounterparty(documents), [documents]);

  // Документ без ИНН карточку получить не может — синк идентифицирует контрагента только
  // по нему. Такие строки не в очереди разбора, но и молчать о них нельзя: иначе они
  // навсегда остаются в зеркале, а человек считает, что ЭДО разобран полностью.
  const withoutInn = useMemo(
    () =>
      documents.filter(
        (doc) => !doc.counterparty_inn && doc.match_status !== "dismissed" && !doc.counterparty_id,
      ),
    [documents],
  );

  const rerouteMutation = useMutation({
    mutationFn: rerouteSbisCounterparty,
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["sbis"] });
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      if (result.materialized > 0) {
        toast.success(`Карточка настроена, счетов в очередь оплат: ${result.materialized}`);
      } else if (result.sent_to_recognition > 0) {
        toast.success(`Карточка настроена, писем-счетов в разбор: ${result.sent_to_recognition}`);
      } else {
        toast.success("Карточка настроена — документы поставщика перепроверены");
      }
    },
    onError: (error) =>
      toast.warning(
        `${apiErrorMessage(error, "не удалось переразобрать документы")}. Карточка сохранена — документы разберутся ближайшим обновлением из СБИС.`,
      ),
  });

  const columns: Array<DataTableColumn<PendingCounterparty>> = [
    {
      key: "name",
      header: "Контрагент",
      className: "min-w-[240px]",
      cell: (row) => (
        <div>
          <div className="font-medium">{row.name}</div>
          <div className="text-xs text-muted-foreground">
            {row.inn ? `ИНН ${row.inn}` : "без ИНН"}
          </div>
        </div>
      ),
    },
    {
      key: "documents",
      header: "Документов",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (row) => row.documents.length,
    },
    {
      key: "total",
      header: "На сумму",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (row) => formatRub(row.total),
    },
    {
      key: "first",
      header: "Ждёт с",
      cell: (row) => (row.firstSeen ? formatDate(row.firstSeen) : "—"),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (row) =>
        canAdmin ? (
          <Button
            variant="outline"
            size="sm"
            onClick={(event) => {
              event.stopPropagation();
              setOpenCounterpartyId(row.counterpartyId);
            }}
          >
            <Settings2 size={15} aria-hidden="true" />
            Настроить карточку
          </Button>
        ) : (
          // Право на разбор — ADMIN, вкладку же видит и операционная роль. Молчащая
          // строка выглядела бы как поломка, поэтому называем причину.
          <span className="text-xs text-muted-foreground">Нужно право на правку карточек</span>
        ),
    },
  ];

  const noInnColumns: Array<DataTableColumn<SbisDocument>> = [
    { key: "date", header: "Дата", cell: (doc) => formatDate(doc.doc_date) },
    {
      key: "counterparty",
      header: "Контрагент в документе",
      cell: (doc) => doc.counterparty_name ?? "—",
    },
    { key: "number", header: "Номер", cell: (doc) => doc.number ?? "—" },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (doc) => (doc.amount != null ? formatRub(doc.amount) : "—"),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (doc) =>
        doc.link_cabinet ? (
          <Button
            variant="ghost"
            size="sm"
            title="Открыть в кабинете СБИС"
            onClick={() => window.open(doc.link_cabinet ?? "", "_blank", "noopener")}
          >
            <ExternalLink size={15} aria-hidden="true" />
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <MetricCard
          label="Ждут разбора"
          value={String(pending.length)}
          accent={pending.length > 0 ? "danger" : undefined}
        />
        <MetricCard label="Документов без ИНН" value={String(withoutInn.length)} />
      </div>

      {documentsQuery.isError ? (
        // Ошибка запроса и пустая очередь — разные состояния: «разбирать некого» по
        // недоступному реестру читается как «работа сделана».
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
          Не удалось загрузить контрагентов из ЭДО:{" "}
          {apiErrorMessage(documentsQuery.error, "ошибка запроса")}.{" "}
          <button className="underline" onClick={() => documentsQuery.refetch()} type="button">
            Повторить
          </button>
        </div>
      ) : (
        <DataTable
          columns={columns}
          rows={pending}
          isLoading={documentsQuery.isLoading}
          getRowKey={(row) => row.counterpartyId}
          onRowClick={(row) => canAdmin && setOpenCounterpartyId(row.counterpartyId)}
          emptyMessage="Новых контрагентов из ЭДО нет — все поставщики настроены."
        />
      )}

      {withoutInn.length > 0 ? (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold">Документы без ИНН</h3>
            <Badge variant="outline">{withoutInn.length}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            Карточка контрагента заводится по ИНН, а в этих документах его нет — связать их с
            поставщиком нельзя ни автоматически, ни руками. Открывайте в кабинете СБИС и
            запрашивайте у отправителя корректный документ.
          </p>
          <DataTable
            columns={noInnColumns}
            rows={withoutInn}
            getRowKey={(doc) => doc.id}
            emptyMessage="Таких документов нет"
          />
        </section>
      ) : null}

      <CounterpartyCard
        counterpartyId={openCounterpartyId}
        canOperate={canOperate}
        canAdmin={canAdmin}
        onClose={() => setOpenCounterpartyId(null)}
        onProfileSaved={(counterpartyId) => {
          // Разбор здесь — всегда разбор «нового»: другого повода открыть карточку с этой
          // вкладки нет. Переразбор под operate, без права молчим — синк подхватит сам.
          if (canOperate) rerouteMutation.mutate(counterpartyId);
        }}
      />
    </div>
  );
}
