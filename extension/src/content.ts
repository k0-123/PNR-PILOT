// Content script on the airline site. It may open the search form's tab, fill the Surname + PNR
// fields, put the cursor in the PNR field, detect result / not-found / block pages, read the page
// text and go back to the search form. A search starts when the staff member presses Enter, or,
// with "Auto-continue" switched on in the side panel, when pressContinue() presses the form's
// Continue button. tests/noSubmit.test.ts fails the build if any other code that could submit or
// click appears in this file.

import {
  autoGapMs,
  HOTKEYS,
  includesAny,
  isSelectorSet,
  pnrOnPage,
  type ContentInstruction,
  type ContentMessage,
  type Lease,
} from "./shared";

declare global {
  interface Window {
    __gdsLookupLoaded?: boolean;
  }
}

let ins: ContentInstruction | null = null;
let filledFor: { pnr: string; surnameEl: Element; pnrEl: Element } | null = null;
let handled = false; // this page load was already reported (result / not found / block)
let scheduled = false;
let lastStatus = "";
const TICK_MS = 500; // re-check even without DOM events (shadow DOM changes aren't observed)
const CAPTURE_WAIT_MS = 15_000; // longest wait for a slow result page to show the PNR / finish loading
const MIN_STABLE_MS = 300; // page text unchanged at least this long = finished loading (profile.settle_ms if longer)
const CAPTURE_TICK_MS = 100;
const HIDDEN_TEXT_MAX = 20_000; // cap for text from collapsed sections
// Separates visible text from collapsed-section text in the upload (app/lookup/page_results.py knows it).
const HIDDEN_MARKER = "=== TEXT FROM COLLAPSED / HIDDEN SECTIONS (may include menus) ===";

/** Diagnostics in the airline page's DevTools console (filter: GDS). No passenger data beyond the PNR. */
function log(...args: unknown[]): void {
  console.info("[GDS]", ...args);
}

/**
 * Visible text of the page, including open shadow roots (web components) and same-origin
 * frames, which document.body.innerText leaves out. Modern booking apps render into these.
 */
function pageText(root: ParentNode = document): string {
  const parts: string[] = [];
  const base = root instanceof Document ? root.body : root;
  if (base instanceof HTMLElement) parts.push(base.innerText);
  for (const el of Array.from(root.querySelectorAll("*"))) {
    const shadow = (el as HTMLElement).shadowRoot;
    if (shadow) {
      for (const child of Array.from(shadow.children)) {
        if (child instanceof HTMLElement) parts.push(child.innerText);
      }
      parts.push(pageText(shadow).trim());
    }
    if (el instanceof HTMLIFrameElement) {
      try {
        const doc = el.contentDocument;
        if (doc?.body) parts.push(pageText(doc));
      } catch {
        // cross-origin frame: not readable
      }
    }
  }
  return parts.filter(Boolean).join("\n");
}

function send(msg: ContentMessage): Promise<ContentInstruction | null> {
  return chrome.runtime.sendMessage(msg).catch(() => null);
}

function status(text: string): void {
  if (text !== lastStatus) {
    lastStatus = text;
    log("status:", text, "·", location.href);
    void send({ type: "status", text });
  }
}

/** On screen for a person: not display:none, not visibility:hidden, not inside a closed <details> etc. */
function rendered(el: Element): boolean {
  return el.checkVisibility({ visibilityProperty: true });
}

/**
 * First element matching the selector, preferring one that is on screen: airline pages often
 * repeat a form (e.g. MH has the same Booking reference + Last name fields under "My booking",
 * "Check-in" and "MHupgrade", only one of them visible).
 */
function find(selector: string | null | undefined): HTMLElement | null {
  if (!isSelectorSet(selector)) return null;
  try {
    const all = Array.from(document.querySelectorAll<HTMLElement>(selector));
    return all.find(rendered) ?? all[0] ?? null;
  } catch {
    return null; // invalid selector in the profile
  }
}

const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "IFRAME"]);

/**
 * Text that innerText leaves out because it isn't shown: collapsed accordions, closed tabs
 * ("Passenger details", "Contact details", e-tickets...). Only the top of each hidden subtree.
 */
