// Background service worker: talks to the app's extension API, keeps the prefetched PNR queue,
// the outbox, and tells the content script what to fill. Searches start with the staff member's
// Enter, or, with "Auto-continue" switched on, by the content script pressing Continue (paced by
// the profile's min_gap_ms, switched off at the first CAPTCHA / block).

import { outbox, type OutboxItem } from "./outbox";
import {
  backoffMs,
  matchesPattern,
  MIN_TEXT_CHARS,
  type ContentInstruction,
  type ContentMessage,
  type Hotkey,
  type Lease,
  type PanelState,
  type SiteProfile,
} from "./shared";

const PREFETCH = 20; // PNRs kept claimed locally
const REFILL_BELOW = 10; // claim more when the queue gets this short
const HISTORY = 20; // items remembered for Back (Alt+B)
const SCRIPT_ID = "gds-content";
// Every app call gives up after this. All state changes run one at a time (see `update`), so a
// call that never returns would also stop the airline page's messages from being answered.
const API_TIMEOUT_MS = 15_000;

interface Settings {
  apiUrl: string;
  token: string;
}

interface State extends PanelState {
  profile: SiteProfile | null;
  queue: Lease[]; // queue[0] is the booking on screen now
  history: Lease[]; // most recent first
  doneLocal: string[]; // handled here, maybe not uploaded yet: never re-queue these
  searched: string | null; // PNR the person has searched (Enter / click / page left)
  lastSearchAt: number | null; // when the last search started (paces Auto-continue)
  tabId: number | null;
}

const initialState = (): State => ({
  running: false,
  paused: false,
  jobId: null,
  jobName: null,
  profileName: null,
  profile: null,
  current: null,
  queued: 0,
  statusLine: "Not started",
  warning: null,
  blocked: false,
  outboxPending: 0,
  autoSubmit: false,
  session: { done: 0, notFound: 0, problems: 0, startedAt: null },
  queue: [],
  history: [],
  doneLocal: [],
  searched: null,
  lastSearchAt: null,
  tabId: null,
});

// ------------------------------------------------------------------ storage
async function getSettings(): Promise<Settings> {
  const s = await chrome.storage.local.get(["apiUrl", "token"]);
  return { apiUrl: String(s.apiUrl ?? "http://127.0.0.1:8000").replace(/\/+$/, ""), token: String(s.token ?? "") };
}

// The service worker may be stopped at any time, so state lives in storage.session. All changes
// go through `update`, one at a time.
let chain: Promise<unknown> = Promise.resolve();

async function load(): Promise<State> {
  const { state } = await chrome.storage.session.get("state");
  return { ...initialState(), ...(state as Partial<State> | undefined) };
}

function update<T>(fn: (s: State) => T | Promise<T>): Promise<T> {
  const next = chain.then(async () => {
    const s = await load();
    const out = await fn(s);
    s.current = s.queue[0] ?? null;
    s.queued = s.queue.length;
    await chrome.storage.session.set({ state: s });
    broadcast(s);
    return out;
  });
  chain = next.catch(() => undefined);
  return next;
}

function panelView(s: State): PanelState {
  const { profile: _p, queue: _q, history: _h, doneLocal: _d, searched: _s, lastSearchAt: _l, tabId: _t, ...view } = s;
  return view;
}

function broadcast(s: State): void {
  chrome.runtime.sendMessage({ type: "state", state: panelView(s) }).catch(() => undefined); // panel may be closed
}

