import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDateTime, formatDdsMoney } from "@/routes/dds/shared";
import { getCheques } from "@/routes/kassa/api";
import { CreateChequeDialog } from "@/routes/kassa/CreateChequeDialog";
import { ChequeStatusBadge } from "@/routes/kassa/shared";

export function ReceiptsTab({ canCreate }: { canCreate: boolean }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const chequesQuery = useQuery({ queryKey: ["kassa", "cheques"], queryFn: () => getCheques() });
  const cheques = chequesQuery.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-muted-foreground">
          Покупки, оплаченные с бизнес-карты (и частично наличными). Каждый чек уже проведён в ДДС.
        </p>
        {canCreate ? (
          <Button onClick={() => setDialogOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            Чек
          </Button>
        ) : null}
      </div>

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Номер</TableHead>
              <TableHead>Дата</TableHead>
              <TableHead>Контрагент</TableHead>
              <TableHead>Статья ДДС</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead>Статус</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {cheques.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                  {chequesQuery.isLoading ? "Загрузка…" : "Чеков пока нет."}
                </TableCell>
              </TableRow>
            ) : (
              cheques.map((cheque) => (
                <TableRow key={cheque.id}>
                  <TableCell className="font-medium">{cheque.number ?? "—"}</TableCell>
                  <TableCell>{formatDateTime(cheque.issued_at)}</TableCell>
                  <TableCell>{cheque.counterparty_name}</TableCell>
                  <TableCell>{cheque.article_name ?? "—"}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatDdsMoney(cheque.amount)}
                  </TableCell>
                  <TableCell>
                    <ChequeStatusBadge status={cheque.payment_status} />
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <CreateChequeDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={() => chequesQuery.refetch()}
      />
    </div>
  );
}
