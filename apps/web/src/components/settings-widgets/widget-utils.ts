import type { SettingWidgetOption } from "./types";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function getPath(value: unknown, path?: string) {
  if (!path) {
    return value;
  }
  let current = value;
  for (const part of path.split(".")) {
    if (!isRecord(current)) {
      return undefined;
    }
    current = current[part];
  }
  return current;
}

export function setPath(value: unknown, path: string | undefined, nextValue: unknown) {
  if (!path) {
    return nextValue;
  }
  const root = isRecord(value) ? { ...value } : {};
  let current: Record<string, unknown> = root;
  const parts = path.split(".");
  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      current[part] = nextValue;
      return;
    }
    const child = current[part];
    current[part] = isRecord(child) ? { ...child } : {};
    current = current[part] as Record<string, unknown>;
  });
  return root;
}

export function numberFromUnknown(value: unknown) {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

export function formatJson(value: unknown) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

export function parseJsonDraft(value: string) {
  return JSON.parse(value);
}

export function stableOptionValue(value: unknown) {
  return JSON.stringify(value);
}

export function findSelectLabel(value: unknown, options?: SettingWidgetOption[]) {
  const selected = stableOptionValue(value);
  return options?.find((option) => stableOptionValue(option.value) === selected)?.label;
}
