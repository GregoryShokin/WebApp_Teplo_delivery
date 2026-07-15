import { useState } from "react";

import { usePermissions } from "@/lib/permissions";

import { CounterpartyCard } from "./CounterpartyCard";
import { RegistryTab } from "./tabs/registry";

/** Единственное место создания и редактирования контрагентов: «Накладные → Контрагенты». */
export function CounterpartyRegistryModule() {
  const permissions = usePermissions();
  const canOperate = permissions.canPerformAction("counterparties.operate");
  const canAdmin =
    permissions.canPerformAction("counterparties.admin") ||
    permissions.canPerformAction("finance.counterparties.edit");
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <>
      <RegistryTab canOperate={canOperate} canAdmin={canAdmin} onOpenCounterparty={setOpenId} />
      <CounterpartyCard
        counterpartyId={openId}
        canOperate={canOperate}
        canAdmin={canAdmin}
        onClose={() => setOpenId(null)}
      />
    </>
  );
}
