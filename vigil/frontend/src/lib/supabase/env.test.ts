// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";

import { supabasePublicEnv } from "./env";

const ORIGINAL_URL = process.env.NEXT_PUBLIC_SUPABASE_URL;
const ORIGINAL_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

describe("supabasePublicEnv", () => {
  afterEach(() => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = ORIGINAL_URL;
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = ORIGINAL_KEY;
  });

  it("returns both values when set", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "https://auth.portfonia.com";
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "sb_publishable_abc";
    expect(supabasePublicEnv()).toEqual({
      url: "https://auth.portfonia.com",
      anonKey: "sb_publishable_abc",
    });
  });

  it("throws when a value is missing", () => {
    delete process.env.NEXT_PUBLIC_SUPABASE_URL;
    expect(() => supabasePublicEnv()).toThrow(/NEXT_PUBLIC_SUPABASE_URL/);
  });
});