// ---------------------------------------------------------------------- API
class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const { apiUrl, token } = await getSettings();
  if (!token) throw new ApiError("No token set: open ⚙ Settings in the side panel", 401);
  let res: Response;
  try {
    res = await fetch(apiUrl + path, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch (e) {
    const timedOut = (e as Error).name === "TimeoutError";
    throw new ApiError(timedOut ? `The app at ${apiUrl} didn't answer in time` : `Can't reach the app at ${apiUrl}`, 0);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(String((data as { detail?: string }).detail ?? res.statusText), res.status);
  return data as T;
}

// -------------------------------------------------------------------- queue
/** Claim more PNRs when the queue runs low (also renews the leases this browser holds). */
async function refill(s: State, force = false): Promise<void> {
  if (!s.running || s.jobId === null || (!force && s.queue.length >= REFILL_BELOW)) return;
  try {
    const { leases } = await api<{ leases: Lease[] }>("POST", `/api/jobs/${s.jobId}/claim?n=${PREFETCH}`);
    const have = new Set(s.queue.map((x) => x.pnr));
    const done = new Set(s.doneLocal);
    for (const l of leases) if (!have.has(l.pnr) && !done.has(l.pnr)) s.queue.push(l);
    if (!s.queue.length) {
      s.statusLine = "All bookings of this job are done 🎉";
      s.running = false;
    }
  } catch (e) {
    const err = e as ApiError;
    s.warning = err.message;
    if (err.status === 409) s.running = false; // job paused / closed on the dashboard
  }
}

/** The booking on screen is finished: remember it and move to the next one. */
function advance(s: State): void {
  s.searched = null;
  const done = s.queue.shift();
  if (done) {
    s.history.unshift({ ...done, recapture: false });
    s.history = s.history.slice(0, HISTORY);
    s.doneLocal = [...s.doneLocal.slice(-500), done.pnr];
  }
}

async function queueUpload(s: State, jobId: number, pnr: string, kind: OutboxItem["kind"], body: Record<string, unknown>) {
  await outbox.add({ jobId, pnr, kind, body, tries: 0, nextAt: Date.now() });
  s.outboxPending = await outbox.count();
  void flushOutbox();
}

// ------------------------------------------------------------------- outbox
let flushing = false;
let flushAgain = false;
let flushTimer: ReturnType<typeof setTimeout> | undefined;

async function flushOutbox(): Promise<void> {
  if (flushing) {
    flushAgain = true; // something was queued meanwhile: run once more when this pass ends
    return;
  }
  flushing = true;
  flushAgain = false;
  let soonest = Infinity;
  let offline: string | null = null;
  try {
    for (const item of await outbox.all()) {
      if (item.nextAt > Date.now()) {
        soonest = Math.min(soonest, item.nextAt);
        continue;
      }
      const path = `/api/jobs/${item.jobId}/lookups/${encodeURIComponent(item.pnr)}/${item.kind}`;
      try {
        await api("POST", path, item.body);
        await outbox.remove(item.id!);
      } catch (e) {
        const err = e as ApiError;
        if (err.status >= 400 && err.status < 500 && err.status !== 401 && err.status !== 408 && err.status !== 429) {
          await outbox.remove(item.id!); // refused for good (e.g. someone else has it): don't retry forever
          await update((s) => {
            s.session.problems += 1;
            s.warning = `${item.pnr}: ${err.message}`;
          });
        } else {
          item.tries += 1;
          item.nextAt = Date.now() + backoffMs(item.tries);
          item.lastError = err.message;
          if (err.status === 0 || err.status >= 500) offline = err.message;
          await outbox.put(item);
          soonest = Math.min(soonest, item.nextAt);
        }
      }
    }
  } finally {
    flushing = false;
  }
  const pending = await outbox.count();
  await update((s) => {
    s.outboxPending = pending;
    if (offline) s.warning = `${offline}: ${pending} capture(s) saved in the browser, retrying automatically`;
    else if (pending === 0 && s.warning?.includes("retrying automatically")) s.warning = null;
  });
  if (flushAgain) return flushOutbox();
  if (flushTimer) clearTimeout(flushTimer);
  if (soonest < Infinity) flushTimer = setTimeout(() => void flushOutbox(), Math.max(200, soonest - Date.now()));
}

// ------------------------------------------------------------ content bridge
function instruction(s: State, extra: Partial<ContentInstruction> = {}): ContentInstruction {
  return {
    running: s.running,
    paused: s.paused,
    blocked: s.blocked,
    profile: s.profile,
    item: s.queue[0] ?? null,
    lastDone: s.history[0] ?? null,
    searched: !!s.queue[0] && s.searched === s.queue[0].pnr,
    autoSubmit: s.autoSubmit && !!s.profile?.auto_submit?.allowed,
    lastSearchAt: s.lastSearchAt,
    ...extra,
  };
}

async function pokeContent(key?: Hotkey): Promise<void> {
  const s = await load();
  if (s.tabId === null) return;
  const ins = instruction(s, key ? toSearchForm(s, key) : {});
  chrome.tabs.sendMessage(s.tabId, { type: "instruction", instruction: ins }).catch(() => undefined);
}

/** Skip / Not found leave the booking on screen: go back to the search form, like a timeout does. */
function toSearchForm(s: State, key: Hotkey): Partial<ContentInstruction> {
  return key === "skip" || key === "notfound" ? { navigate: s.profile?.search_url } : {};
}

async function screenshot(s: State, textLength: number, windowId: number | undefined): Promise<string | null> {
  const mode = s.profile?.capture.screenshot ?? "fallback";
  if (mode === "never" || (mode === "fallback" && textLength >= MIN_TEXT_CHARS) || windowId === undefined) return null;
  try {
    return await chrome.tabs.captureVisibleTab(windowId, { format: "jpeg", quality: 70 });
  } catch {
    return null; // not permitted on this page: the text alone is sent
  }
}

async function onContent(msg: ContentMessage, sender: chrome.runtime.MessageSender): Promise<ContentInstruction> {
  const tab = sender.tab;
  return update(async (s) => {
    if (tab?.id !== undefined && (msg.type === "hello" || s.tabId === null)) s.tabId = tab.id;
    const job = s.jobId;
    const current = s.queue[0];
    switch (msg.type) {
      case "hello":
        return instruction(s);
      case "status":
        s.statusLine = msg.text;
        return instruction(s);
      case "searched":
        if (current?.pnr === msg.pnr) {
          s.searched = msg.pnr;
          s.lastSearchAt = Date.now();
        }
        return instruction(s);
      case "result": {
        if (job === null) return instruction(s);
        // A capture for the booking on screen counts only after it was searched (Re-capture aside).
        if (!msg.recapture && current?.pnr === msg.pnr && s.searched !== msg.pnr) return instruction(s);
        const recapture = msg.recapture || (current?.pnr === msg.pnr && !!current?.recapture);
        const shot = await screenshot(s, msg.text.length, tab?.windowId);
        await queueUpload(s, job, msg.pnr, "capture", { text: msg.text, url: msg.url, screenshot_b64: shot, recapture });
        if (current?.pnr === msg.pnr) {
          advance(s);
          s.session.done += 1;
        }
        s.statusLine = `Captured ✓ ${msg.pnr}`;
        s.warning = null;
        await refill(s);
        return instruction(s, { navigate: s.profile?.search_url });
      }
      case "notfound":
      case "mismatch": {
        if (job === null || current?.pnr !== msg.pnr || s.searched !== msg.pnr) return instruction(s);
        const status = msg.type === "notfound" ? "NOT_FOUND" : "MISMATCH";
        const reason = msg.type === "notfound" ? msg.reason : undefined;
        // The site's own wording goes to the Error column, e.g. "not eligible for retrieval".
        await queueUpload(s, job, msg.pnr, "status", { status, note: reason ? `website says: ${reason}` : undefined });
        advance(s);
        if (msg.type === "notfound") {
          s.session.notFound += 1;
          s.statusLine = `Not found: ${msg.pnr}${reason ? ` (${reason})` : ""}`;
        } else {
          s.session.problems += 1;
          s.warning = `The page showed another booking than ${msg.pnr}. Not saved (marked MISMATCH). Press Back (Alt+B) to try it again.`;
        }
        await refill(s);
        return instruction(s, { navigate: s.profile?.search_url });
      }
      case "timeout": {
        if (job === null || current?.pnr !== msg.pnr || s.searched !== msg.pnr) return instruction(s);
        await queueUpload(s, job, msg.pnr, "status", { status: "SKIPPED", note: "timed out: the page did not load a result in time" });
        advance(s);
        s.session.problems += 1;
        s.statusLine = `Skipped (timed out): ${msg.pnr}`;
        await refill(s);
        return instruction(s, { navigate: s.profile?.search_url });
      }
      case "blocked": {
        if (job !== null && msg.pnr && !s.blocked) {
          await queueUpload(s, job, msg.pnr, "status", { status: "BLOCKED", note: msg.reason });
        }
        s.blocked = true;
        s.running = false;
        s.autoSubmit = false; // never search automatically into a CAPTCHA / block
        s.statusLine = "Stopped: the website is blocking";
        s.warning = `The website showed "${msg.reason}". Auto-continue was switched off and no bookings ` +
          `were lost. Solve it in the browser (or wait a while), then press Resume to carry on.`;
        return instruction(s);
      }
      case "hotkey":
        await hotkey(s, msg.key);
        return instruction(s, toSearchForm(s, msg.key));
    }
  });
}

async function hotkey(s: State, key: Hotkey): Promise<void> {
  const current = s.queue[0];
  switch (key) {
    case "pause":
      s.paused = !s.paused;
      s.statusLine = s.paused ? "Paused (Alt+P to continue)" : "Ready — press Enter";
      return;
    case "notfound":
    case "skip":
      if (!current || s.jobId === null) return;
      await queueUpload(s, s.jobId, current.pnr, "status", { status: key === "notfound" ? "NOT_FOUND" : "SKIPPED" });
      advance(s);
      if (key === "notfound") s.session.notFound += 1;
      s.statusLine = `${key === "notfound" ? "Not found" : "Skipped"}: ${current.pnr}`;
      await refill(s);
      return;
    case "back": {
      const prev = s.history.shift();
      if (!prev) return;
      s.doneLocal = s.doneLocal.filter((p) => p !== prev.pnr);
      s.queue.unshift({ ...prev, recapture: true });
      s.searched = null;
      s.statusLine = `Back to ${prev.pnr}: press Enter to search it again`;
      return;
    }
    case "recapture":
      // handled by the content script (it re-reads the page it shows and sends "result")
      return;
  }
}

// ---------------------------------------------------------- side panel API
type PanelMessage =
  | { type: "getState" }
  | { type: "saveSettings"; apiUrl: string; token: string }
  | { type: "getSettings" }
  | { type: "call"; method: string; path: string }
  | { type: "start"; jobId: number; jobName: string; profileName: string }
  | { type: "stop" }
  | { type: "resume" }
  | { type: "setAuto"; on: boolean }
  | { type: "hotkey"; key: Hotkey };

async function registerContentScript(profile: SiteProfile): Promise<void> {
  const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [SCRIPT_ID] });
  if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
  await chrome.scripting.registerContentScripts([
    { id: SCRIPT_ID, matches: profile.match_urls, js: ["content.js"], runAt: "document_idle", persistAcrossSessions: true },
  ]);
}

