"use client";

import Link from "next/link";
import { useMemo } from "react";

import { Panel, MetricTile, EmptyState, ErrorState, SkeletonRows, Button } from "@/components/ui/primitives";
import { AlertStateMachine } from "@/components/AlertStateMachine";
import { useAlerts, useMetrics, useSummary } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import {
  histogramQuantile,
  metricByLabel,
  metricTotal,
  metricValue,
  parseMetrics,
} from "@/lib/metrics";
import { ALERT_STATE_ORDER, isActiveAlert } from "@/lib/domain";
import type { AlertState } from "@/lib/api/types";
import { RequestTrend, type TrendPoint } from "@/components/charts";

export default function DashboardPage() {
  const metrics = useMetrics();
  const summary = useSummary();
  const alerts = useAlerts({ limit: 500 });

  const samples = useMemo(() => (metrics.data ? parseMetrics(metrics.data) : []), [metrics.data]);

  const candidatesTotal = useMemo(() => {
    if (summary.data) return summary.data.candidates_total;
    return metricValue(samples, "data_dog_db_rows", { table: "candidates" }) ?? 0;
  }, [summary.data, samples]);

  const uptime = summary.data?.uptime_s ?? metricValue(samples, "data_dog_uptime_seconds") ?? 0;
  const p95 = histogramQuantile(samples, "data_dog_http_request_duration_seconds", 0.95);
  const requestsByStatus = useMemo(
    () => metricByLabel(samples, "data_dog_http_requests_total", "status"),
    [samples],
  );

  const alertCounts = useMemo(() => {
    const items = alerts.data?.items ?? [];
    const out: Partial<Record<AlertState, number>> = {};
    for (const a of items) out[a.state] = (out[a.state] ?? 0) + 1;
    return out;
  }, [alerts.data]);

  const activeAlerts = useMemo(
    () =>
      (alerts.data?.items ?? []).filter((a) => isActiveAlert(a.state)).length,
    [alerts.data],
  );

  const outboxPending =
    summary.data?.outbox_pending ??
    metricTotal(samples, "data_dog_outbox_pending_total");

  const trend: TrendPoint[] = useMemo(() => buildTrend(requestsByStatus), [requestsByStatus]);

  return (
    <div className="apple-ui stagger space-y-5">
      <PageHeading />

      {/* Metric row. P95 leads: it is the one number an operator is here to
          read, and it is the only one with a threshold to fail. It sits bare on
          the substrate at display size while the three counts stay as plates,
          so the hierarchy is stated by contrast — not by four equal boxes. */}
      <section className="grid grid-cols-1 items-end gap-5 xl:grid-cols-[auto_1fr]">
        <MetricTile
          hero
          label="请求 P95"
          value={p95 !== undefined ? p95.toFixed(3) : "—"}
          unit="s"
          tone={p95 !== undefined && p95 > 0.5 ? "warning" : "primary"}
          hint={
            <>
              {uptime ? `运行 ${formatUptime(uptime)}` : "自启动以来的 HTTP 耗时"}
              {p95 !== undefined && p95 > 0.5 && " · 超过 0.5s 目标"}
            </>
          }
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <MetricTile
            label="候选事件总数"
            value={candidatesTotal.toLocaleString("zh-CN")}
            hint="库内候选事件行数"
          />
          <MetricTile
            label="活动告警"
            value={activeAlerts}
            tone={activeAlerts > 0 ? "warning" : "confirmed"}
            hint="已检测 / 已确认 / 已升级 / 恢复中"
          />
          <MetricTile
            label="待投递告警"
            value={outboxPending}
            hint="等待投递至外部的告警"
            tone={outboxPending > 0 ? "warning" : "confirmed"}
          />
        </div>
      </section>

      {/* Trend */}
      <Panel
        title="请求量趋势"
        subtitle="按请求状态码分类的累计计数"
        actions={
          metrics.isFetching ? <span className="text-[11px] text-ink-faint">刷新中…</span> : null
        }
      >
        {metrics.isPending ? (
          <SkeletonRows rows={6} />
        ) : metrics.isError ? (
          <ErrorState
            {...humanize(metrics.error)}
            technical={technicalLine(metrics.error)}
            onRetry={() => metrics.refetch()}
          />
        ) : trend.length === 0 ? (
          <EmptyState
            title="还没有请求指标"
            hint="服务启动后尚未记录到任何 HTTP 请求。提交一批设备日志即可看到曲线。"
            action={
              <Button variant="secondary" onClick={() => metrics.refetch()}>
                重新读取指标
              </Button>
            }
          />
        ) : (
          <div className="px-2 py-3">
            <RequestTrend data={trend} />
          </div>
        )}
      </Panel>

      {/* State distribution */}
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1.6fr_1fr]">
        <Panel
          title="告警状态分布"
          subtitle="当前各状态的告警数量 · 点击状态可跳转筛选"
        >
          {alerts.isPending ? (
            <SkeletonRows rows={4} />
          ) : alerts.isError ? (
            <ErrorState
              {...humanize(alerts.error)}
              technical={technicalLine(alerts.error)}
              onRetry={() => alerts.refetch()}
            />
          ) : (alerts.data?.items.length ?? 0) === 0 ? (
            <EmptyState
              title="暂无告警"
              hint="当候选异常经 P3 策略门判定为需上报时，会在这里出现，并携带完整的状态流转历史。"
            />
          ) : (
            <AlertStateMachine
              counts={alertCounts}
              onSelect={() => {
                window.location.href = "/alerts";
              }}
            />
          )}
        </Panel>

        <Panel variant="quiet" title="请求状态码分布" subtitle="各状态码的累计请求数">
          {metrics.isPending ? (
            <SkeletonRows rows={4} />
          ) : Object.keys(requestsByStatus).length === 0 ? (
            <EmptyState title="暂无数据" hint="服务尚未记录请求。" />
          ) : (
            <ul className="divide-y divide-rule">
              {Object.entries(requestsByStatus)
                .sort((a, b) => b[1] - a[1])
                .map(([status, n]) => (
                  <li key={status} className="flex items-center justify-between px-4 py-2.5">
                    <span
                      className={
                        "reading text-sm " +
                        (status.startsWith("2")
                          ? "text-confirmed"
                          : status.startsWith("4")
                            ? "text-warning"
                            : status.startsWith("5")
                              ? "text-critical"
                              : "text-ink")
                      }
                    >
                      {status}
                    </span>
                    <span className="reading text-sm text-ink-muted">
                      {n.toLocaleString("zh-CN")}
                    </span>
                  </li>
                ))}
            </ul>
          )}
        </Panel>
      </div>

      <Panel variant="quiet" title="后续动作" subtitle="不知道从哪开始时的建议入口">
        <div className="flex flex-wrap gap-2.5 p-4">
          <Link
            href="/investigate"
            className="card-link inline-flex items-center gap-2 rounded-[var(--radius-token)] px-3.5 py-2 text-sm font-medium text-primary"
          >
            去调查台选一个候选开始调查 →
          </Link>
          <Link
            href="/alerts"
            className="card-link inline-flex items-center gap-2 rounded-[var(--radius-token)] px-3.5 py-2 text-sm font-medium text-primary"
          >
            查看告警状态机 →
          </Link>
        </div>
      </Panel>
    </div>
  );
}

function PageHeading() {
  return (
    <div className="flex items-end justify-between gap-4">
      <div>
        <h1 className="text-lg font-semibold text-ink">仪表盘</h1>
        <p className="mt-1 text-sm text-ink-muted">一眼看服务健康与业务量。</p>
      </div>
    </div>
  );
}

function formatUptime(s: number): string {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** Turn {status: count} into a small series so the chart has something to plot.
 *  These are cumulative counters, so we present share-of-total rather than a
 *  fake time series — being explicit beats inventing a timeline. */
function buildTrend(byStatus: Record<string, number>): TrendPoint[] {
  return ALERT_STATE_ORDER.length && Object.keys(byStatus).length
    ? Object.entries(byStatus).map(([status, value]) => ({ label: status, value }))
    : [];
}
