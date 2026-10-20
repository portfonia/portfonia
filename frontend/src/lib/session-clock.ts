const STORAGE_KEY = "portfonia.lastBackendTouch";
let backendTouch = 0;

export function lastBackendTouch(): number {
  try {
    return Math.max(backendTouch, Number(localStorage.getItem(STORAGE_KEY)) || 0);
  } catch {
    return backendTouch;
  }
}

export function recordBackendTouch(ms: number): void {
  backendTouch = Math.max(lastBackendTouch(), ms);
  try {
    localStorage.setItem(STORAGE_KEY, String(backendTouch));
  } catch {
    // The module clock remains available when storage is unavailable.
  }
}
