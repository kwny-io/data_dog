"use client";

import { useState } from "react";
import Link from "next/link";

import { CandidateList } from "@/components/CandidateList";
import { CandidateDetail } from "@/components/CandidateDetail";
import { InvestigationPanel } from "@/components/InvestigationPanel";
import { Panel, EmptyState } from "@/components/ui/primitives";
import type { CandidateEvent, InvestigationResult, PageParams } from "@/lib/api/types";

/**
 * The investigate console: the core page.
 *
 * Left: the candidate list (paginated, filterable). Right: the selected
 * candidate's detail + evidence, then the Agent investigation. Selecting shows
 * evidence immediately (no LLM wait); investigating is an explicit long task.
 *
 * Deep-linkable via ?candidate=<id> so a URL can be shared or refreshed.
 */
export default function InvestigatePage() {
  const [page, setPage] = useState<PageParams>({ limit: 20, offset: 0 });
  const [selected, setSelected] = useState<CandidateEvent | null>(null);
  const [result, setResult] = useState<InvestigationResult | null>(null);

  // Read the deep-link param without useSearchParams so the page stays a plain
  // client component (and doesn't force a Suspense boundary at build time).
  const [deepLink] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("candidate");
  });

  const activeId = selected?.candidate_id ?? deepLink;

  function select(c: CandidateEvent) {
    setSelected(c);
    const url = new URL(window.location.href);
    url.searchParams.set("candidate", c.candidate_id);
    window.history.replaceState(null, "", url);
  }

  return (
    <div className="apple-ui stagger space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-ink">调查台</h1>
        <p className="mt-1 text-sm text-ink-muted">
          选候选 → 看证据 → 发起 Agent 调查 → 审阅轨迹与报告。调查为同步长任务（最长 90s，可取消）。
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(240px,300px)_1fr]">
        <CandidateList
          page={page}
          onPage={setPage}
          selectedId={activeId}
          onSelect={select}
          filters={{}}
        />

        <div className="min-w-0 space-y-4">
          {!activeId ? (
            <Panel title="候选详情">
              <EmptyState
                title="从左侧选一个候选"
                hint={
                  <>
                    选定后会立即显示候选详情与证据链（无需等待模型）。若没有候选，
                    先去{" "}
                    <Link href="/" className="text-primary hover:underline">
                      仪表盘
                    </Link>{" "}
                    确认服务已收到日志。
                  </>
                }
              />
            </Panel>
          ) : (
            <>
              <CandidateDetail candidateId={activeId} selected={selected ?? undefined} />
              {selected ? (
                <InvestigationPanel
                  candidate={selected}
                  existingResult={result}
                  onResult={setResult}
                />
              ) : (
                <Panel title="Agent 调查">
                  <EmptyState
                    title="候选来自深链，尚未加载完整信息"
                    hint="请从左侧列表点选该候选后再发起调查。"
                  />
                </Panel>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
