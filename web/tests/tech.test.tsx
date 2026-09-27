import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { CopyId, FieldName, TechValue } from "@/components/tech";
import { metricName } from "@/lib/vocabulary";
import { setShowTech } from "@/lib/tech-fields";

/**
 * The "two vocabularies" fix has two halves, and both need pinning:
 *  - raw machine values are demoted/hidden by default (TechValue, FieldName);
 *  - the raw value is still reachable when the operator asks for it.
 */

describe("raw-value demotion", () => {
  it("hides a raw value by default", () => {
    setShowTech(false);
    render(<TechValue value="motor_over_temperature" />);
    expect(screen.queryByText("motor_over_temperature")).not.toBeInTheDocument();
  });

  it("reveals the raw value when the toggle is on", () => {
    setShowTech(true);
    render(<TechValue value="motor_over_temperature" label="事件类型" />);
    expect(screen.getByText("motor_over_temperature")).toBeInTheDocument();
    // and it is labelled so it is not an orphaned token
    expect(screen.getByLabelText("事件类型：motor_over_temperature")).toBeInTheDocument();
  });

  it("hides a raw field name by default and shows it on demand", () => {
    setShowTech(false);
    const { rerender } = render(<FieldName name="missing_evidence" />);
    expect(screen.queryByText("missing_evidence")).not.toBeInTheDocument();

    setShowTech(true);
    rerender(<FieldName name="missing_evidence" />);
    expect(screen.getByText("missing_evidence")).toBeInTheDocument();
  });

  it("renders nothing for a null raw value even when the toggle is on", () => {
    setShowTech(true);
    const { container } = render(<TechValue value={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("CopyId", () => {
  it("shows the truncated id but copies the full one", async () => {
    const user = userEvent.setup();
    const full = "cand_5544dd6677aa00112233";
    render(<CopyId id={full} short={12} />);

    expect(screen.getByText("cand_5544dd6…")).toBeInTheDocument();

    // jsdom has no clipboard API; userEvent installs a stub we can assert on.
    await user.click(screen.getByRole("button"));
    expect(await navigator.clipboard.readText()).toBe(full);
  });

  it("exposes a persistent, always-visible copy control (not hover-only)", () => {
    render(<CopyId id="alert_9f8e7d6c" />);
    // the button exists in the DOM without any hover interaction
    expect(screen.getByRole("button", { name: /复制 ID alert_9f8e7d6c/ })).toBeInTheDocument();
  });
});

describe("metric vocabulary", () => {
  it("translates a metric key with a unit suffix", () => {
    expect(metricName("temperature_c")).toBe("温度 °C");
    expect(metricName("bus_voltage_v")).toBe("母线电压 V");
  });

  it("translates a unitless metric key", () => {
    expect(metricName("sample_count")).toBe("采样数");
  });
});

beforeEach(() => {
  setShowTech(false);
});
