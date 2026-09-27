import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AlertStateMachine } from "@/components/AlertStateMachine";
import { ALERT_TRANSITIONS } from "@/lib/domain";
import type { AlertState } from "@/lib/api/types";

describe("AlertStateMachine", () => {
  it("renders every state from the backend map", () => {
    render(<AlertStateMachine counts={{ detected: 2, confirmed: 1 }} />);
    for (const state of Object.keys(ALERT_TRANSITIONS) as AlertState[]) {
      // the machine is the source of truth: every state must appear
      // (the title renders the state code in full-width parens: （detected）)
      expect(screen.getAllByTitle(new RegExp(`（${state}）`)).length).toBeGreaterThan(0);
    }
  });

  it("shows live counts", () => {
    render(<AlertStateMachine counts={{ detected: 7, recovering: 3 }} />);
    expect(screen.getByTitle(/（detected）.*7/)).toBeInTheDocument();
    expect(screen.getByTitle(/（recovering）.*3/)).toBeInTheDocument();
  });

  it("reports the number of legal transitions it drew", () => {
    const edgeCount = Object.values(ALERT_TRANSITIONS).reduce((n, tos) => n + tos.length, 0);
    render(<AlertStateMachine />);
    expect(screen.getByText(String(edgeCount))).toBeInTheDocument();
  });

  it("calls onSelect when a state is clicked, and null when re-clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AlertStateMachine selected={null} onSelect={onSelect} />);

    await user.click(screen.getByTitle(/（confirmed）/));
    expect(onSelect).toHaveBeenCalledWith("confirmed");
  });

  it("marks the selected state as pressed", () => {
    render(<AlertStateMachine selected="escalated" onSelect={() => {}} />);
    const btn = screen.getByTitle(/（escalated）/);
    expect(btn).toHaveAttribute("aria-pressed", "true");
  });

  it("fills the status dot only when the state has a non-zero count", () => {
    // The dot reads like an indicator light, so it must mean one: solid = this
    // state currently has alerts. A state with no alerts stays hollow — it is
    // not "the empty step", it is "a state you are not in right now".
    const { container } = render(<AlertStateMachine counts={{ detected: 3 }} />);
    const btn = (state: string) =>
      [...container.querySelectorAll("button")].find((b) =>
        b.getAttribute("title")?.includes(`（${state}）`),
      )!;
    const dot = (state: string) => btn(state).querySelector<HTMLElement>("span[aria-hidden]")!;

    // jsdom has no Tailwind, so assert the inline style the component sets:
    // a filled dot carries an inline background; a hollow one does not.
    expect(dot("detected").style.backgroundColor).toBeTruthy();
    expect(dot("closed").style.backgroundColor).toBe("");
  });
});
