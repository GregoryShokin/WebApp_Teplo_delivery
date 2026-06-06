import type { ReactNode } from "react";

type PageHeaderProps = {
  title: string;
  titleAccessory?: ReactNode;
  description?: string;
  action?: ReactNode;
};

export function PageHeader({ title, titleAccessory, description, action }: PageHeaderProps) {
  return (
    <header className="flex flex-col gap-4 border-b border-border pb-5 xl:flex-row xl:items-center xl:justify-between">
      <div className="min-w-0">
        <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold tracking-normal text-foreground">
          <span>{title}</span>
          {titleAccessory}
        </h1>
        {description ? (
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {action ? <div className="flex flex-wrap items-center gap-2">{action}</div> : null}
    </header>
  );
}
