/**
 * "Remember username and password" storage.
 *
 * !!! The password is stored in **plaintext** in localStorage. That is what was
 *     asked for, and it is a real trade-off, not an oversight:
 *       - any script running on this origin can read it (XSS / a compromised
 *         dependency);
 *       - anyone using the same browser profile can read it.
 *     The login form says so next to the checkbox so the user is choosing with
 *     eyes open. Do not remove that line without asking first.
 *
 * !!! Both getters/setters are wrapped in try/catch: localStorage throws
 *     outright in private mode and in sandboxed iframes, and a login page that
 *     crashes on load is a worse outcome than not remembering anything.
 *
 * Key name is namespaced so it is obvious what it is if anyone inspects storage.
 */
const KEY = 'campus-rag-saved-credentials'

export interface SavedCredentials {
  username: string
  password: string
}

export function loadCredentials(): SavedCredentials | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<SavedCredentials>
    if (typeof parsed.username === 'string' && typeof parsed.password === 'string') {
      return { username: parsed.username, password: parsed.password }
    }
  } catch { /* corrupted or unavailable — fall back to an empty form */ }
  return null
}

/** Persist credentials, or erase them when the box is unchecked. */
export function saveCredentials(value: SavedCredentials | null): void {
  try {
    if (value) localStorage.setItem(KEY, JSON.stringify(value))
    else localStorage.removeItem(KEY)
  } catch { /* storage unavailable — remembering is best-effort */ }
}
