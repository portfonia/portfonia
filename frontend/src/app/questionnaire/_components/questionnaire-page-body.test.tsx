import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
// lib/api.ts (imported transitively via QuestionnaireForm) now imports
// logout() from auth-actions.ts, which pulls in the server-only-guarded
// Supabase server client — that throws when bundled into a Client
// Component test unless mocked here (same pattern as
// get-started-menu.test.tsx).
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { QuestionnairePageBody } from "./questionnaire-page-body";

function renderBody(mode?: "onboarding" | "edit") {
  return render(
    <LocaleProvider>
      <QuestionnairePageBody initialContext={null} hadLoadError={false} mode={mode} />
    </LocaleProvider>,
  );
}

describe("QuestionnairePageBody", () => {
  it("defaults to the edit-mode heading/subtitle", () => {
    renderBody();
    expect(screen.getByRole("heading", { name: "Investment style" })).toBeInTheDocument();
    expect(screen.getByText(/skip straight to Save/i)).toBeInTheDocument();
  });

  it("shows the onboarding heading/subtitle in onboarding mode (issue #221 §2.2)", () => {
    renderBody("onboarding");
    expect(
      screen.getByRole("heading", { name: "Let's personalize your reports" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Pre-filled defaults match the system framework/i),
    ).toBeInTheDocument();
  });
});
