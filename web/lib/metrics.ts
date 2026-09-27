/**
 * Minimal parser for the backend's Prometheus text exposition (spec §3.1).
 *
 * The dashboard needs a handful of lines; we parse the text format directly
 * rather than depend on `/v1/summary` (which the backend does not expose yet,
 * spec §3.7). When `/v1/summary` lands, prefer it and keep this as a fallback.
 */

export interface Sample {
  name: string;
  labels: Record<string, string>;
  value: number;
}

const SAMPLE_RE =
  /^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]Inf)\s*$/;

function parseLabels(raw: string | undefined): Record<string, string> {
  if (!raw) return {};
  const out: Record<string, string> = {};
  const re = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) {
    const key = m[1];
    const val = m[2];
    if (key !== undefined && val !== undefined) out[key] = val.replace(/\\"/g, '"');
  }
  return out;
}

export function parseMetrics(text: string): Sample[] {
  const samples: Sample[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const m = SAMPLE_RE.exec(trimmed);
    if (!m) continue;
    const [, name, labelRaw, valueRaw] = m;
    if (name === undefined || valueRaw === undefined) continue;
    const value = Number(valueRaw);
    if (Number.isNaN(value)) continue;
    samples.push({ name, labels: parseLabels(labelRaw), value });
  }
  return samples;
}

/** Sum all samples of a metric (ignoring labels). */
export function metricTotal(samples: Sample[], name: string): number {
  return samples
    .filter((s) => s.name === name)
    .reduce((acc, s) => acc + s.value, 0);
}

/** One sample's value, optionally filtered by labels. */
export function metricValue(
  samples: Sample[],
  name: string,
  labels?: Record<string, string>,
): number | undefined {
  const hit = samples.find(
    (s) =>
      s.name === name &&
      (!labels || Object.entries(labels).every(([k, v]) => s.labels[k] === v)),
  );
  return hit?.value;
}

/** Request counts grouped by a label (e.g. status, path). */
export function metricByLabel(
  samples: Sample[],
  name: string,
  label: string,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const s of samples) {
    if (s.name !== name) continue;
    const key = s.labels[label] ?? "unknown";
    out[key] = (out[key] ?? 0) + s.value;
  }
  return out;
}

/** Histogram: estimate a quantile from cumulative bucket counts. Approximate
 *  by design — buckets are coarse; the SLO detail lives in docs/slo.md. */
export function histogramQuantile(
  samples: Sample[],
  baseName: string,
  q: number,
): number | undefined {
  const buckets = samples
    .filter((s) => s.name === `${baseName}_bucket`)
    .map((s) => ({ le: s.labels["le"] === "+Inf" ? Infinity : Number(s.labels["le"]), v: s.value }))
    .filter((b) => !Number.isNaN(b.le))
    .sort((a, b) => a.le - b.le);
  if (buckets.length === 0) return undefined;
  const total = buckets[buckets.length - 1]?.v ?? 0;
  if (total === 0) return undefined;
  const target = q * total;
  for (const b of buckets) {
    if (b.v >= target) return b.le === Infinity ? undefined : b.le;
  }
  return undefined;
}
