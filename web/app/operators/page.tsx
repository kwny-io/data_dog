"use client";

import { useMemo, useState } from "react";

import { Panel, DataTable, Th, Td, EmptyState, SkeletonRows, ErrorState, Badge, Toolbar } from "@/components/ui/primitives";
import { FieldName, TechValue } from "@/components/tech";
import { useCandidates } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import { componentLabel } from "@/lib/domain";
import { ruleName } from "@/lib/vocabulary";
import type { PageParams } from "@/lib/api/types";

/**
 * Operator control plane — read-only, and honest about it.
 *
 * There is no operator registry endpoint yet (P4 is explicitly not built), so
 * we do NOT invent an operator list. What we CAN show truthfully is the
 * provenance actually attached to real candidates: each candidate carries
 * `detector_provenance` ("operator_id:version"), one entry per distinct
 * operator that has fired. That is a real, derivable view — labelled as such.
 */
export default function OperatorsPage() {
  const [page] = useState<PageParams>({ limit: 200, offset: 0 });
  const q = useCandidates({}, page);

  const operators = useMemo(() => {
    const map = new Map<
      string,
      { id: string; version: string; components: Set<string>; count: number; lastWindow: string }
    >();
    for (const c of q.data?.items ?? []) {
      const [id, version] = splitProvenance(c.detector_provenance);
      const key = `${id}:${version}`;
      const entry = map.get(key) ?? {
        id,
        version,
        components: new Set<string>(),
        count: 0,
        lastWindow: "",
      };
      entry.components.add(c.component_ref);
      entry.count += 1;
      if (c.window_end > entry.lastWindow) entry.lastWindow = c.window_end;
      map.set(key, entry);
    }
    return [...map.values()].sort((a, b) => b.count - a.count);
  }, [q.data]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-ink">算子控制面</h1>
        <p className="mt-1 text-sm text-ink-muted">
          只读视图。算子 Registry / 审计 / 灰度（P4）尚未实现，本页不编造数据。
        </p>
      </header>

      <Panel title="实现状态" subtitle="如实说明数据来源边界">
        <div className="space-y-2 px-4 py-4 text-sm text-ink-muted">
          <p className="flex items-start gap-2">
            <Badge tone="warning">P4 未实现</Badge>
            <span>
              没有算子注册表端点，因此无法列出「全部算子」或它们的审计/灰度状态。
            </span>
          </p>
          <p>
            下表来自一个真实可推导的来源：库内候选事件所携带的算子来源标识（格式{" "}
            <FieldName name="detector_provenance" className="mx-0.5" /> 的{" "}
            <FieldName name="operator_id:version" className="mx-0.5" />
            ）。也就是说，这里只显示
            <strong className="text-ink">实际触发过</strong>的算子，而不是全部注册算子。
          </p>
        </div>
      </Panel>

      <Panel
        title="触发过的算子"
        subtitle={q.data ? `来自 ${q.data.count} 条候选的 provenance` : "加载中"}
      >
        {q.isPending ? (
          <SkeletonRows rows={4} />
        ) : q.isError ? (
          <ErrorState {...humanize(q.error)} technical={technicalLine(q.error)} onRetry={() => q.refetch()} />
        ) : operators.length === 0 ? (
          <EmptyState
            title="暂无算子触发记录"
            hint="当候选产生时，其检测算子的 provenance 会记录在这里。先在仪表盘确认服务收到了日志。"
          />
        ) : (
          <>
            <Toolbar>
              <span className="text-xs text-ink-muted">
                {operators.length} 个不同算子被触发
              </span>
            </Toolbar>
            <DataTable
              head={
                <>
                  <Th>算子 ID</Th>
                  <Th>版本</Th>
                  <Th numeric>触发次数</Th>
                  <Th>涉及部件</Th>
                  <Th>最近窗口</Th>
                </>
              }
            >
              {operators.map((op) => (
                <tr key={`${op.id}:${op.version}`} className="border-b border-rule">
                  <Td>
                    <span className="text-sm text-ink">{ruleName(op.id)}</span>
                    <TechValue value={op.id} label="算子标识" className="ml-1.5" />
                  </Td>
                  <Td>
                    <Badge tone="primary">v{op.version}</Badge>
                  </Td>
                  <Td numeric>{op.count}</Td>
                  <Td>
                    <span className="text-xs text-ink-muted">
                      {[...op.components].map((c) => componentLabel(c)).join("、")}
                    </span>
                  </Td>
                  <Td>
                    <span className="reading text-xs text-ink-muted">{op.lastWindow || "—"}</span>
                  </Td>
                </tr>
              ))}
            </DataTable>
          </>
        )}
      </Panel>
    </div>
  );
}

function splitProvenance(p: string): [string, string] {
  if (!p) return ["（无 provenance）", "—"];
  const idx = p.lastIndexOf(":");
  if (idx < 0) return [p, "—"];
  return [p.slice(0, idx), p.slice(idx + 1)];
}
