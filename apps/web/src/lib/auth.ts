export type AuthUser = {
  id: string;
  email: string;
  full_name: string;
  roles: string[];
};

type AuthSnapshot = {
  accessToken: string | null;
  user: AuthUser | null;
};

type Listener = (snapshot: AuthSnapshot) => void;

let accessToken: string | null = null;
let user: AuthUser | null = null;
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
  return { accessToken, user };
}

export function setSession(nextAccessToken: string, nextUser: AuthUser) {
  accessToken = nextAccessToken;
  user = nextUser;
  emit();
}

export function clearSession() {
  accessToken = null;
  user = null;
  emit();
}

export function subscribeAuth(listener: Listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
