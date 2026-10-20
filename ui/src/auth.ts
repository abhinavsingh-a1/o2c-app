const KEY = "o2c-session";

export type Session = { token: string; username: string; name: string };

export function getSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function setSession(session: Session): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // storage blocked (e.g. private mode): the user simply has to log in again
  }
}

export function clearSession(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}
