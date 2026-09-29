// Types and pure helpers shared by the background worker, the content script and the side panel.

export interface Passenger {
  surname: string | null;
  first_name: string | null;
  title: string | null;
  line_no: string | null;
}

export interface Lease {
  pnr: string;
  surname: string | null;
  passengers: Passenger[];
  lease_until: string;
  recapture?: boolean; // set by Back (Alt+B): the next capture replaces the earlier one
}

export interface SiteProfile {
  name: string;
  search_url: string;
  match_urls: string[];
  fields: { surname: { selector: string }; pnr: { selector: string } };
  focus_after_fill: "surname" | "pnr";
  open_form?: { selector: string | null; text: string | null } | null;
  auto_submit?: { allowed: boolean; min_gap_ms: number; max_gap_ms?: number; button_text: string | null } | null;
  result_detect: { url_contains: string | null; selector: string | null; pnr_visible?: boolean };
  not_found_detect: { selector: string | null; text_contains: string[] };
  block_detect: { selector: string | null; text_contains: string[] };
  capture: { container_selector: string; screenshot: "always" | "fallback" | "never" };
  settle_ms: number;
  pnr_check: boolean;
  row_timeout_ms?: number; // skip a searched booking that hasn't resolved in this long (0 = off)
  configured?: boolean;
}

export type Hotkey = "notfound" | "skip" | "back" | "pause" | "recapture";

export interface PanelState {
  running: boolean;
  paused: boolean;
  jobId: number | null;
  jobName: string | null;
  profileName: string | null;
  current: Lease | null;
  queued: number;
  statusLine: string;
  warning: string | null;
  blocked: boolean;
  outboxPending: number;
  autoSubmit: boolean; // staff switched on "Auto-continue" in the side panel
  session: { done: number; notFound: number; problems: number; startedAt: number | null };
}

// content -> background
export type ContentMessage =
  | { type: "hello"; url: string }
  | { type: "result"; pnr: string; text: string; url: string; recapture?: boolean }
  | { type: "notfound"; pnr: string }
  | { type: "mismatch"; pnr: string }
  | { type: "blocked"; pnr: string | null; reason: string }
  | { type: "timeout"; pnr: string } // searched but no result within row_timeout_ms: skip and move on
  | { type: "status"; text: string }
  | { type: "searched"; pnr: string } // the person pressed Enter / clicked / the page left
  | { type: "hotkey"; key: Hotkey };

// what the background tells the content script to do
export interface ContentInstruction {
  running: boolean;
  paused: boolean;
  blocked: boolean;
  profile: SiteProfile | null;
  item: Lease | null;
  lastDone: Lease | null; // for Re-capture (Alt+R)
  // True once the person has searched the current booking. Until then a "not found" message
  // or a result page on screen belongs to something else and is never recorded.
  searched: boolean;
  // Auto-continue: the extension presses the form's Continue itself (switched on by staff, and
  // only on sites whose profile allows it). lastSearchAt paces searches (profile min_gap_ms).
  autoSubmit: boolean;
  lastSearchAt: number | null;
  navigate?: string; // go to this URL (the search form) now
}

export const HOTKEYS: Record<string, Hotkey> = { n: "notfound", s: "skip", b: "back", p: "pause", r: "recapture" };

// Captured text shorter than this gets a screenshot too when capture.screenshot is "fallback".
export const MIN_TEXT_CHARS = 200;

export function includesAny(haystack: string, needles: string[] | null | undefined): string | null {
  const text = haystack.toLowerCase();
  for (const n of needles ?? []) {
    const needle = n.trim().toLowerCase();
    if (needle && text.includes(needle)) return n;
  }
  return null;
}

/** True when the PNR appears in the captured text as a whole token (not inside a longer code). */
export function pnrOnPage(text: string, pnr: string): boolean {
  const re = new RegExp(`(^|[^A-Z0-9])${pnr.toUpperCase()}([^A-Z0-9]|$)`);
  return re.test(text.toUpperCase());
}

export function isSelectorSet(sel: string | null | undefined): sel is string {
  return !!sel && sel.trim() !== "" && !sel.includes("TODO");
}

/** Chrome match pattern ("https://host/*", "http://127.0.0.1/*") -> does `url` match it? Ports are ignored. */
export function matchesPattern(url: string, pattern: string): boolean {
  const m = /^(\*|https?):\/\/([^/]+)(\/.*)$/.exec(pattern);
  if (!m) return false;
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  const [, scheme, host, path] = m;
  if (scheme !== "*" && u.protocol !== `${scheme}:`) return false;
  if (host !== "*" && !(host.startsWith("*.") ? u.hostname.endsWith(host.slice(1)) : u.hostname === host)) {
    return false;
  }
  const pathRe = new RegExp("^" + path.split("*").map((p) => p.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$");
  return pathRe.test(u.pathname + u.search);
}

/** Exponential backoff with a cap, in ms: 1 s, 2 s, 4 s ... 60 s. */
export function backoffMs(tries: number): number {
  return Math.min(60_000, 1000 * 2 ** Math.max(0, tries - 1));
}

/**
 * The gap Auto-continue waits between searches, in ms: at least min_gap_ms, and up to max_gap_ms
 * when that is set and larger. A random value in [min, max] (not a fixed interval) makes the
 * automation look less like a bot. `rand` is injectable for tests; it defaults to Math.random.
 */
export function autoGapMs(
  auto: { min_gap_ms?: number | null; max_gap_ms?: number | null } | null | undefined,
  rand: () => number = Math.random,
): number {
  const min = Math.max(0, auto?.min_gap_ms ?? 6000);
  const max = Math.max(min, auto?.max_gap_ms ?? 0);
  return Math.round(min + (max - min) * rand());
}
