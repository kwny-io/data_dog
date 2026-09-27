import { describe, expect, it } from "vitest";

import {
  ACTIVE_ALERT_STATES,
  ALERT_TRANSITIONS,
  componentLabel,
  formatValue,
  isActiveAlert,
  severityTone,
} from "@/lib/domain";

describe("alert lifecycle map", () => {
  it("matches the backend TRANSITIONS exactly", () => {
    // Pinned to alerting/lifecycle.py::TRANSITIONS. If the backend map changes,
    // this test must change too — that is the point.
    expect(ALERT_TRANSITIONS).toEqual({
      detected: ["confirmed", "suppressed", "rejected"],
      confirmed: ["escalated", "recovering", "closed"],
      escalated: ["recovering", "closed"],
      recovering: ["resolved", "escalated"],
      resolved: ["closed", "detected"],
      suppressed: ["detected"],
      rejected: ["detected"],
      closed: [],
    });
  });

  it("has no transitions out of the terminal state", () => {
    expect(ALERT_TRANSITIONS.closed).toHaveLength(0);
  });

  it("classifies active states", () => {
    expect(isActiveAlert("confirmed")).toBe(true);
    expect(isActiveAlert("closed")).toBe(false);
    expect(ACTIVE_ALERT_STATES).toEqual(["detected", "confirmed", "escalated", "recovering"]);
  });
});

describe("presentation helpers", () => {
  it("maps severity to a semantic tone", () => {
    expect(severityTone("critical")).toBe("critical");
    expect(severityTone("warning")).toBe("warning");
    expect(severityTone("info")).toBe("suppressed");
  });

  it("formats metric values without inventing precision", () => {
    expect(formatValue(96, "°C")).toBe("96°C");
    expect(formatValue(58, "°C")).toBe("58°C");
    expect(formatValue(null, "°C")).toBe("—");
    expect(formatValue(12.5, "A")).toBe("12.5A");
  });

  it("renders a friendly component label and does NOT leak the raw ref", () => {
    // The raw ref is no longer bundled into the label — it is demoted to a
    // separate, toggle-gated rank (see components/tech.tsx). The label itself
    // must read in one language.
    const label = componentLabel("joint_motor.front_left_hip");
    expect(label).toBe("左前髋关节电机");
    expect(label).not.toMatch(/_/);
  });

  it("composes a label for a joint the old lookup table did not cover", () => {
    // The original defect: this ref fell through the closed map and was shown
    // verbatim. Composition must still produce readable Chinese.
    const label = componentLabel("joint_motor.rear_right_knee");
    expect(label).toContain("膝");
    expect(label).not.toMatch(/_/);
  });
});
