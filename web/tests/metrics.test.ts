// @vitest-environment node
import { describe, expect, it } from "vitest";

import { histogramQuantile, metricByLabel, metricTotal, metricValue, parseMetrics } from "@/lib/metrics";

const TEXT = `# HELP data_dog_db_rows rows
# TYPE data_dog_db_rows gauge
data_dog_db_rows{table="candidates"} 137
data_dog_db_rows{table="telemetry_events"} 900
data_dog_candidates_created_total 42
data_dog_http_requests_total{method="POST",path="/v1/telemetry",status="200"} 12
data_dog_http_requests_total{method="GET",path="/v1/candidates",status="200"} 40
data_dog_http_requests_total{method="GET",path="/v1/candidates",status="500"} 3
data_dog_http_request_duration_seconds_bucket{le="0.05"} 5
data_dog_http_request_duration_seconds_bucket{le="0.1"} 10
data_dog_http_request_duration_seconds_bucket{le="+Inf"} 10
`;

describe("prometheus text parsing", () => {
  it("parses samples and ignores comments", () => {
    const s = parseMetrics(TEXT);
    expect(s.find((x) => x.name === "data_dog_candidates_created_total")?.value).toBe(42);
    expect(s.every((x) => !x.name.startsWith("#"))).toBe(true);
  });

  it("reads a labelled value", () => {
    const s = parseMetrics(TEXT);
    expect(metricValue(s, "data_dog_db_rows", { table: "candidates" })).toBe(137);
  });

  it("sums a metric across labels", () => {
    const s = parseMetrics(TEXT);
    expect(metricTotal(s, "data_dog_db_rows")).toBe(1037);
  });

  it("groups by a label", () => {
    const s = parseMetrics(TEXT);
    const byStatus = metricByLabel(s, "data_dog_http_requests_total", "status");
    expect(byStatus["200"]).toBe(52);
    expect(byStatus["500"]).toBe(3);
  });

  it("estimates a quantile from cumulative buckets", () => {
    const s = parseMetrics(TEXT);
    // 95th percentile of 10 requests falls in the 0.1 bucket.
    expect(histogramQuantile(s, "data_dog_http_request_duration_seconds", 0.95)).toBe(0.1);
  });

  it("returns undefined for an unknown metric", () => {
    const s = parseMetrics(TEXT);
    expect(metricValue(s, "nope")).toBeUndefined();
    expect(histogramQuantile(s, "nope", 0.5)).toBeUndefined();
  });
});
