import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";

import { CourierDepositsRoute } from "./deposits";
import { CourierEvaluationsRoute } from "./evaluations";
import { ShiftInbox } from "./shift-inbox";

export type CourierShiftActiveTab = "inbox" | "deposits" | "evaluations";

type CourierShiftRouteProps = {
  activeTab: CourierShiftActiveTab;
  onNavigate: (path: string) => void;
};

const TABS: Array<{ value: CourierShiftActiveTab; label: string }> = [
  { value: "inbox", label: "На смене" },
  { value: "deposits", label: "Депозиты" },
  { value: "evaluations", label: "Оценки" },
];

export function CourierShiftRoute({ activeTab, onNavigate }: CourierShiftRouteProps) {
  return (
    <div className="space-y-5">
      <PageHeader
        title="Смена курьеров"
        description="Оценки, депозиты и подтверждение смены за день."
      />

      <Tabs onValueChange={(value) => onNavigate(`/couriers/shift/${value}`)} value={activeTab}>
        <TabsList>
          {TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {activeTab === "inbox" ? <ShiftInbox /> : null}
      {activeTab === "deposits" ? <CourierDepositsRoute embedded onNavigate={onNavigate} /> : null}
      {activeTab === "evaluations" ? <CourierEvaluationsRoute embedded /> : null}
    </div>
  );
}
