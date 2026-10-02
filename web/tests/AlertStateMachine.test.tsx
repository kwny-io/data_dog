import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AlertStateMachine } from "@/components/AlertStateMachine";
import { ALERT_STATE_LABEL, ALERT_TRANSITIONS } from "@/lib/domain";
import type { AlertState } from "@/lib/api/types";

/**
 * The machine is read through the four semantic colour roles, so the tests pin
 * the structure the operator relies on: every state from the backend map is
 * present, each carries its live count, each is a filter control, and the
 * indicator dot means one thing — "this state currently has alerts".
 *
 * Nodes are addressed by their Chinese label (the tooltip leads with it). The
 * raw enum stays out of the visible title on purpose: the console never prints
 * wire vocabulary at the operator's rank.
 */

const byLabel = (state: AlertState) => new RegExp(ALERT_STATE_LABEL[state]);

describe("AlertStateMachine", () => {
  it("renders every state from the backend map", () => {
    render(<AlertStateMachine counts={{ detected: 2, confirmed: 1 }} />);
    for (const state of Object.keys(ALERT_TRANSITIONS) as AlertState[]) {
      expect(screen.getAllByTitle(byLabel(state)).length).toBeGreaterThan(0);
    }
  });

  it("shows live counts", () => {
    render(<AlertStateMachine counts={{ detected: 7, recovering: 3 }} />);
    expect(screen.getByTitle(new RegExp(`${ALERT_STATE_LABEL.detected}：当前 7 条`))).toBeInTheDocument();
    expect(screen.getByTitle(new RegExp(`${ALERT_STATE_LABEL.recovering}：当前 3 条`))).toBeInTheDocument();
  });

  it("renders one node per state, across the rail and the bypass", () => {
    render(<AlertStateMachine />);
    // 6 forward states on the rail + 2 bypass states = 8 controls; every node
    // carries a "当前 N 条" title.
    expect(screen.getAllByTitle(/当前 \d+ 条/)).toHaveLength(8);
  });

  it("enumerates the legal transitions in readable form", () => {
    render(<AlertStateMachine />);
    // Back-edge and branch verbs are the parts a reader cannot infer from the
    // linear rail, so they must be spelled out.
    expect(screen.getByText(/复发/)).toBeInTheDocument();
    expect(screen.getByText(/直接关闭/)).toBeInTheDocument();
  });

  it("calls onSelect when a state is clicked, and null when re-clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AlertStateMachine selected={null} onSelect={onSelect} />);

    await user.click(screen.getByTitle(byLabel("confirmed")));
    expect(onSelect).toHaveBeenCalledWith("confirmed");
  });

  it("marks the selected state as pressed", () => {
    render(<AlertStateMachine selected="escalated" onSelect={() => {}} />);
    const btn = screen.getByTitle(byLabel("escalated"));
    expect(btn).toHaveAttribute("aria-pressed", "true");
  });

  it("fills the status dot only when the state has a non-zero count", () => {
    // The dot reads like an indicator light, so it must mean one: solid = this
    // state currently has alerts. A state with no alerts stays hollow — it is
    // not "the empty step", it is "a state you are not in right now".
    const { container } = render(<AlertStateMachine counts={{ detected: 3 }} />);
    const dot = (state: AlertState) => {
      const btn = [...container.querySelectorAll("button")].find((b) =>
        b.getAttribute("title")?.startsWith(ALERT_STATE_LABEL[state]),
      )!;
      return btn.querySelector<HTMLElement>("[data-dot]")!;
    };

    // jsdom has no Tailwind, so assert the inline style the component sets:
    // a lit dot carries a glow; a hollow one does not.
    expect(dot("detected").style.boxShadow).toBeTruthy();
    expect(dot("closed").style.boxShadow).toBe("");
  });
});
