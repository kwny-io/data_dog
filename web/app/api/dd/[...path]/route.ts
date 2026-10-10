/**
 * `/api/dd/*` — thin Next adapter over `lib/api/proxy-handler.ts`.
 *
 * The handler itself is runtime-agnostic (plain Request/Response) so it can be
 * unit-tested without Next; this file only wires the catch-all segment.
 */
import type { NextRequest } from "next/server";

import { proxyRequest } from "@/lib/api/proxy-handler";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path: string[] }> };

async function handle(req: NextRequest, ctx: RouteContext): Promise<Response> {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}

export {
  handle as GET,
  handle as POST,
  handle as PUT,
  handle as PATCH,
  handle as DELETE,
  handle as HEAD,
};
