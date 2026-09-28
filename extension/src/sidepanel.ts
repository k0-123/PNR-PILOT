// Side panel: settings, job + website picker, the booking on screen, counters and hotkeys.
// Everything goes through the background worker; the panel keeps no state of its own.

import { fillDots, setDots } from "./dots";
import { HOTKEYS, type Hotkey, type PanelState, type SiteProfile } from "./shared";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

interface CallResult<T> {
  ok: boolean;
  data?: T;
  error?: string;
  status?: number;
}

function bg<T>(msg: Record<string, unknown>): Promise<T> {
  return chrome.runtime.sendMessage(msg) as Promise<T>;
}

const call = <T>(path: string, method = "GET") => bg<CallResult<T>>({ type: "call", method, path });

let state: PanelState | null = null;
let profileOrigins: string[] = [];

// ----------------------------------------------------------------- settings
async function loadSettings(): Promise<boolean> {
  const s = await bg<{ apiUrl: string; token: string }>({ type: "getSettings" });
  $<HTMLInputElement>("api-url").value = s.apiUrl;
  $<HTMLInputElement>("token").value = s.token;
  return !!s.token;
}

async function saveSettings(): Promise<void> {
  const apiUrl = $<HTMLInputElement>("api-url").value.trim().replace(/\/+$/, "");
  await bg({ type: "saveSettings", apiUrl, token: $<HTMLInputElement>("token").value });
  // The app's address needs a host permission (asked once; localhost is allowed already).
  try {
    const origin = new URL(apiUrl).origin + "/*";
    if (!(await chrome.permissions.contains({ origins: [origin] }))) await chrome.permissions.request({ origins: [origin] });
  } catch {
    // invalid URL: the connection test below reports it
  }
  const me = await call<{ name: string }>("/api/me");
  $("settings-msg").textContent = me.ok ? `Connected as ${me.data!.name} ✓` : `✗ ${me.error}`;
  if (me.ok) await loadLists();
}

// -------------------------------------------------------------------- lists
async function loadLists(): Promise<void> {
  const [jobs, profiles] = await Promise.all([
    call<{ id: number; name: string; status: string; remaining: number }[]>("/api/jobs"),
    call<{ name: string; title: string; configured: boolean }[]>("/api/site-profiles"),
  ]);
  const jobSel = $<HTMLSelectElement>("job");
  const profSel = $<HTMLSelectElement>("profile");
  jobSel.replaceChildren();
  profSel.replaceChildren();
  if (!jobs.ok || !profiles.ok) {
    showWarning(jobs.error ?? profiles.error ?? "Can't load jobs");
    return;
  }
  for (const j of jobs.data!) {
    jobSel.add(new Option(`#${j.id} ${j.name} · ${j.remaining} left${j.status === "PAUSED" ? " · PAUSED" : ""}`, String(j.id)));
  }
  if (!jobs.data!.length) jobSel.add(new Option("No job is open for lookups (dashboard → Open for lookups)", ""));
  const saved = (await chrome.storage.local.get("profileName")).profileName as string | undefined;
  for (const p of profiles.data!) {
    profSel.add(new Option(p.title + (p.configured ? "" : " (not set up yet)"), p.name, false, p.name === saved));
  }
  if (state?.jobId) jobSel.value = String(state.jobId);
  await loadProfileOrigins();
}

async function loadProfileOrigins(): Promise<void> {
  const name = $<HTMLSelectElement>("profile").value;
  if (!name) return;
  await chrome.storage.local.set({ profileName: name });
  const r = await call<SiteProfile>(`/api/site-profile/${encodeURIComponent(name)}`);
  profileOrigins = r.ok ? r.data!.match_urls : [];
}

// ---------------------------------------------------------------- actions
async function start(): Promise<void> {
  const jobSel = $<HTMLSelectElement>("job");
  if (!jobSel.value) return showWarning("Pick a job first.");
  // Must run straight from the click: Chrome only asks for permissions during a user gesture.
  if (profileOrigins.length && !(await chrome.permissions.request({ origins: profileOrigins }))) {
    return showWarning("The extension needs permission for the airline website to fill the form.");
  }
  const r = await bg<{ ok: boolean; error?: string }>({
    type: "start",
    jobId: Number(jobSel.value),
    jobName: jobSel.selectedOptions[0]?.text ?? "",
    profileName: $<HTMLSelectElement>("profile").value,
  });
  if (!r.ok) showWarning(r.error ?? "Could not start");
}

function hotkey(key: Hotkey): void {
  void bg({ type: "hotkey", key });
}