async function start(jobId: number, jobName: string, profileName: string): Promise<{ ok: boolean; error?: string }> {
  let profile: SiteProfile;
  try {
    profile = await api<SiteProfile>("GET", `/api/site-profile/${encodeURIComponent(profileName)}`);
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
  if (profile.configured === false) {
    return { ok: false, error: `Site profile "${profile.name}" still has TODO selectors. Set them up first.` };
  }
  const allowed = await chrome.permissions.contains({ origins: profile.match_urls });
  if (!allowed) return { ok: false, error: "Permission for the airline site is missing. Click Start again and allow it." };
  await registerContentScript(profile);
  await update(async (s) => {
    if (s.jobId !== jobId) {
      s.queue = [];
      s.history = [];
      s.doneLocal = [];
      s.session = { done: 0, notFound: 0, problems: 0, startedAt: Date.now() };
    }
    Object.assign(s, { running: true, paused: false, blocked: false, warning: null, jobId, jobName, profileName, profile });
    s.session.startedAt ??= Date.now();
    s.statusLine = "Starting…";
    await refill(s);
  });
  // Use the airline tab that is already open (inject now: it loaded before registration), else open one.
  const tabs = await chrome.tabs.query({ url: profile.match_urls });
  const tab = tabs.find((t) => t.active) ?? tabs[0];
  if (tab?.id !== undefined) {
    await update((s) => void (s.tabId = tab.id!));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] }).catch(() => undefined);
    await pokeContent();
  } else {
    const created = await chrome.tabs.create({ url: profile.search_url });
    await update((s) => void (s.tabId = created.id ?? null));
  }
  return { ok: true };
}