function hiddenText(root: Element): string {
  const parts: string[] = [];
  let size = 0;
  for (const el of Array.from(root.querySelectorAll("*"))) {
    if (SKIP_TAGS.has(el.tagName.toUpperCase()) || el.closest("script, style, noscript, template, svg")) continue;
    const parent = el.parentElement;
    if (rendered(el) || !parent || !rendered(parent)) continue;
    const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
    if (text.length < 3) continue;
    parts.push(text);
    size += text.length;
    if (size > HIDDEN_TEXT_MAX) break;
  }
  return [...new Set(parts)].join("\n").slice(0, HIDDEN_TEXT_MAX);
}

/** Set a value the way a framework (React/Angular) notices: native setter + input/change/blur events. */
function setValue(el: HTMLInputElement, value: string): void {
  const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
  const base = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  (own?.set ?? base?.set)?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new FocusEvent("blur"));
  el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
}

function fill(item: Lease, surnameEl: HTMLInputElement, pnrEl: HTMLInputElement): void {
  setValue(surnameEl, item.surname ?? "");
  setValue(pnrEl, item.pnr);
  const target = ins?.profile?.focus_after_fill === "surname" ? surnameEl : pnrEl;
  target.focus();
  try {
    target.setSelectionRange(target.value.length, target.value.length);
  } catch {
    // some input types don't support a selection
  }
  filledFor = { pnr: item.pnr, surnameEl, pnrEl };
}