/** Download the job's final Excel as it is now (the app builds it fresh). */
async function downloadExcel(): Promise<void> {
  const jobId = state?.jobId ?? Number($<HTMLSelectElement>("job").value);
  if (!jobId) return showWarning("Pick a job first.");
  const msg = $("download-msg");
  msg.textContent = "Preparing…";
  const { apiUrl, token } = await bg<{ apiUrl: string; token: string }>({ type: "getSettings" });
  try {
    const res = await fetch(`${apiUrl}/api/jobs/${jobId}/final.xlsx`, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: `job_${jobId}_final.xlsx` });
    document.body.append(a);
    a.click(); // side panel only (not the airline page): saves the file
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    msg.textContent = "";
  } catch (e) {
    msg.textContent = `✗ ${(e as Error).message}`;
  }
}

// ----------------------------------------------------------------- render
function showWarning(text: string | null, withResume = false): void {
  $("warning").hidden = !text;
  $("warning-text").textContent = text ?? "";
  $("resume").hidden = !withResume;
}

function render(s: PanelState): void {
  state = s;
  document.body.classList.toggle("running", s.running);
  const cur = s.current;
  $("cur-pnr").textContent = cur?.pnr ?? "–";
  $("cur-surname").textContent = cur?.surname ?? "";
  $("cur-pax").replaceChildren(
    ...(cur?.passengers ?? []).map((p) => {
      const li = document.createElement("li");
      li.textContent = `${p.surname ?? "?"}/${p.first_name ?? ""}${p.title ? " " + p.title : ""}`;
      return li;
    }),
  );
  $("status-line").textContent = s.statusLine;
  setDots($("c-done"), String(s.session.done));
  setDots($("c-nf"), String(s.session.notFound));
  setDots($("c-err"), String(s.session.problems));
  $("pause-btn").firstChild!.textContent = s.paused ? "Continue " : "Pause ";
  $<HTMLInputElement>("auto").checked = s.autoSubmit;
  $<HTMLButtonElement>("start").disabled = s.running;
  $<HTMLButtonElement>("stop").disabled = !s.running;
  $("outbox").textContent = s.outboxPending ? `${s.outboxPending} upload(s) waiting…` : "";
  showWarning(s.warning, s.blocked);
}

async function pollProgress(): Promise<void> {
  if (!state?.jobId) return;
  const r = await call<{ pnrs: { left: number }; per_minute: number; eta_minutes: number | null }>(
    `/api/jobs/${state.jobId}/progress`,
  );
  if (!r.ok) return;
  setDots($("c-left"), String(r.data!.pnrs.left));
  setDots($("c-speed"), r.data!.per_minute ? r.data!.per_minute.toFixed(1) : "–");
  const eta = r.data!.eta_minutes;
  setDots($("c-eta"), eta === null ? "–" : eta < 60 ? `${Math.round(eta)} min` : `${(eta / 60).toFixed(1)} h`);
}

// ------------------------------------------------------------------ wiring
chrome.runtime.onMessage.addListener((msg: { type: string; state?: PanelState }) => {
  if (msg.type === "state" && msg.state) render(msg.state);
});

$("toggle-settings").addEventListener("click", () => ($("settings").hidden = !$("settings").hidden));
$("save-settings").addEventListener("click", () => void saveSettings());
$("start").addEventListener("click", () => void start());
$("stop").addEventListener("click", () => void bg({ type: "stop" }));
$("resume").addEventListener("click", () => void bg({ type: "resume" }));
$("refresh").addEventListener("click", () => void loadLists());
$("auto").addEventListener("change", () => void bg({ type: "setAuto", on: $<HTMLInputElement>("auto").checked }));
$("download").addEventListener("click", () => void downloadExcel());
$("profile").addEventListener("change", () => void loadProfileOrigins());
for (const b of document.querySelectorAll<HTMLButtonElement>("[data-key]")) {
  b.addEventListener("click", () => hotkey(b.dataset.key as Hotkey));
}
window.addEventListener("keydown", (e) => {
  if (!e.altKey || e.ctrlKey || e.metaKey) return;
  const key = HOTKEYS[e.key.toLowerCase()];
  if (key) {
    e.preventDefault();
    hotkey(key);
  }
});

(async () => {
  fillDots();
  const hasToken = await loadSettings();
  $("settings").hidden = hasToken;
  render(await bg<PanelState>({ type: "getState" }));
  if (hasToken) await loadLists();
  setInterval(() => void pollProgress(), 5000);
  void pollProgress();
})();
