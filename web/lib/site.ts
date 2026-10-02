/**
 * The site's own origin, in one place.
 *
 * Why this exists: metadata that carries an absolute URL — `metadataBase`,
 * `canonical`, `og:url`, the OG image — must agree with the domain the app is
 * actually served under. The classic launch-day bug is a share card that still
 * points at a build/preview domain (e.g. `*.vercel.app`), so links pasted into
 * WeChat or Slack open the wrong host. The fix is to derive every absolute URL
 * from a single value rather than hand-writing the domain in several files.
 *
 * Resolution order (first non-empty wins):
 *   1. NEXT_PUBLIC_SITE_URL  — explicit override (staging, previews)
 *   2. DD_DOMAIN             — the same variable Caddy uses to issue the TLS
 *                              cert and the compose stack injects. Reusing it
 *                              keeps "the cert domain" and "the OG domain" in
 *                              lockstep — set it once, both follow.
 *   3. DEFAULT_ORIGIN        — the production domain, so a build with no env
 *                              still emits correct absolute URLs.
 *
 * `DD_DOMAIN` is a bare host ("data.rob.com"); a scheme is added if missing.
 * This is server-only data (read at build/render time), so it never reaches the
 * client bundle.
 */

const DEFAULT_ORIGIN = "https://data.rob.com";

function withScheme(value: string): string {
  return /^https?:\/\//i.test(value) ? value : `https://${value}`;
}

export function siteOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_SITE_URL || process.env.DD_DOMAIN;
  if (!raw) return DEFAULT_ORIGIN;
  // Trim a trailing slash so `${origin}/path` never doubles up.
  return withScheme(raw.trim()).replace(/\/+$/, "");
}
