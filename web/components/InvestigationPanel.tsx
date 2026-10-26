"use client";

import { useEffect, useRef, useState } from "react";

import { Panel, Button, ErrorState } from "@/components/ui/primitives";
import { TraceTimeline } from "@/components/TraceTimeline";
import { ReportCard } from "@/components/ReportCard";
import { useInvestigate } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import type { CandidateEvent, InvestigationResult } from "@/lib/api/types";

/**
 * The investigative action, treated as the long task it is.
 *
 * POST /v1/investigations starts a 10–60s LLM investigation on the backend
 * (timeout 90s by default). The client hides the mode difference: it submits and,
 * when the backend is in async mode, polls the job before reading the result
 * (`api.investigate`). So the UI:
 *  - disables the button while in flight;
 *  - shows a progress state with the expected time envelope;
 *  - offers a real cancel (AbortController) that stops the poll and asks the backend
 *    to cancel the job;
 *  - never repeats on its own — this is operator-initiated and single-shot.
 *
 * A completed report can be kept; the backend persists it and it can be
 * reloaded later via GET /v1/investigations/{id} without re-running the LLM.
 */
export function InvestigationPanel({
  candidate,
  onResult,
  existingResult,
}: {
  candidate: CandidateEvent;
  onResult: (r: InvestigationResult | null) => void;
  existingResult: InvestigationResult | null;
}) {
  const mutation = useInvestigate();
  const [elapsed, setElapsed] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  // Reset local result when the selected candidate changes.
  useEffect(() => {
    onResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate.candidate_id]);

  // A ticking clock during the long task: gives honest feedback that work is
  // happening, and shows how close we are to the backend's 90s envelope.
  useEffect(() => {
    if (!mutation.isPending) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(t);
  }, [mutation.isPending]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function start() {
    const controller = new AbortController();
    abortRef.current = controller;
    mutation.mutate(
      { candidateId: candidate.candidate_id, signal: controller.signal },
      { onSuccess: (data) => onResult(data) },
    );
  }

  function cancel() {
    abortRef.current?.abort(new DOMException("用户取消", "AbortError"));
  }

  const result = existingResult;
  const cancelled =
    mutation.isError && (mutation.error as { name?: string })?.name === "AbortError";

  return (
    <div className="space-y-4">
      <Panel title="Agent 调查" subtitle="提交后轮询，最长 90 秒 · 可取消">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          {!mutation.isPending ? (
            <Button variant="primary" onClick={start}>
              ▶ 开始调查
            </Button>
          ) : (
            <>
              <Button variant="danger" onClick={cancel}>
                ■ 取消
              </Button>
              <span className="flex items-center gap-2 text-sm text-ink-muted" role="status" aria-live="polite">
                <Spinner /> 调查中（提交后轮询，最长 90s）… 已用 {elapsed}s
              </span>
            </>
          )}
          {result && !mutation.isPending && (
            <span className="ml-auto text-xs text-ink-faint">
              已获得报告 · 该结果已被后端持久化，可稍后直接载入
            </span>
          )}
        </div>

        {mutation.isPending && (
          <div className="border-t border-rule px-4 py-3">
            <ProgressTrack elapsed={elapsed} />
            <p className="mt-2 text-xs text-ink-faint">
              调查期间会依次调用 4 个受控工具（查证据 / 厂商知识 / 邻居事件 / 近期变更），
              轨迹将在完成后一次性显示。这一步不能自动轮询——后端不可水平扩展。
            </p>
          </div>
        )}

        {mutation.isError && !cancelled && (
          <ErrorState
            className="border-l-0 border-t"
            {...humanize(mutation.error)}
            technical={technicalLine(mutation.error)}
            onRetry={start}
          />
        )}
        {cancelled && (
          <p className="border-t border-rule px-4 py-3 text-sm text-ink-muted">
            已取消。可以重新开始调查。
          </p>
        )}
      </Panel>

      {result && (
        <>
          <Panel title="ReAct 轨迹" subtitle={`${result.trace.length} 步`}>
            <TraceTimeline trace={result.trace} />
          </Panel>

          {result.verdict && (
            <Panel title="一句话结论">
              <div className="space-y-1.5 px-4 py-4">
                <p className="text-base font-semibold text-ink">{result.verdict}</p>
                {result.verdict_detail && (
                  <p className="text-sm text-ink-muted">{result.verdict_detail}</p>
                )}
                <p className="reading pt-1 text-xs text-ink-faint">
                  调用 {result.usage.call_count} 次 · {result.usage.total_tokens} tokens · $
                  {result.usage.cost_usd}
                  {result.usage.cost_estimated ? "（估算）" : ""}
                </p>
              </div>
            </Panel>
          )}

          <ReportCard report={result.report} />
        </>
      )}
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-rule-strong border-t-primary"
    />
  );
}

function ProgressTrack({ elapsed }: { elapsed: number }) {
  const pct = Math.min(100, (elapsed / 90) * 100);
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-paper-sunken"
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="调查进度（按 90 秒时限估算）"
    >
      <div
        className="h-full rounded-full bg-spectrum transition-[width]"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
