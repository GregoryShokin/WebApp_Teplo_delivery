export type AuthUser = {
  id: string;
  email: string;
  full_name: string;
  roles: string[];
  permissions?: string[];
};

type AuthSnapshot = {
  accessToken: string | null;
  user: AuthUser | null;
  permissions: string[];
};

type Listener = (snapshot: AuthSnapshot) => void;

let accessToken: string | null = null;
let user: AuthUser | null = null;
let permissions: string[] = [];
const listeners = new Set<Listener>();

function emit() {
  const snapshot = getAuthSnapshot();
  listeners.forEach((listener) => listener(snapshot));
}

export function getAccessToken() {
  return accessToken;
}

export function getCurrentUser() {
  return user;
}

export function getAuthSnapshot(): AuthSnapshot {
  return { accessToken, user, permissions };
}

export function getCurrentPermissions() {
  return permissions;
}

export function hasAuthPermission(permission: string, snapshot = getAuthSnapshot()) {
  if (!snapshot.user) {
    return false;
  }
  if (snapshot.permissions.includes(permission)) {
    return true;
  }
  return snapshot.user.roles.some((role) => role === "owner" || role === "admin");
}

export function setSession(nextAccessToken: string, nextUser: AuthUser) {
  const tokenPermissions = decodePermissionsClaim(nextAccessToken);
  accessToken = nextAccessToken;
  permissions = tokenPermissions;
  user = { ...nextUser, permissions: tokenPermissions };
  emit();
}

export function clearSession() {
  accessToken = null;
  user = null;
  permissions = [];
  emit();
}

export function subscribeAuth(listener: Listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function decodePermissionsClaim(token: string) {
  const [, payload] = token.split(".");
  if (!payload) {
    return [];
  }

  try {
    const parsed = JSON.parse(decodeBase64Url(payload)) as { permissions?: unknown };
    if (!Array.isArray(parsed.permissions)) {
      return [];
    }
    return parsed.permissions.filter((permission): permission is string => typeof permission === "string");
  } catch {
    return [];
  }
}

function decodeBase64Url(value: string) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
  const decoder = globalThis.atob;
  if (!decoder) {
    return "";
  }

  const binary = decoder(padded);
  const bytes = Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(new Uint8Array(bytes));
}
