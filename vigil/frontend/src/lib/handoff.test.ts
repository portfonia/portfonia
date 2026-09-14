// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  HANDSHAKE_STATE_BYTES,
  HANDSHAKE_TTL_MS,
  createStateTicket,
  generateHandshakeState,
  isAllowedOrigin,
  parseHandshakeMessage,
} from "./handoff";

const STATE = "A".repeat(43);
const SOURCE = { id: "popup-window" };
const ORIGIN = "https://portfonia.com";

describe("isAllowedOrigin", () => {
  it("accepts only the exact configured origin", () => {
    expect(isAllowedOrigin(ORIGIN, ORIGIN)).toBe(true);
  });

  it.each([
    "https://evil.example",
    "https://portfonia.com.evil.example",
    "https://sub.portfonia.com",
    "http://portfonia.com",
    "https://portfonia.com/",
    "https://Portfonia.com",
    "*",
    "",
  ])("rejects %j against the configured origin", (actual) => {
    expect(isAllowedOrigin(actual, ORIGIN)).toBe(false);
  });

  it("never treats a wildcard config as allowed", () => {
    expect(isAllowedOrigin("https://portfonia.com", "*")).toBe(false);
  });
});

describe("parseHandshakeMessage", () => {
  it("accepts a ready message", () => {
    expect(parseHandshakeMessage({ type: "ready" })).toEqual({ type: "ready" });
  });

  it("accepts a request message with state", () => {
    expect(parseHandshakeMessage({ type: "request", state: STATE })).toEqual({
      type: "request",
      state: STATE,
    });
  });

  it("accepts a session message with tokens", () => {
    expect(
      parseHandshakeMessage({
        type: "session",
        state: STATE,
        access_token: "at",
        refresh_token: "rt",
      }),
    ).toEqual({
      type: "session",
      state: STATE,
      access_token: "at",
      refresh_token: "rt",
    });
  });

  it.each([
    null,
    undefined,
    "ready",
    1,
    [],
    { type: "other" },
    { type: "ready", extra: true },
    { type: "request" },
    { type: "request", state: 1 },
    { type: "session", state: STATE, access_token: "at" },
    { type: "session", state: STATE, access_token: "at", refresh_token: "" },
    { type: "session", state: STATE, access_token: "at", refresh_token: "rt", extra: 1 },
  ])("rejects malformed or unknown payload %j", (data) => {
    expect(parseHandshakeMessage(data)).toBeNull();
  });
});

describe("generateHandshakeState", () => {
  it("encodes 32 random bytes as unpadded base64url", () => {
    const bytes = new Uint8Array(32).map((_, i) => i);
    const state = generateHandshakeState(() => bytes);
    expect(state).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(Buffer.from(state, "base64url").length).toBe(HANDSHAKE_STATE_BYTES);
  });
});

describe("createStateTicket", () => {
  const source = SOURCE;

  it("accepts a matching unused state within the ttl", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 1_000,
    });
    expect(
      ticket.consume({ origin: ORIGIN, source, data: { type: "request", state: STATE } }),
    ).toEqual({ ok: true, message: { type: "request", state: STATE } });
  });

  it("rejects the wrong origin even with a matching state", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    expect(
      ticket.consume({
        origin: "https://evil.example",
        source,
        data: { type: "request", state: STATE },
      }),
    ).toEqual({ ok: false, reason: "origin" });
  });

  it("rejects a different window source", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    expect(
      ticket.consume({
        origin: ORIGIN,
        source: { id: "other" },
        data: { type: "request", state: STATE },
      }),
    ).toEqual({ ok: false, reason: "source" });
  });

  it("rejects a stale state after 3 minutes", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => HANDSHAKE_TTL_MS + 1,
    });
    expect(
      ticket.consume({ origin: ORIGIN, source, data: { type: "request", state: STATE } }),
    ).toEqual({ ok: false, reason: "expired" });
  });

  it("rejects a replay of the same state", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    const first = {
      origin: ORIGIN,
      source,
      data: {
        type: "session",
        state: STATE,
        access_token: "at",
        refresh_token: "rt",
      },
    };
    expect(ticket.consume(first).ok).toBe(true);
    expect(ticket.consume(first)).toEqual({ ok: false, reason: "duplicate" });
  });

  it("rejects a state that does not match the ticket", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    expect(
      ticket.consume({
        origin: ORIGIN,
        source,
        data: { type: "request", state: "other" },
      }),
    ).toEqual({ ok: false, reason: "state" });
  });

  it("ready messages do not consume the state ticket", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    expect(ticket.consume({ origin: ORIGIN, source, data: { type: "ready" } })).toEqual({
      ok: true,
      message: { type: "ready" },
    });
    expect(
      ticket.consume({
        origin: ORIGIN,
        source,
        data: {
          type: "session",
          state: STATE,
          access_token: "at",
          refresh_token: "rt",
        },
      }).ok,
    ).toBe(true);
  });

  it("rejects unknown types before touching the ticket", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    expect(ticket.consume({ origin: ORIGIN, source, data: { type: "steal" } })).toEqual({
      ok: false,
      reason: "schema",
    });
  });

  it("rejects after clear, including a later matching session", () => {
    const ticket = createStateTicket({
      state: STATE,
      expectedSource: source,
      expectedOrigin: ORIGIN,
      createdAtMs: 0,
      now: () => 0,
    });
    ticket.clear();
    expect(
      ticket.consume({
        origin: ORIGIN,
        source,
        data: {
          type: "session",
          state: STATE,
          access_token: "at",
          refresh_token: "rt",
        },
      }),
    ).toEqual({ ok: false, reason: "cleared" });
  });
});
