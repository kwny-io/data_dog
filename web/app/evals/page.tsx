import { Panel, Badge, MetricTile, PendingSlot, DataTable, Th, Td, Toolbar } from "@/components/ui/primitives";
import { TechValue } from "@/components/tech";
import { EVAL_CASES, EVAL_SOURCES } from "@/lib/eval-catalog";

/**
 * Evaluation console — full information architecture, honest about which parts
 * are wired.
 *
 * Two kinds of content live here and are treated differently:
 *  - The CASE CATALOG is real: cases are static, defined in `evals/cases.py`,
 *    and need no endpoint. We render them fully.
 *  - The RESULTS (pass/fail, pass-rate, history) have no HTTP endpoint yet, so
 *    they appear as designed pending slots — the shape the data will take, its
 *    exact source, and the command to reproduce it. No invented numbers.
 *
 * Server component: static content, no client JS.
 */

export default function EvalsPage() {
  const total = EVAL_CASES.length;
  const adversarial = EVAL_CASES.filter((c) => c.adversarial).length;

  return (
    <div className="space-y-5">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-ink">评估看板</h1>
          <p className="mt-1 text-sm text-ink-muted">
            离线评估：Agent 行为、物理仿真与整链回放的可复现评测。
          </p>
        </div>
      </header>

      {/* KPI row — real counts where derivable, designed slots where not. */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricTile
          label="评估用例"
          value={total}
          hint="evals/cases.py 中定义的行为化用例"
        />
        <MetricTile
          label="对抗用例"
          value={adversarial}
          tone="warning"
          hint="提示注入 / 身份伪装等鲁棒性用例"
        />
        <MetricTile
          label="整体通过率"
          pending
          hint="需运行评估后产出"
        />
        <MetricTile
          label="最近一次运行"
          pending
          hint="需评估结果端点"
        />
      </section>

      {/* Results region: the real IA, standing by for the endpoint. */}
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1.6fr_1fr]">
        <Panel
          title="逐用例结果"
          subtitle="每个用例的裁决 / 证据召回 / 是否越界"
        >
          <div className="p-4">
            <PendingSlot
              title="逐用例评分"
              will="运行评估后，此处按用例展示：实际裁决 vs 期望集、证据工具是否调用、是否触犯禁用裁决。"
              source="evals/run_evals.py"
              command="python -m evals.run_evals"
            />
          </div>
        </Panel>

        <Panel title="通过率趋势" subtitle="历次运行的滚动通过率">
          <div className="p-4">
            <PendingSlot
              title="历史趋势"
              will="保留的历次运行记录接入后，此处展示通过率随版本的走势。"
            />
          </div>
        </Panel>
      </div>

      {/* Case catalog — REAL. Static configuration, rendered in full. */}
      <Panel
        title="评估用例目录"
        subtitle={
          <>
            静态定义，无需端点即可展示
            <TechValue value="evals/cases.py::CASES" label="来源" className="ml-1.5" />
          </>
        }
      >
        <Toolbar>
          <span className="text-xs text-ink-muted">
            共 {total} 个用例，其中 {adversarial} 个对抗用例
          </span>
          <span className="ml-auto flex items-center gap-3 text-[11px] text-ink-faint">
            <span className="flex items-center gap-1.5">
              <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-warning" />
              对抗鲁棒性
            </span>
            <span className="flex items-center gap-1.5">
              <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-primary" />
              行为正确性
            </span>
          </span>
        </Toolbar>
        <DataTable
          head={
            <>
              <Th>用例</Th>
              <Th>期望裁决（任一）</Th>
              <Th>禁用裁决</Th>
            </>
          }
        >
          {EVAL_CASES.map((c) => (
            <tr key={c.id} className="border-b border-rule align-top">
              <Td className="max-w-md">
                <div className="flex items-center gap-2">
                  <span
                    aria-hidden
                    className={
                      "inline-block h-2 w-2 shrink-0 rounded-full " +
                      (c.adversarial ? "bg-warning" : "bg-primary")
                    }
                  />
                  <span className="text-sm font-medium text-ink">{c.name}</span>
                  {c.adversarial && <Badge tone="warning">对抗</Badge>}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-ink-muted">{c.description}</p>
                <TechValue value={c.id} label="用例标识" className="mt-1" />
              </Td>
              <Td>
                <div className="flex flex-wrap gap-1">
                  {c.allowed.map((a) => (
                    <Badge key={a} tone="suppressed">
                      {a}
                    </Badge>
                  ))}
                </div>
              </Td>
              <Td>
                <div className="flex flex-wrap gap-1">
                  {c.forbidden.map((f) => (
                    <Badge key={f} tone="critical">
                      {f}
                    </Badge>
                  ))}
                </div>
              </Td>
            </tr>
          ))}
        </DataTable>
      </Panel>

      {/* Sources — reproduce commands, kept from the previous page. */}
      <Panel title="可复现的评估入口" subtitle="这些结果不经过 /v1/* 暴露，需在服务仓库内运行">
        <ul className="divide-y divide-rule">
          {EVAL_SOURCES.map((s) => (
            <li key={s.name} className="px-4 py-3.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink">{s.name}</span>
                <Badge tone="suppressed">{s.path}</Badge>
              </div>
              <p className="mt-1 text-xs text-ink-muted">{s.note}</p>
              <code className="reading mt-1.5 block break-all border-l-2 border-rule pl-3 text-xs text-ink">
                {s.how}
              </code>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