async function onPanel(msg: PanelMessage): Promise<unknown> {
  switch (msg.type) {
    case "getState":
      return panelView(await load());
    case "getSettings":
      return getSettings();
    case "saveSettings":
      await chrome.storage.local.set({ apiUrl: msg.apiUrl.trim(), token: msg.token.trim() });
      return { ok: true };
    case "call":
      try {
        return { ok: true, data: await api("GET" === msg.method ? "GET" : "POST", msg.path) };
      } catch (e) {
        return { ok: false, error: (e as Error).message, status: (e as ApiError).status };
      }
    case "start":
      return start(msg.jobId, msg.jobName, msg.profileName);
    case "stop": {
      const s = await load();
      if (s.jobId !== null) {
        const pnrs = s.queue.map((x) => x.pnr);
        await api("POST", `/api/jobs/${s.jobId}/release`, { pnrs }).catch(() => undefined);
      }
      await update((st) => {
        st.running = false;
        st.queue = [];
        st.statusLine = "Stopped";
      });
      await pokeContent();
      return { ok: true };
    }
    case "resume": {
      const s = await load();
      if (s.jobId === null || !s.profileName) return { ok: false, error: "No job selected" };
      try {
        await api("POST", `/api/jobs/${s.jobId}/resume`);
      } catch (e) {
        if ((e as ApiError).status !== 409) return { ok: false, error: (e as Error).message };
      }
      return start(s.jobId, s.jobName ?? "", s.profileName);
    }
    case "setAuto":
      await update((s) => {
        s.autoSubmit = msg.on;
        s.statusLine = msg.on ? "Auto-continue on" : "Auto-continue off: press Enter to search";
      });
      await pokeContent();
      return { ok: true };
    case "hotkey":
      await update((s) => hotkey(s, msg.key));
      if (msg.key === "recapture") {
        const s = await load();
        if (s.tabId !== null) chrome.tabs.sendMessage(s.tabId, { type: "recapture" }).catch(() => undefined);
      }
      await pokeContent(msg.key);
      return { ok: true };
  }
}

// ------------------------------------------------------------------- wiring
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // The side panel is an extension page (also when opened in a tab); content scripts run on the site.
  const fromContent = !(sender.url ?? "").startsWith(chrome.runtime.getURL(""));
  const handler = fromContent ? onContent(msg as ContentMessage, sender) : onPanel(msg as PanelMessage);
  handler.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
  return true; // async response
});

// Make sure the content script runs on every airline page that finishes loading while a run is on,
// also after a redirect to another subdomain or a result opened in a new tab. The registered
// content script normally does this; injecting again is harmless (the script loads once per page).
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  void load().then((s) => {
    if (!s.running || !s.profile || !s.profile.match_urls.some((m) => matchesPattern(tab.url!, m))) return;
    chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }).catch(() => undefined);
  });
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
});

// Renew leases and retry the outbox even while the worker was asleep.
chrome.alarms.create("renew", { periodInMinutes: 2 });
chrome.alarms.create("flush", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "flush") void flushOutbox();
  if (a.name === "renew") {
    void update((s) => refill(s, true));
  }
});

void flushOutbox();
