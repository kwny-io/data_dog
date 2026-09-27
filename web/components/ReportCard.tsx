"use client";

import clsx from "clsx";

import { Panel } from "@/components/ui/primitives";
import { DispositionBadge, HypothesisStatusBadge } from "@/components/badges";
import { CopyId, FieldName } from "@/components/tech";
import { TONE_CLASS } from "@/lib/domain";
import type { InvestigationReport } from "@/lib/api/types";

/**
 * The conclusion, with its evidence. Every section maps to a report field, so
 * nothing here is invented: summary, disposition + confidence, hypotheses
 * (each carrying refs), counter-evidence, missing evidence, actions, and the
 * model provenance that produced it.
 */
export function ReportCard({ report }: { report: InvestigationReport }) {
  return (
    <div className="space-y-4">
      <Panel title="结论" subtitle={<CopyId id={report.investigation_id} short={22} />}>
        <div className="space-y-3 px-4 py-4">
          <div className="flex flex-wrap items-center gap-3">
            <DispositionBadge disposition={report.disposition} />
            <ConfidenceBar value={report.confidence} />
          </div>
          {report.summary && <p className="text-sm leading-relaxed text-ink">{report.summary}</p>}
          {report.risk_window && (
            <p className="text-xs text-ink-muted">
              风险窗口：<span className="reading">{report.risk_window}</span>
            </p>
          )}
        </div>
      </Panel>

      <Panel
        title="可能原因"
        subtitle={
          <>
            {report.hypotheses.length} 条假设，逐条列出支持/反证
            <FieldName name="hypotheses" className="ml-1.5" />
          </>
        }
      >
        {report.hypotheses.length === 0 ? (
          <p className="px-4 py-4 text-sm text-ink-muted">未提出假设。</p>
        ) : (
          <ul className="divide-y divide-rule">
            {report.hypotheses.map((h, i) => (
              <li key={i} className="px-4 py-3">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm text-ink">{h.cause}</p>
                  <HypothesisStatusBadge status={h.status} />
                </div>
                {h.evidence_refs.length > 0 && (
                  <RefList label="支持证据" refs={h.evidence_refs} tone="confirmed" />
                )}
                {h.counter_evidence_refs.length > 0 && (
                  <RefList label="反证" refs={h.counter_evidence_refs} tone="critical" />
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Panel
          title="还缺的证据"
          subtitle={<FieldName name="missing_evidence" />}
        >
          {report.missing_evidence.length === 0 ? (
            <p className="px-4 py-4 text-sm text-ink-muted">没有标注缺失证据。</p>
          ) : (
            <ul className="space-y-2 px-4 py-3 text-sm text-ink">
              {report.missing_evidence.map((m, i) => (
                <li key={i} className="flex gap-2">
                  <span aria-hidden className="text-warning">
                    ○
                  </span>
                  <span>{m}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="建议动作" subtitle={<FieldName name="recommended_actions" />}>
          {report.recommended_actions.length === 0 ? (
            <p className="px-4 py-4 text-sm text-ink-muted">没有给出建议动作。</p>
          ) : (
            <ul className="space-y-2 px-4 py-3 text-sm text-ink">
              {report.recommended_actions.map((a, i) => (
                <li key={i} className="flex gap-2">
                  <span aria-hidden className="text-primary">
                    ▸
                  </span>
                  <span>{a}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <Panel
        title="模型来源"
        subtitle={
          <>
            结论由哪个模型、调用多少次产生
            <FieldName name="model_provenance" className="ml-1.5" />
          </>
        }
      >
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-4 py-3 text-sm sm:grid-cols-4">
          <Field label="供应商" value={String(report.model_provenance.provider ?? "—")} />
          <Field label="模型" value={String(report.model_provenance.model ?? "—")} />
          <Field label="调用次数" value={String(report.model_provenance.call_count ?? "—")} />
          <Field label="Token 数" value={String(report.model_provenance.total_tokens ?? "—")} />
        </dl>
      </Panel>
    </div>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <span className="flex items-center gap-2">
      <span className="text-xs text-ink-muted">置信度</span>
      <span className="relative h-2 w-32 overflow-hidden bg-paper-sunken" aria-hidden>
        <span
          className={clsx("absolute inset-y-0 left-0", pct >= 70 ? "bg-confirmed" : pct >= 40 ? "bg-warning" : "bg-critical")}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="reading text-sm font-semibold text-ink">{value.toFixed(2)}</span>
    </span>
  );
}

function RefList({
  label,
  refs,
  tone,
}: {
  label: string;
  refs: string[];
  tone: "confirmed" | "critical";
}) {
  return (
    <div className="mt-1.5">
      <span className={clsx("text-[11px] font-medium", TONE_CLASS[tone].text)}>{label}</span>
      <ul className="mt-0.5 space-y-0.5">
        {refs.map((r, i) => (
          <li key={i} className="reading break-all text-[11px] text-ink-muted">
            {r}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="reading break-all text-sm text-ink">{value}</dd>
    </div>
  );
}
