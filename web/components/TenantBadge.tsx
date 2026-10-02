"use client";

import { useState } from "react";

import { useTenant } from "@/lib/providers";

/**
 * Shows the active tenant (sent as X-Tenant-Id) and lets an operator switch
 * it. The value comes from config/session, never a URL query param (spec §2),
 * and is stored locally. Multi-tenant login is a backend open question
 * (spec §9.3), so this is a local override, not an auth flow.
 */
export function TenantBadge() {
  const { tenant, setTenant } = useTenant();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(tenant);

  if (editing) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setTenant(draft);
          setEditing(false);
        }}
        className="flex items-center gap-1"
      >
        <label htmlFor="tenant-input" className="sr-only">
          租户 ID
        </label>
        <input
          id="tenant-input"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
          className="reading h-8 w-40 rounded-full border border-rule-strong bg-panel/60 px-3 text-xs text-ink focus:border-spec-violet"
          placeholder="tenant-demo"
        />
      </form>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        setDraft(tenant);
        setEditing(true);
      }}
      title="切换租户（X-Tenant-Id）"
      className="shine inline-flex h-8 items-center gap-1.5 rounded-full border border-rule-strong bg-panel/50 px-2.5 text-xs text-ink-muted transition-colors hover:border-spec-violet/60 hover:text-ink"
    >
      <span className="text-ink-faint">租户</span>
      <span className="reading font-medium text-ink">{tenant}</span>
    </button>
  );
}
