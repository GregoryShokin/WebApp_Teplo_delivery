import type { ReactNode } from "react";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type EmptyStateProps = {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <Card className={cn("border-dashed bg-card shadow-none", className)}>
      <CardContent className="flex flex-col items-center gap-3 px-6 py-10 text-center">
        {icon ? (
          <div className="flex h-10 w-10 items-center justify-center rounded-md bg-accent text-accent-foreground">
            {icon}
          </div>
        ) : null}
        <div>
          <div className="text-sm font-semibold text-foreground">{title}</div>
          {description ? (
            <p className="mt-1 max-w-md text-sm leading-6 text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {action ? <div className="mt-1">{action}</div> : null}
      </CardContent>
    </Card>
  );
}
