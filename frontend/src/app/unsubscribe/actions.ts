"use server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface ConfirmUnsubscribeState {
  error: string | null;
  email?: string;
}

export async function confirmUnsubscribe(
  _prevState: ConfirmUnsubscribeState | undefined,
  formData: FormData,
): Promise<ConfirmUnsubscribeState | undefined> {
  const token = String(formData.get("token") ?? "");

  if (!token) {
    return { error: "invalidOrExpired" };
  }

  const res = await fetch(`${BACKEND_URL}/unsubscribe/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });

  if (!res.ok) {
    return { error: res.status === 400 ? "invalidOrExpired" : "genericError" };
  }

  const body = (await res.json()) as { email: string };
  return { error: null, email: body.email };
}
