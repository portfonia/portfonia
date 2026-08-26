import { clearPendingLogin } from "@/hooks/use-session";

import { isNextRedirectError } from "@/lib/next-redirect-error";

// Shared by LoginForm / SignupForm: the pending-login signal must stay
// armed across a successful redirect(), and must be disarmed on any
// other failure — returned `{ error }` or a thrown network/server error.
export async function settleAuthAction<T extends { error?: string | null }>(
  run: () => Promise<T | undefined>,
  thrownError: string,
): Promise<T | undefined> {
  try {
    const result = await run();
    if (result?.error) clearPendingLogin();
    return result;
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    clearPendingLogin();
    return { error: thrownError } as T;
  }
}
