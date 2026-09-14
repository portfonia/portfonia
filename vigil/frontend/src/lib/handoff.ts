export const HANDSHAKE_STATE_BYTES = 32;
export const HANDSHAKE_TTL_MS = 3 * 60 * 1000;

export type ReadyMessage = { type: "ready" };
export type RequestMessage = { type: "request"; state: string };
export type SessionMessage = {
  type: "session";
  state: string;
  access_token: string;
  refresh_token: string;
};
export type HandshakeMessage = ReadyMessage | RequestMessage | SessionMessage;

export type RejectReason =
  | "origin"
  | "source"
  | "schema"
  | "state"
  | "expired"
  | "duplicate"
  | "cleared";

export type ConsumeResult =
  | { ok: true; message: HandshakeMessage }
  | { ok: false; reason: RejectReason };

export type HandshakeEvent = {
  origin: string;
  source: unknown;
  data: unknown;
};

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function keysOf(value: object): string[] {
  return Object.keys(value).sort();
}

export function isAllowedOrigin(eventOrigin: string, configuredOrigin: string): boolean {
  if (!configuredOrigin || configuredOrigin === "*") {
    return false;
  }
  return eventOrigin === configuredOrigin;
}

export function parseHandshakeMessage(data: unknown): HandshakeMessage | null {
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return null;
  }
  const rec = data as Record<string, unknown>;
  if (rec.type === "ready") {
    if (keysOf(rec).join(",") !== "type") return null;
    return { type: "ready" };
  }
  if (rec.type === "request") {
    if (keysOf(rec).join(",") !== "state,type") return null;
    if (!isNonEmptyString(rec.state)) return null;
    return { type: "request", state: rec.state };
  }
  if (rec.type === "session") {
    if (keysOf(rec).join(",") !== "access_token,refresh_token,state,type") return null;
    if (
      !isNonEmptyString(rec.state) ||
      !isNonEmptyString(rec.access_token) ||
      !isNonEmptyString(rec.refresh_token)
    ) {
      return null;
    }
    return {
      type: "session",
      state: rec.state,
      access_token: rec.access_token,
      refresh_token: rec.refresh_token,
    };
  }
  return null;
}

export function generateHandshakeState(
  randomBytes: (size: number) => Uint8Array = (size) => {
    const bytes = new Uint8Array(size);
    crypto.getRandomValues(bytes);
    return bytes;
  },
): string {
  const bytes = randomBytes(HANDSHAKE_STATE_BYTES);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function createStateTicket(opts: {
  state: string;
  expectedOrigin: string;
  expectedSource: object;
  createdAtMs: number;
  now?: () => number;
}): { consume: (event: HandshakeEvent) => ConsumeResult; clear: () => void } {
  let consumed = false;
  let cleared = false;
  const now = opts.now ?? Date.now;

  return {
    consume(event: HandshakeEvent): ConsumeResult {
      if (cleared) {
        return { ok: false, reason: "cleared" };
      }
      if (!isAllowedOrigin(event.origin, opts.expectedOrigin)) {
        return { ok: false, reason: "origin" };
      }
      if (event.source !== opts.expectedSource) {
        return { ok: false, reason: "source" };
      }
      const message = parseHandshakeMessage(event.data);
      if (message === null) {
        return { ok: false, reason: "schema" };
      }
      if (message.type === "ready") {
        return { ok: true, message };
      }
      if (now() - opts.createdAtMs > HANDSHAKE_TTL_MS) {
        return { ok: false, reason: "expired" };
      }
      if (message.state !== opts.state) {
        return { ok: false, reason: "state" };
      }
      if (message.type === "session") {
        if (consumed) {
          return { ok: false, reason: "duplicate" };
        }
        consumed = true;
      }
      return { ok: true, message };
    },
    clear() {
      cleared = true;
    },
  };
}
