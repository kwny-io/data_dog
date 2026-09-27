"use client";

import { useMemo, useState } from "react";

import {
  Panel,
  DataTable,
  Th,
  Td,
  EmptyState,
  SkeletonRows,
  ErrorState,
  Badge,
  Toolbar,
  PendingSlot,
} from "@/components/ui/primitives";
import { FieldName, TechValue } from "@/components/tech";
import { useCandidates } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import { componentLabel } from "@/lib/domain";
import { operatorName } from "@/lib/vocabulary";
import type { PageParams } from "@/lib/api/types";

/**
 * Operator control plane.
 *
 * Full information architecture. The P4 registry/audit/canary plane has no
 * endpoint yet, so its section is drawn as a designed pending slot — the shape
 * the data will take, its source, and how to run it — rather than described in
 * apologetic prose.
 *
 * What IS real and stays: the provenance actually attached to live candidates.
 * Each candidate carries `detector_provenance` ("operator_id:version"), one per
 * distinct operator that has fired. That is a truthful, derivable view.
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

  const totalFired = operators.reduce((n, o) => n + o.count, 0);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-ink">算子控制面</h1>
        <p className="mt-1 text-sm text-ink-muted">
          观测已触发算子的行为；注册、审计与灰度（P4）接入后在此接管。
        </p>
      </header>

      {/* Derived metric row — real, from live candidate provenance. */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Panel className="px-4 py-3">
          <div className="text-xs font-medium text-ink-muted">已触发算子</div>
          <div className="reading mt-1.5 text-[26px] font-semibold leading-none text-primary">
            {q.isPending ? "—" : operators.length}
          </div>
          <div className="mt-1.5 text-xs text-ink-faint">去重后（id:version）</div>
        </Panel>
        <Panel className="px-4 py-3">
          <div className="text-xs font-medium text-ink-muted">触发总次数</div>
          <div className="reading mt-1.5 text-[26px] font-semibold leading-none text-ink">
            {q.isPending ? "—" : totalFired}
          </div>
          <div className="mt-1.5 text-xs text-ink-faint">候选 provenance 累计</div>
        </Panel>
        <Panel className="px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="text-xs font-medium text-ink-muted">注册算子总数</div>
            <Badge tone="warning">P4 未实现</Badge>
          </div>
          <div className="reading mt-1.5 text-[26px] font-semibold leading-none text-ink-faint">
            —
          </div>
          <div className="mt-1.5 text-xs text-ink-faint">需算子注册表端点</div>
        </Panel>
      </section>

      {/* Real, derivable table. */}
      <Panel
        title="触发过的算子"
        subtitle={
          q.data ? (
            <>
              来自 {q.data.count} 条候选的 provenance
              <FieldName name="detector_provenance" className="ml-1.5" />
            </>
          ) : (
            "加载中"
          )
        }
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
              <span className="ml-auto text-[11px] text-ink-faint">
                仅显示实际触发过的算子，非全部注册算子
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
                    <span className="text-sm text-ink">{operatorName(op.id)}</span>
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

      {/* P4 plane: designed IA, standing by for the endpoint. */}
      <Panel
        title="算子注册与灰度"
        subtitle="P4 平面：注册表 / 审计 / 灰度发布"
      >
        <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
          <PendingSlot
            title="算子注册表"
            will="全部注册算子及其规则、版本与启停状态。当前仅能显示实际触发过的算子。"
            source="需 /v1/operators 端点"
          />
          <PendingSlot
            title="变更审计"
            will="每次算子增删改的操作者、时间与 diff。"
            source="需 /v1/operators/audit 端点"
          />
          <PendingSlot
            title="灰度发布"
            will="按设备/租户切分的灰度比例与生效范围。"
            source="需 /v1/operators/rollout 端点"
          />
        </div>
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
