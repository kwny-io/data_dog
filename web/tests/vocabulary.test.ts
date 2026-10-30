// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  componentName,
  eventTypeName,
  metricName,
  operatorName,
  signalName,
  splitTokens,
} from "@/lib/vocabulary";

describe("vocabulary — graceful degradation", () => {
  it("never returns raw snake_case for a component ref", () => {
    // the exact value the user flagged as "dirty"
    const out = componentName("joint_motor.rear_right_knee");
    expect(out).not.toContain("snake");
    expect(out).not.toMatch(/_/);
    expect(out).toContain("膝");
  });

  it("translates the listed hips and composes the unlisted ones", () => {
    expect(componentName("joint_motor.front_left_hip")).toBe("左前髋关节电机");
    expect(componentName("joint_motor.rear_left_knee")).toContain("左后");
    expect(componentName("joint_motor.front_right_foot")).toContain("足");
  });

  it("handles non-joint components", () => {
    expect(componentName("battery.main")).toContain("电池");
    expect(componentName("controller.main")).toContain("主控");
  });

  it("translates event types", () => {
    // Exact glossary name wins over token composition: the composed form was
    // "电机温度过高", which reads worse than the established "电机过温".
    expect(eventTypeName("motor_over_temperature")).toBe("电机过温");
    expect(eventTypeName("joint_motor_thermal_degradation")).not.toMatch(/_/);
  });

  it("uses the exact name for signals instead of the bare field name", () => {
    // Regression: a candidate's `signal` is `_signal_of(rule_id, field)`, which
    // returns the FIELD NAME when it cannot classify. The console showed
    // "tau_est" as the first line of a candidate card.
    expect(signalName("tau_est")).toBe("估计力矩");
    expect(signalName("temperature_high")).toBe("温度过高");
    // Unlisted values still degrade through composition, never raw snake_case.
    expect(signalName("some_new_signal")).not.toMatch(/_/);
  });

  it("translates rule matches", () => {
    expect(componentName("joint_motor.front_left_hip")).not.toMatch(/_/);
  });

  it("translates operator provenance", () => {
    const out = operatorName("op.temperature_protect:1");
    expect(out).toContain("温度");
    expect(out).toContain("v1");
    expect(out).not.toContain("op.");
  });

  it("does not leak the op. namespace prefix when the id has no version", () => {
    // Regression: the operators table called ruleName() on a bare id like
    // "op.temperature_protect" and rendered "op 温度保护" — the namespace
    // prefix surfaced as a literal token. operatorName() drops it.
    const out = operatorName("op.temperature_protect");
    expect(out).toBe("温度保护算子");
    expect(out).not.toMatch(/^op\b/);
  });

  it("does not leak English glue words into a composed phrase", () => {
    expect(signalName("temperature_rising_under_normalized_load")).not.toMatch(
      /\b(under|of|the|in)\b/,
    );
  });

  it("splits camelCase too", () => {
    expect(splitTokens("motorOverTemp")).toEqual(["motor", "over", "temp"]);
  });

  it("never throws on an empty or odd value", () => {
    expect(componentName("")).toBe("");
    expect(() => componentName("...")).not.toThrow();
    expect(() => eventTypeName("")).not.toThrow();
    expect(() => operatorName("")).not.toThrow();
  });

  it("does not fuse two unrecognised English tokens into a non-word", () => {
    // Regression: joining unknown tokens with no separator turned
    // "sample_foo" into "samplefoo" — worse than the raw key. Unknown tokens
    // must stay separated so a partial translation stays readable.
    const out = signalName("widget_spinner");
    expect(out).not.toBe("widgetspinner");
    expect(out).toMatch(/\s/);
  });

  it("translates a metric key to a label with unit", () => {
    expect(metricName("temperature_c")).toBe("温度 °C");
    expect(metricName("sample_count")).toBe("采样数");
  });
});
