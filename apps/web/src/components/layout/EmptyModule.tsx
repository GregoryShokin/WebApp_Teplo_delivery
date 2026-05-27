import { Hammer } from "lucide-react";

import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";

export function EmptyModule({ name }: { name: string }) {
  return (
    <div className="space-y-5">
      <PageHeader
        title={name}
        description="Раздел появится в общем рабочем контуре после следующей итерации."
      />
      <EmptyState
        icon={<Hammer className="h-5 w-5" aria-hidden="true" />}
        title="Модуль в разработке"
        description="Сейчас здесь закреплено место в навигации, чтобы структура продукта уже была понятной."
      />
    </div>
  );
}
