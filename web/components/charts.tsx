"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface TrendPoint {
  label: string;
  value: number;
}

/** Status-code distribution as horizontal bars. Colours key off the same
 *  semantic tokens the rest of the app uses, so 5xx reads red everywhere. */
export function RequestTrend({ data }: { data: TrendPoint[] }) {
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
          <CartesianGrid horizontal={false} stroke="var(--rule)" />
          <XAxis
            type="number"
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            stroke="var(--rule-strong)"
          />
          <YAxis
            type="category"
            dataKey="label"
            width={48}
            tick={{ fill: "var(--ink-muted)", fontSize: 11, fontFamily: "var(--font-plex-mono)" }}
            stroke="var(--rule-strong)"
          />
          <Tooltip
            cursor={{ fill: "var(--paper-sunken)" }}
            contentStyle={{
              background: "var(--panel)",
              border: "1px solid var(--rule-strong)",
              borderRadius: "var(--radius-token)",
              fontSize: 12,
            }}
            labelStyle={{ color: "var(--ink)" }}
            formatter={(v: number) => [v.toLocaleString("zh-CN"), "请求数"]}
          />
          <Bar dataKey="value" isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.label} fill={statusColor(d.label)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function statusColor(status: string): string {
  if (status.startsWith("2")) return "var(--confirmed)";
  if (status.startsWith("4")) return "var(--warning)";
  if (status.startsWith("5")) return "var(--critical)";
  return "var(--primary)";
}
