"use client";

import { Panel, Badge, SkeletonRows, ErrorState } from "@/components/ui/primitives";
import { SeverityBadge } from "@/components/badges";
import { EvidenceRail } from "@/components/EvidenceRail";
import { EvidenceRefsPanel } from "@/components/RefViewer";
import { CopyId, TechValue } from "@/components/tech";
import { useCandidate, useCandidateEvidence } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import { componentLabel } from "@/lib/domain";
import { metricName, operatorName, ruleName, signalName } from "@/lib/vocabulary";
import type { CandidateEvent } from "@/lib/api/types";

/**
 * What is known about the selected candidate *before* any LLM call: identity,
 * the evidence rails (from→to), and — when the backend exposes it — the
 * evidence endpoint's reference-window metrics and vendor knowledge.
 */
export function CandidateDetail({ candidateId, selected }: { candidateId: string; selected?: CandidateEvent }) {
  // Prefer the cached list row; fetch the single resource so a deep link works.
  const q = useCandidate(candidateId, { enabled: !selected });
  const candidate = selected ?? q.data;

  const evidenceQ = useCandidateEvidence(candidateId);

  if (!candidate && q.isPending) return <Panel title="候选详情"><SkeletonRows rows={5} /></Panel>;
  if (!candidate && q.isError)
    return (
      <Panel title="候选详情">
        <ErrorState {...humanize(q.error)} technical={technicalLine(q.error)} onRetry={() => q.refetch()} />
      </Panel>
    );
  if (!candidate)
    return (
      <Panel title="候选详情">
        <p className="px-4 py-4 text-sm text-ink-muted">未找到该候选。</p>
      </Panel>
    );

  return (
    <div className="space-y-4">
      <Panel
        title="候选详情"
        subtitle={<CopyId id={candidate.candidate_id} short={26} />}
        actions={<SeverityBadge severity={candidate.severity_hint} />}
      >
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-4 text-sm sm:grid-cols-3">
          <Field
            label="部件"
            value={componentLabel(candidate.component_ref)}
            tech={candidate.component_ref}
            wide
          />
          <Field label="设备" value={candidate.device_sn} />
          <Field
            label="信号"
            value={signalName(candidate.signal)}
            tech={candidate.signal}
            wide
          />
          <Field label="异常评分" value={candidate.anomaly_score.toFixed(2)} />
          <Field label="采样数" value={String(candidate.sample_count)} />
          <Field
            label="算子来源"
            value={candidate.detector_provenance ? operatorName(candidate.detector_provenance) : "—"}
            tech={candidate.detector_provenance || undefined}
          />
          <Field label="窗口开始" value={candidate.window_start} />
          <Field label="窗口结束" value={candidate.window_end} />
        </dl>

        {candidate.rule_matches.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 border-t border-rule px-4 py-3">
            {candidate.rule_matches.map((r) => (
              <Badge key={r} tone="primary">
                {ruleName(r)}
                <TechValue value={r} className="ml-1" label="规则标识" />
              </Badge>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="证据" subtitle="每个结论挂一条 from → to 的测量区间；点开可看出处">
        {candidate.evidence.length === 0 ? (
          <p className="px-4 py-4 text-sm text-ink-muted">该候选没有随附结构化证据。</p>
        ) : (
          <ul className="divide-y divide-rule">
            {candidate.evidence.map((e, i) => (
              <li key={`${e.key}-${i}`} className="px-4 py-3.5">
                <EvidenceRail
                  evidence={e}
                  threshold={thresholdFor(e.key)}
                  context={{
                    componentRef: candidate.component_ref,
                    windowStart: candidate.window_start,
                    windowEnd: candidate.window_end,
                  }}
                  tone={candidate.severity_hint === "critical" ? "critical" : "warning"}
                />
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {/* Evidence endpoint (spec §3.5). Absent on the backend today → we show a
       *  labeled placeholder rather than pretending, or hiding it entirely. */}
      <Panel title="Agent 所见证据" subtitle="该候选的实时指标与参考窗口基线对照">
        {evidenceQ.isPending ? (
          <SkeletonRows rows={3} />
        ) : evidenceQ.isError ? (
          <p className="px-4 py-4 text-sm text-ink-muted">
            该端点暂不可用（后端阶段 D 未实现）。候选证据已在上方展示；
            参考窗口与厂商知识将在该端点上线后补充。
          </p>
        ) : (
          <div className="space-y-3 px-4 py-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {Object.entries(evidenceQ.data.metrics).map(([k, v]) => (
                <div key={k}>
                  <div className="text-[11px] text-ink-muted">{metricName(k)}</div>
                  <div className="reading text-sm text-ink">{v}</div>
                  <TechValue value={k} label="字段名" />
                </div>
              ))}
            </div>
            {Object.keys(evidenceQ.data.reference_window_metrics).length > 0 && (
              <div>
                <div className="mb-1 text-[11px] text-ink-faint">参考窗口（基线）</div>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {Object.entries(evidenceQ.data.reference_window_metrics).map(([k, v]) => (
                    <div key={k}>
                      <div className="text-[11px] text-ink-muted">{metricName(k)}</div>
                      <div className="reading text-sm text-ink-muted">{v}</div>
                      <TechValue value={k} label="字段名" />
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Panel>

      <EvidenceRefsPanel candidateId={candidateId} refs={candidate.evidence_refs} />
    </div>
  );
}

function Field({
  label,
  value,
  tech,
  wide = false,
}: {
  label: string;
  value: string;
  /** The raw machine value, demoted below the human label (see components/tech). */
  tech?: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "col-span-2" : undefined}>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="break-all text-sm text-ink">{value}</dd>
      {tech && <TechValue value={tech} label={label} className="mt-0.5 block break-all" />}
    </div>
  );
}

/** Known safe limits. Only motor temperature has a verified vendor figure
 *  (90°C protection, per the operator factory's honest-extraction work);
 *  everything else returns null and the rail simply draws no threshold. */
function thresholdFor(key: string): number | null {
  if (key === "temperature_c") return 90;
  return null;
}