function apply(next: ContentInstruction | null): void {
  if (!next) return;
  ins = next;
  if (next.navigate && next.navigate !== location.href) {
    location.assign(next.navigate); // back to the search form (not a search: nothing is submitted)
    return;
  }
  if (next.navigate) {
    handled = false; // already on the search form
  }
  schedule();
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function captureText(): string {
  const sel = ins?.profile?.capture.container_selector;
  const container = sel && sel !== "body" ? find(sel) : null;
  return (container ? pageText(container) : pageText()).trim();
}

/** What is uploaded: the visible text, plus collapsed sections under a clear marker. */
function fullCapture(visible: string): string {
  const sel = ins?.profile?.capture.container_selector;
  const container = (sel && sel !== "body" ? find(sel) : null) ?? document.body;
  const hidden = container ? hiddenText(container) : "";
  return hidden ? `${visible}\n\n${HIDDEN_MARKER}\n${hidden}` : visible;
}

async function capture(item: Lease, recapture: boolean): Promise<void> {
  const profile = ins?.profile;
  if (!profile) return;
  let text = captureText();
  // Slow pages show the booking in steps (reference first, then passengers, tickets, contact).
  // Wait until the text stops changing, up to CAPTURE_WAIT_MS. Then decide: a page with another
  // booking = mismatch.
  const until = Date.now() + CAPTURE_WAIT_MS;
  const stableNeeded = Math.max(profile.settle_ms, MIN_STABLE_MS);
  let stableFor = 0;
  while (Date.now() < until && stableFor < stableNeeded) {
    status(pnrOnPage(text, item.pnr) ? "Reading result… waiting for the page to finish loading"
      : `Reading result… waiting for ${item.pnr} to appear`);
    await sleep(CAPTURE_TICK_MS);
    const next = captureText();
    stableFor = next === text ? stableFor + CAPTURE_TICK_MS : 0;
    text = next;
  }
  log("capture:", item.pnr, "visible chars", text.length, "PNR on page", pnrOnPage(text, item.pnr));
  if (profile.pnr_check && !pnrOnPage(text, item.pnr)) {
    const last = ins?.lastDone;
    if (!recapture && last && pnrOnPage(text, last.pnr)) {
      // An earlier result page (browser Back): not a mismatch for the booking now in the queue.
      return status(`This is ${last.pnr} (already captured). Alt+R re-captures it.`);
    }
    status(`⚠ This page doesn't show ${item.pnr}`);
    apply(await send({ type: "mismatch", pnr: item.pnr }));
    return;
  }
  const full = fullCapture(text);
  log("capture: sending", full.length, "chars (", full.length - text.length, "from hidden sections )");
  status(`Captured ✓ ${item.pnr}`);
  apply(await send({ type: "result", pnr: item.pnr, text: full, url: location.href, recapture }));
}

function isResultPage(): boolean {
  const profile = ins?.profile;
  const rd = profile?.result_detect;
  if (!profile || !rd) return false;
  const urlSet = !!rd.url_contains;
  const selSet = isSelectorSet(rd.selector);
  if (rd.pnr_visible) {
    // No search form on the page, and the booking's PNR (or the one just done) is shown.
    const formGone = !find(profile.fields.surname.selector) && !find(profile.fields.pnr.selector);
    const text = pageText();
    const shown = [ins?.item?.pnr, ins?.lastDone?.pnr].some((p) => !!p && pnrOnPage(text, p));
    if (!(formGone && shown)) return false;
    return (!urlSet || location.href.includes(rd.url_contains!)) && (!selSet || !!find(rd.selector));
  }
  if (!urlSet && !selSet) return false;
  return (!urlSet || location.href.includes(rd.url_contains!)) && (!selSet || !!find(rd.selector));
}

function evaluate(): void {
  scheduled = false;
  const profile = ins?.profile;
  if (!ins || !profile) return;
  if (!ins.running) {
    if (!ins.blocked) status(ins.item ? "Stopped" : "Not started");
    return;
  }
  const text = pageText();

  // 1. CAPTCHA / access denied / unusual traffic: stop and let the person handle it.
  const blockWord = includesAny(text, profile.block_detect.text_contains);
  if (blockWord || find(profile.block_detect.selector)) {
    if (!handled) {
      handled = true;
      void send({ type: "blocked", pnr: ins.item?.pnr ?? null, reason: blockWord ?? "security check" }).then(apply);
    }
    return;
  }
  if (ins.paused) return status("Paused (Alt+P to continue)");
  const item = ins.item;
  if (!item) return status("Waiting for bookings…");

  // 2. "Booking not found" message (only after the person searched this booking).
  const nf = profile.not_found_detect;
  if (ins.searched && (find(nf.selector) || includesAny(text, nf.text_contains))) {
    if (!handled) {
      handled = true;
      void send({ type: "notfound", pnr: item.pnr }).then(apply);
    }
    return;
  }

  // 3. Result page: let it finish rendering, then read it (only after the person searched).
  if (ins.searched && isResultPage()) {
    if (!handled) {
      handled = true;
      log("result page detected");
      status("Reading result…");
      void capture(item, !!item.recapture); // waits until the page stops changing (settle_ms)
    }
    return;
  }

  // 3b. Watchdog: a booking that was searched but never became a result / not-found page within
  // row_timeout_ms is skipped, so one stuck page doesn't freeze the whole run (Retry failed can
  // re-run it later). A CAPTCHA / block is handled above and pauses instead — this never skips one.
  const rowTimeout = profile.row_timeout_ms ?? 30_000;
  if (ins.searched && ins.lastSearchAt && rowTimeout > 0 && Date.now() - ins.lastSearchAt > rowTimeout) {
    if (!handled) {
      handled = true;
      log("row timed out after", rowTimeout, "ms:", item.pnr);
      status(`No result in ${Math.round(rowTimeout / 1000)} s — skipping ${item.pnr}`);
      void send({ type: "timeout", pnr: item.pnr }).then(apply);
    }
    return;
  }

  // 4. Search form: fill it and wait for the person to press Enter.
  const surnameEl = find(profile.fields.surname.selector) as HTMLInputElement | null;
  const pnrEl = find(profile.fields.pnr.selector) as HTMLInputElement | null;
  if (surnameEl && pnrEl) {
    const stale = !filledFor || filledFor.pnr !== item.pnr || filledFor.surnameEl !== surnameEl ||
      filledFor.pnrEl !== pnrEl || pnrEl.value !== item.pnr;
    if (stale) fill(item, surnameEl, pnrEl);
    if (!rendered(surnameEl) || !rendered(pnrEl)) {
      if (!ins.searched) openForm(pnrEl);
      return status(`Open the booking form (e.g. the "My booking" tab) · ${item.pnr}`);
    }
    status(scheduleContinue(item, surnameEl, pnrEl) ?? `Ready — press Enter · ${item.pnr}`);
    return;
  }
  // 5. Some other page of the site (still loading, or not the search form yet).
  if (!handled) status(`On ${location.hostname}: waiting for the result or the search form…`);
}

const OPEN_FORM_TRIES = 3;
const OPEN_FORM_RETRY_MS = 1500;
let openFormTries = 0;
let openFormAt = 0;

/** The profile's "open the form" control (e.g. MH's "My booking" tab), if it is safe to press. */
function formOpener(pnrEl: HTMLElement): HTMLElement | null {
  const spec = ins?.profile?.open_form;
  if (!spec || (!isSelectorSet(spec.selector) && !spec.text?.trim())) return null;
  let candidates: HTMLElement[];
  try {
    candidates = isSelectorSet(spec.selector)
      ? Array.from(document.querySelectorAll<HTMLElement>(spec.selector))
      : Array.from(document.querySelectorAll<HTMLElement>("button, [role=tab], a, li, span, div"))
          .filter((el) => el.innerText?.trim() === spec.text!.trim());
  } catch {
    return null; // invalid selector in the profile
  }
  // Text match: a wrapper and its inner label both read "My booking". Take the innermost one;
  // its click reaches the tab's handler on the way up.
  candidates = candidates.filter((el) => !candidates.some((other) => other !== el && el.contains(other)));
  const searchForm = pnrEl.closest("form");
  return candidates.find((el) => {
    if (!rendered(el)) return false;
    // Never anything that could send a form or leave the page.
    if (el.closest("[type=submit]")) return false;
    if (searchForm && searchForm.contains(el)) return false;
    const link = el.closest("a");
    const href = link?.getAttribute("href");
    if (link && href && !href.startsWith("#") && !href.toLowerCase().startsWith("javascript:void")) return false;
    return true;
  }) ?? null;
}

/**
 * Show a search form that sits behind a tab. This only switches the visible tab: the form is
 * not sent and no search starts (the staff member's Enter does that). A few tries per page load.
 */
function openForm(pnrEl: HTMLElement): void {
  if (!ins?.running || ins.paused || openFormTries >= OPEN_FORM_TRIES) return;
  if (Date.now() - openFormAt < OPEN_FORM_RETRY_MS) return;
  const tab = formOpener(pnrEl);
  if (!tab) return;
  openFormTries += 1;
  openFormAt = Date.now();
  log("opening the booking form tab:", tab.innerText?.trim() || tab.tagName);
  tab.click(); // gds-allow: opens form tab, never a search
}

/** The booking on screen has been searched (by the person, or by Auto-continue): tell the background. */
function noteSearched(): void {
  const pnr = ins?.item?.pnr;
  if (!ins?.running || !pnr || !filledFor || filledFor.pnr !== pnr || ins.searched) return;
  ins.searched = true;
  log("search noticed for", pnr);
  void send({ type: "searched", pnr });
}

// ------------------------------------------------------------ Auto-continue
// Switched on by staff in the side panel, and only for sites whose profile allows it. Presses
// the search form's own Continue/submit button once the form is filled and visible, at most once
// per page and at least profile.auto_submit.min_gap_ms after the previous search. The background
// switches it off at the first CAPTCHA / block, so it never searches into one.
const AUTO_SETTLE_MS = 800; // let the site's app take in the filled values first
let autoTimer: ReturnType<typeof setTimeout> | undefined;
let autoFor: string | null = null; // PNR an automatic press is scheduled / done for on this page
let autoGap = 0; // the randomized gap chosen for autoFor (kept so the countdown doesn't jitter)

/** Sites keep the search button disabled until both fields are valid (e.g. MH's "My booking"). */
function disabled(button: HTMLElement): boolean {
  return (button as HTMLButtonElement).disabled || button.getAttribute("aria-disabled") === "true";
}

/** The Continue/submit button of the form that holds the PNR field. */
function continueButton(pnrEl: HTMLElement): HTMLElement | null {
  const form = pnrEl.closest("form");
  if (!form) return null;
  const buttons = Array.from(form.querySelectorAll<HTMLElement>("button, input[type=submit]")).filter(rendered);
  const text = ins?.profile?.auto_submit?.button_text?.trim().toLowerCase();
  const label = (b: HTMLElement) => ((b as HTMLInputElement).value || b.innerText || "").trim().toLowerCase();
  return buttons.find((b) => b.getAttribute("type") === "submit" || (b.tagName === "BUTTON" && !b.getAttribute("type")))
    ?? (text ? buttons.find((b) => label(b) === text) : undefined)
    ?? null;
}

function autoReady(item: Lease, surnameEl: HTMLInputElement, pnrEl: HTMLInputElement): boolean {
  return !!ins?.running && ins.autoSubmit && !ins.paused && !ins.blocked && !ins.searched &&
    ins.item?.pnr === item.pnr && !item.recapture && filledFor?.pnr === item.pnr &&
    pnrEl.value === item.pnr && rendered(surnameEl) && rendered(pnrEl);
}

/** Schedule the automatic Continue for the filled booking; returns the status line to show. */
function scheduleContinue(item: Lease, surnameEl: HTMLInputElement, pnrEl: HTMLInputElement): string | null {
  if (!autoReady(item, surnameEl, pnrEl)) return null;
  const button = continueButton(pnrEl);
  if (!button) return `Auto-continue: no Continue button found — press Enter · ${item.pnr}`;
  if (disabled(button)) return `Auto-continue: waiting for the site to accept the fields · ${item.pnr}`;
  // Pick the (jittered) gap once per booking, then schedule against it. Re-computing it every tick
  // would both re-randomize the wait and make the countdown flicker.
  if (autoFor !== item.pnr) {
    autoFor = item.pnr;
    autoGap = autoGapMs(ins!.profile?.auto_submit);
    const wait = Math.max(AUTO_SETTLE_MS, (ins!.lastSearchAt ?? 0) + autoGap - Date.now());
    clearTimeout(autoTimer);
    autoTimer = setTimeout(() => pressContinue(item, surnameEl, pnrEl), wait);
    log("auto-continue in", wait, "ms for", item.pnr, "(gap", autoGap, "ms)");
    return `Auto-continue in ${Math.ceil(wait / 1000)} s · ${item.pnr} (Alt+P pauses)`;
  }
  const wait = Math.max(0, (ins!.lastSearchAt ?? 0) + autoGap - Date.now());
  return `Auto-continue in ${Math.ceil(wait / 1000)} s · ${item.pnr} (Alt+P pauses)`;
}

function pressContinue(item: Lease, surnameEl: HTMLInputElement, pnrEl: HTMLInputElement): void {
  if (!autoReady(item, surnameEl, pnrEl)) {
    autoFor = null; // conditions changed (paused, switched off, other booking): decide again
    return schedule();
  }
  const button = continueButton(pnrEl);
  if (!button || disabled(button)) {
    autoFor = null; // try again once the site enables the button
    return schedule();
  }
  noteSearched();
  log("auto-continue: pressing", (button as HTMLInputElement).value || button.innerText?.trim(), "for", item.pnr);
  button.click(); // gds-allow: auto-continue, switched on by staff
}

/** Re-evaluate on the next frame (DOM changes come in bursts). */
function schedule(): void {
  if (!scheduled) {
    scheduled = true;
    requestAnimationFrame(evaluate);
  }
}

function main(): void {
  new MutationObserver(schedule).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  setInterval(schedule, TICK_MS);

  chrome.runtime.onMessage.addListener((msg: { type: string; instruction?: ContentInstruction }) => {
    if (msg.type === "instruction" && msg.instruction) apply(msg.instruction);
    if (msg.type === "recapture") recapture();
  });

  // Hotkeys: Alt+N not found, Alt+S skip, Alt+B back, Alt+P pause, Alt+R re-capture.
  // Only Alt+letter combinations are read; every other key (Enter!) goes to the page untouched.
  window.addEventListener(
    "keydown",
    (e) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const key = HOTKEYS[e.key.toLowerCase()];
      if (!key) return;
      e.preventDefault();
      if (key === "recapture") return recapture();
      void send({ type: "hotkey", key }).then(apply);
    },
    true,
  );

  // Notice the person's search: Enter in a field, a click on a button, a form being sent, or the
  // page leaving after the fields were filled. These listeners only read events.
  const searched = noteSearched;
  window.addEventListener("keydown", (e) => e.key === "Enter" && !e.altKey && searched(), true);
  window.addEventListener("pointerup", (e) => {
    if ((e.target as Element | null)?.closest?.("button, [type=submit], [role=button], a")) searched();
  }, true);
  window.addEventListener("submit", searched, true);
  window.addEventListener("pagehide", () => {
    searched();
    if (filledFor && ins?.running) void send({ type: "status", text: "Waiting for page…" });
  });

  log("loaded on", location.href);
  void send({ type: "hello", url: location.href }).then((next) => {
    if (!next) log("no answer from the extension: reload this page (or the extension)");
    else log("instruction:", { running: next.running, searched: next.searched, pnr: next.item?.pnr ?? null });
    apply(next);
  });
}

function recapture(): void {
  const last = ins?.lastDone;
  if (!last || !isResultPage()) return status("Re-capture works on a result page, right after a booking");
  void capture(last, true);
}

if (!window.__gdsLookupLoaded) {
  window.__gdsLookupLoaded = true;
  main();
}
