"use client";

import { useMemo, useState } from "react";

import { Panel, DataTable, Th, Td, Toolbar, Pagination, SkeletonRows, EmptyState, ErrorState, Button, Badge, RowChevron } from "@/components/ui/primitives";
import { AlertStateMachine } from "@/components/AlertStateMachine";
import { AlertStateBadge, SeverityBadge } from "@/components/badges";
import { CopyId, TechValue } from "@/components/tech";
import { useAlerts, useOutbox } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import { ALERT_STATE_ORDER, formatTime, isActiveAlert, ALERT_STATE_LABEL } from "@/lib/domain";
import { componentName, eventTypeName } from "@/lib/vocabulary";
import type { AlertEvent, AlertState, PageParams } from "@/lib/api/types";

export default function AlertsPage() {
  const [page, setPage] = useState<PageParams>({ limit: 50, offset: 0 });
  const [stateFilter, setStateFilter] = useState<AlertState | null>(null);
  const alerts = useAlerts(page);
  const [openId, setOpenId] = useState<string | null>(null);

  const counts = useMemo(() => {
    const out: Partial<Record<AlertState, number>> = {};
    for (const a of alerts.data?.items ?? []) out[a.state] = (out[a.state] ?? 0) + 1;
    return out;
  }, [alerts.data]);

  const rows = useMemo(() => {
    const items = alerts.data?.items ?? [];
    return stateFilter ? items.filter((a) => a.state === stateFilter) : items;
  }, [alerts.data, stateFilter]);

  const open = rows.find((a) => a.event_id === openId) ?? null;

  return (
    <div className="stagger space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-ink">告警</h1>
        <p className="mt-1 text-sm text-ink-muted">
          P3 闭环首次在前端呈现：真实状态机、状态流转历史、以及告警投递状态。
        </p>
      </header>

      <Panel title="告警状态机" subtitle="点击状态筛选下方列表">
        {alerts.isPending ? (
          <SkeletonRows rows={4} />
        ) : alerts.isError ? (
          <ErrorState {...humanize(alerts.error)} technical={technicalLine(alerts.error)} onRetry={() => alerts.refetch()} />
        ) : (
          <AlertStateMachine
            counts={counts}
            selected={stateFilter}
            onSelect={(s) => {
              setStateFilter(s);
              setOpenId(null);
            }}
          />
        )}
      </Panel>

      <Panel
        title="告警事件"
        subtitle={alerts.data ? `共 ${alerts.data.total} 条` : "加载中"}
        actions={
          <Button size="sm" onClick={() => alerts.refetch()} disabled={alerts.isFetching}>
            {alerts.isFetching ? "刷新中…" : "刷新"}
          </Button>
        }
      >
        <Toolbar>
          <span className="text-xs text-ink-muted">状态：</span>
          <FilterChip active={stateFilter === null} onClick={() => setStateFilter(null)}>
            全部
          </FilterChip>
          {ALERT_STATE_ORDER.map((s) => (
            <FilterChip key={s} active={stateFilter === s} onClick={() => setStateFilter(s)}>
              {ALERT_STATE_LABEL[s]}
            </FilterChip>
          ))}
        </Toolbar>

        {alerts.isPending ? (
          <SkeletonRows rows={6} />
        ) : alerts.isError ? (
          <ErrorState {...humanize(alerts.error)} technical={technicalLine(alerts.error)} onRetry={() => alerts.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={stateFilter ? `没有「${ALERT_STATE_LABEL[stateFilter]}」状态的告警` : "暂无告警"}
            hint="告警由 P3 上报闭环在候选经策略门判定后产生。"
          />
        ) : (
          <>
            <DataTable
              head={
                <>
                  <Th sticky>事件 ID</Th>
                  <Th>类型</Th>
                  <Th>状态</Th>
                  <Th>严重度</Th>
                  <Th numeric>置信度</Th>
                  <Th>检测时间</Th>
                  <Th>操作</Th>
                </>
              }
            >
              {rows.map((a) => (
                <tr key={a.event_id} className="group border-b border-rule hover:bg-paper-sunken/60">
                  <Td sticky>
                    <CopyId id={a.event_id} short={16} />
                  </Td>
                  <Td>
                    {a.event_type ? eventTypeName(a.event_type) : "—"}
                    <TechValue value={a.event_type} className="ml-1.5" label="事件类型标识" />
                  </Td>
                  <Td>
                    <AlertStateBadge state={a.state} />
                  </Td>
                  <Td>
                    <SeverityBadge severity={a.severity} />
                  </Td>
                  <Td numeric>{a.confidence.toFixed(2)}</Td>
                  <Td>
                    <span className="reading text-xs text-ink-muted">{formatTime(a.detected_at)}</span>
                  </Td>
                  <Td>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setOpenId(a.event_id)}
                      aria-label={`查看告警 ${a.event_id} 的详情`}
                    >
                      详情
                      <RowChevron className="affordance-chevron" />
                    </Button>
                  </Td>
                </tr>
              ))}
            </DataTable>
            <Pagination
              count={alerts.data.count}
              total={alerts.data.total}
              limit={alerts.data.limit}
              offset={alerts.data.offset}
              hasMore={alerts.data.has_more}
              onChange={(offset) => setPage({ ...page, offset })}
            />
          </>
        )}
      </Panel>

      {open && <AlertDrawer alert={open} onClose={() => setOpenId(null)} />}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        "inline-flex items-center gap-1.5 rounded-[var(--radius-token)] border px-2 py-0.5 text-xs " +
        (active
          ? "border-primary bg-primary-soft font-medium text-primary"
          : "border-rule-strong text-ink-muted hover:bg-paper-sunken hover:text-ink")
      }
    >
      {/* persistent selected marker, not colour alone */}
      {active && <CheckGlyph />}
      {children}
    </button>
  );
}

