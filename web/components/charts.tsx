"use client";

import type { SVGProps } from "react";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface TrendPoint {
  label: string;
  value: number;
}

const BAR_SIZE = 18;
const BAR_RADIUS: [number, number, number, number] = [0, 5, 5, 0];

// recharts 把 `background` 声明成 SVGProps，而 SVGProps 的 `radius` 只收单个
// 数字；真正渲染它的是 Rectangle，四角元组是合法的。断言就是补这个缺口。
const GROOVE = {
  fill: "color-mix(in srgb, var(--ink) 9%, transparent)",
  radius: BAR_RADIUS,
} as unknown as SVGProps<SVGPathElement>;

/** Status-code distribution as horizontal bars. Colours key off the same
 *  semantic tokens the rest of the app uses, so 5xx reads red everywhere.
 *
 *  Each bar is a filled tube rather than a flat block: a vertical sheen
 *  gradient across its 18px thickness, a rounded outer end and a faint groove
 *  behind it. The groove is what makes a near-zero count (a single 404) still
 *  legible as a row instead of a hairline the eye skips. */
export function RequestTrend({ data }: { data: TrendPoint[] }) {
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout="vertical"
          barSize={BAR_SIZE}
          margin={{ top: 8, right: 52, bottom: 8, left: 8 }}
        >
          <defs>
            {data.map((d) => {
              const colour = statusColor(d.label);
              return (
                <linearGradient key={d.label} id={gradientId(d.label)} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={`color-mix(in srgb, ${colour} 62%, var(--panel))`} />
                  <stop offset="42%" stopColor={colour} />
                  <stop offset="100%" stopColor={`color-mix(in srgb, ${colour} 78%, var(--panel))`} />
                </linearGradient>
              );
            })}
          </defs>
          <CartesianGrid horizontal={false} stroke="var(--rule)" />
          <XAxis
            type="number"
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            axisLine={{ stroke: "var(--rule)" }}
            tickLine={false}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={48}
            tick={{ fill: "var(--ink-muted)", fontSize: 11, fontFamily: "var(--font-plex-mono)" }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            cursor={{ fill: "color-mix(in srgb, var(--ink) 4%, transparent)" }}
            contentStyle={{
              background: "var(--panel)",
              border: "1px solid var(--rule-strong)",
              borderRadius: "var(--radius-token)",
              fontSize: 12,
            }}
            labelStyle={{ color: "var(--ink)" }}
            formatter={(v: number) => [v.toLocaleString("zh-CN"), "请求数"]}
          />
          <Bar
            dataKey="value"
            isAnimationActive={false}
            radius={BAR_RADIUS}
            background={GROOVE}
          >
            {data.map((d) => (
              <Cell key={d.label} fill={`url(#${gradientId(d.label)})`} />
            ))}
            <LabelList
              dataKey="value"
              position="right"
              offset={9}
              fill="var(--ink-muted)"
              fontSize={11}
              formatter={(v: number) => v.toLocaleString("zh-CN")}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function gradientId(label: string): string {
  return `trend-${label.replace(/[^a-zA-Z0-9_-]/g, "")}`;
}

function statusColor(status: string): string {
  if (status.startsWith("2")) return "var(--confirmed)";
  if (status.startsWith("4")) return "var(--warning)";
  if (status.startsWith("5")) return "var(--critical)";
  return "var(--primary)";
}
