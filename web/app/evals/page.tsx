import { Panel, Badge, EmptyState } from "@/components/ui/primitives";

/**
 * Evaluation dashboard — placeholder by design.
 *
 * Eval results live in the backend's `evals/` and the synth / MuJoCo runs;
 * there is no HTTP endpoint for them. We do not fabricate numbers. Instead we
 * state exactly where the real figures come from and how to reproduce them,
 * which is more useful than an empty chart pretending to be data.
 *
 * Server component: it is static content, so it ships no client JS.
 */

const SOURCES = [
  {
    name: "合成数据评估",
    path: "src/data_dog/synth/",
    how: "python -m data_dog.synth.synth_eval",
    note: "基于 Go2 物理参数的 5 类故障注入 + ground truth，Agent 调查对比真值算准确率。",
  },
  {
    name: "MuJoCo 物理仿真评估",
    path: "src/data_dog/mujoco_sim/",
    how: "python -m data_dog.mujoco_sim.mujoco_eval",
    note: "同一批故障的三层注入，用真实物理仿真产出数据并对照 ground truth。",
  },
  {
    name: "整链回放 E2E",
    path: "src/data_dog/e2e/",
    how: "python -m data_dog.e2e.replay_demo",
    note: "P1→P2→Agent→P3 一条命令贯通，回放用例逐条比对。",
  },
  {
    name: "对抗性评估",
    path: "tests/test_adversarial_evals.py",
    how: "pytest tests/test_adversarial_evals.py",
    note: "提示注入 / 幻觉 / 指令遵循等行为化用例。",
  },
];

export default function EvalsPage() {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-ink">评估看板</h1>
        <p className="mt-1 text-sm text-ink-muted">
          离线评估结果目前没有 HTTP 端点。本页标明真实数据来源与复现命令，不呈现编造的数字。
        </p>
      </header>

      <Panel title="数据来源" subtitle="这些结果不经过 /v1/* 暴露，需在服务仓库内运行">
        <div className="px-4 py-4">
          <EmptyState
            title="图表待接入"
            hint="后端补齐评估结果端点后，这里会展示通过率、逐用例对比与历史趋势。当前请按下方命令在仓库内运行，或直接查看对应目录下的产物。"
            icon={<ChartIcon />}
          />
        </div>
      </Panel>

      <Panel title="可复现的评估入口">
        <ul className="divide-y divide-rule">
          {SOURCES.map((s) => (
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

function ChartIcon() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path d="M3 3v18h18" />
      <path d="M7 14l3-4 3 3 4-6" />
    </svg>
  );
}
