import * as React from "react";

import { cn } from "@/lib/utils";

const Checkbox = React.forwardRef<
  HTMLInputElement,
  Omit<React.InputHTMLAttributes<HTMLInputElement>, "type">
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-4 w-4 shrink-0 rounded border border-primary accent-primary disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    type="checkbox"
    {...props}
  />
));
Checkbox.displayName = "Checkbox";

export { Checkbox };
