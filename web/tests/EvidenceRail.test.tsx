import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { EvidenceRail } from "@/components/EvidenceRail";
import type { EvidenceSummary } from "@/lib/api/types";

const evidence: EvidenceSummary = {
  key: "temperature_c",
  from_value: 58,
  to_value: 96,
  baseline: null,
  unit: "°C",
  note: "左前髋关节电机",
};

describe("EvidenceRail", () => {
  it("shows the from→to pair and the metric label, not the raw key", () => {
    render(<EvidenceRail evidence={evidence} />);
    // The human label is the primary read; the raw field name is demoted and
    // hidden by default (see components/tech.tsx).
    expect(screen.getByText("温度 °C")).toBeInTheDocument();
    expect(screen.queryByText("temperature_c")).not.toBeInTheDocument();
    // both endpoints rendered (possibly more than once inside end-marks)
    expect(screen.getAllByText("58°C").length).toBeGreaterThan(0);
    expect(screen.getAllByText("96°C").length).toBeGreaterThan(0);
  });

  it("flags over-threshold evidence when a threshold is crossed", () => {
    render(<EvidenceRail evidence={evidence} threshold={90} />);
    expect(screen.getByText("超阈值")).toBeInTheDocument();
  });

  it("does not flag when the value stays under the threshold", () => {
    render(
      <EvidenceRail
        evidence={{ ...evidence, to_value: 61 }}
        threshold={90}
      />,
    );
    expect(screen.queryByText("超阈值")).not.toBeInTheDocument();
  });

  it("expands to reveal the source window and translated component", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <EvidenceRail
        evidence={evidence}
        context={{
          componentRef: "joint_motor.front_left_hip",
          windowStart: "2026-08-13T10:05:00Z",
          windowEnd: "2026-08-13T10:06:00Z",
        }}
      />,
    );

    await user.click(screen.getByRole("button", { expanded: false }));
    // the window appears once expanded…
    expect(container.textContent).toContain("2026-08-13T10:05:00Z");
    // …and the component reads in Chinese, not as a raw ref
    expect(container.textContent).toContain("左前髋关节电机");
    expect(container.textContent).not.toContain("joint_motor.front_left_hip");
  });

  it("is keyboard operable (focus + Enter toggles)", async () => {
    const user = userEvent.setup();
    render(<EvidenceRail evidence={evidence} context={{ componentRef: "x.y" }} />);
    const btn = screen.getByRole("button");
    btn.focus();
    await user.keyboard("{Enter}");
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });
});
