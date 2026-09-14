import "@testing-library/jest-dom/vitest";

process.env.NEXT_PUBLIC_SUPABASE_URL ??= "https://auth.test.local";
process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??= "sb_publishable_test";
process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN ??= "https://portfonia.com";