function CheckGlyph() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" aria-hidden>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

/** Detail drawer: summary, actions, the real transition history as a timeline,
 *  the linked investigation, and the outbox delivery status. */
function AlertDrawer({ alert, onClose }: { alert: AlertEvent; onClose: () => void }) {
  const outbox = useOutbox({ status: undefined }, { limit: 500 });

  const related = useMemo(
    () => (outbox.data?.items ?? []).filter((o) => o.event_id === alert.event_id),
    [outbox.data, alert.event_id],
  );

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label="告警详情">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="drawer-scrim absolute inset-0 bg-ink/30 backdrop-blur-[2px]"
      />
      <div className="drawer-panel glass relative ml-auto h-full w-full max-w-xl overflow-y-auto border-l border-rule-strong">
        <header className="glass sticky top-0 flex items-center justify-between gap-3 border-b border-rule px-5 py-3">
          <span
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-[-1px] h-px bg-gradient-to-r from-transparent via-spec-violet/50 to-transparent"
          />
          <div>
            <div className="flex items-center gap-2">
              <AlertStateBadge state={alert.state} />
              <SeverityBadge severity={alert.severity} />
              {isActiveAlert(alert.state) && (
                <Badge tone="warning" dot filled>
                  进行中
                </Badge>
              )}
            </div>
            <CopyId id={alert.event_id} className="mt-1" />
          </div>
          <Button size="sm" onClick={onClose}>
            关闭
          </Button>
        </header>

        <div className="space-y-5 px-5 py-4">
          <Section title="摘要">
            <p className="text-sm text-ink">{alert.summary || "（无摘要）"}</p>
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <Kv label="设备" value={alert.device_ref} />
              <Kv
                label="部件"
                value={componentName(alert.component_ref)}
                tech={alert.component_ref}
              />
              <Kv
                label="事件类型"
                value={alert.event_type ? eventTypeName(alert.event_type) : "—"}
                tech={alert.event_type ?? undefined}
              />
              <Kv label="修订号" value={String(alert.event_revision)} />
              <Kv label="策略版本" value={alert.policy_version || "—"} />
              <Kv label="证据回执" value={alert.evidence_receipt || "—"} />
            </dl>
          </Section>

          {alert.recommended_actions.length > 0 && (
            <Section title="建议动作">
              <ul className="space-y-1.5 text-sm text-ink">
                {alert.recommended_actions.map((a, i) => (
                  <li key={i} className="flex gap-2">
                    <span aria-hidden className="text-primary">
                      ▸
                    </span>
                    {a}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="状态流转历史" subtitle="每条流转均由后端审计留痕">
            <HistoryTimeline alert={alert} />
          </Section>

          {alert.investigation_id && (
            <Section title="关联调查">
              <p className="text-sm text-ink-muted">
                本次告警来自调查{" "}
                <code className="reading text-ink">{alert.investigation_id}</code>。可在调查台查看其报告。
              </p>
            </Section>
          )}

          <Section title="投递状态" subtitle="与该告警关联的投递记录">
            {outbox.isPending ? (
              <SkeletonRows rows={2} />
            ) : related.length === 0 ? (
              <p className="text-sm text-ink-muted">没有找到与该事件关联的投递记录。</p>
            ) : (
              <ul className="space-y-1.5">
                {related.map((o, i) => (
                  <li key={i} className="flex items-center justify-between text-sm">
                    <code className="reading text-xs">{String(o.id ?? "—")}</code>
                    <span className="reading text-xs text-ink-muted">
                      {String(o.status ?? "—")} · 尝试 {String(o.attempts ?? 0)} 次
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>
      </div>
    </div>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">{title}</h3>
      {subtitle && <p className="mt-0.5 text-[11px] text-ink-faint">{subtitle}</p>}
      <div className="mt-2">{children}</div>
    </section>
  );
}

function Kv({ label, value, tech }: { label: string; value: string; tech?: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="break-all text-ink">{value}</dd>
      {tech && <TechValue value={tech} label={label} className="break-all" />}
    </div>
  );
}

function HistoryTimeline({ alert }: { alert: AlertEvent }) {
  const entries = alert.history ?? [];
  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">没有流转记录（该事件尚未经历状态变化）。</p>;
  }
  return (
    <ol className="relative space-y-3 border-l border-rule pl-4">
      {entries.map((h, i) => (
        <li key={i} className="relative">
          <span
            aria-hidden
            className="absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full border border-rule-strong bg-panel"
          />
          <div className="flex items-center gap-2 text-sm">
            <span className="text-ink-faint">{h.from ? ALERT_STATE_LABEL[h.from] : "—"}</span>
            <span aria-hidden className="text-ink-faint">
              →
            </span>
            <span className="font-medium text-ink">{ALERT_STATE_LABEL[h.to]}</span>
          </div>
          <div className="reading mt-0.5 text-[11px] text-ink-faint">
            {formatTime(h.at)}
            {h.reason ? ` · ${h.reason}` : ""}
          </div>
        </li>
      ))}
    </ol>
  );
}
