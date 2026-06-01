import * as React from "react";

import { cn } from "@/lib/utils";

type AccordionProps = React.HTMLAttributes<HTMLDivElement> & {
  type?: "single" | "multiple";
  collapsible?: boolean;
};

function Accordion({ className, ...props }: AccordionProps) {
  return <div className={cn("grid gap-2", className)} {...props} />;
}

type AccordionItemProps = Omit<React.DetailsHTMLAttributes<HTMLDetailsElement>, "value"> & {
  value?: string;
};

const AccordionItem = React.forwardRef<HTMLDetailsElement, AccordionItemProps>(
  ({ className, value, ...props }, ref) => {
    void value;
    return (
      <details
        ref={ref}
        className={cn("group rounded-md border bg-background", className)}
        {...props}
      />
    );
  },
);
AccordionItem.displayName = "AccordionItem";

const AccordionTrigger = React.forwardRef<
  HTMLElement,
  React.HTMLAttributes<HTMLElement>
>(({ className, children, ...props }, ref) => (
  <summary
    ref={ref}
    className={cn(
      "flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-medium [&::-webkit-details-marker]:hidden",
      className,
    )}
    {...props}
  >
    {children}
  </summary>
));
AccordionTrigger.displayName = "AccordionTrigger";

const AccordionContent = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("border-t px-4 py-3", className)} {...props} />
));
AccordionContent.displayName = "AccordionContent";

export { Accordion, AccordionItem, AccordionTrigger, AccordionContent };
