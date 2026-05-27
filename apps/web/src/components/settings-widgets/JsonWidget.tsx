import { useEffect, useState } from "react";

import type { SettingWidgetProps } from "./types";
import { formatJson, parseJsonDraft } from "./widget-utils";

export function JsonWidget({ value, onChange, disabled }: SettingWidgetProps) {
  const [draft, setDraft] = useState(formatJson(value));
  const [invalid, setInvalid] = useState(false);

  useEffect(() => {
    setDraft(formatJson(value));
    setInvalid(false);
  }, [value]);

  return (
    <div className="grid gap-1">
      <textarea
        aria-label="JSON"
        className="min-h-28 w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-xs leading-5 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled}
        onBlur={() => {
          try {
            onChange(parseJsonDraft(draft));
            setInvalid(false);
          } catch {
            setInvalid(true);
          }
        }}
        onChange={(event) => {
          setDraft(event.target.value);
          setInvalid(false);
        }}
        spellCheck={false}
        value={draft}
      />
      {invalid ? <div className="text-xs text-destructive">Проверьте JSON</div> : null}
    </div>
  );
}
