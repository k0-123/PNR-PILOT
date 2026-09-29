(function() {
	//#region src/shared.ts
	var HOTKEYS = {
		n: "notfound",
		s: "skip",
		b: "back",
		p: "pause",
		r: "recapture"
	};
	function includesAny(haystack, needles) {
		const text = haystack.toLowerCase();
		for (const n of needles ?? []) {
			const needle = n.trim().toLowerCase();
			if (needle && text.includes(needle)) return n;
		}
		return null;
	}
	/** True when the PNR appears in the captured text as a whole token (not inside a longer code). */
	function pnrOnPage(text, pnr) {
		return new RegExp(`(^|[^A-Z0-9])${pnr.toUpperCase()}([^A-Z0-9]|$)`).test(text.toUpperCase());
	}
	function isSelectorSet(sel) {
		return !!sel && sel.trim() !== "" && !sel.includes("TODO");
	}
	/**
	* The gap Auto-continue waits between searches, in ms: at least min_gap_ms, and up to max_gap_ms
	* when that is set and larger. A random value in [min, max] (not a fixed interval) makes the
	* automation look less like a bot. `rand` is injectable for tests; it defaults to Math.random.
	*/
	function autoGapMs(auto, rand = Math.random) {
		const min = Math.max(0, auto?.min_gap_ms ?? 6e3);
		const max = Math.max(min, auto?.max_gap_ms ?? 0);
		return Math.round(min + (max - min) * rand());
	}
	//#endregion
	//#region src/content.ts
	var ins = null;
	var filledFor = null;
	var handled = false;
	var scheduled = false;
	var lastStatus = "";
	var TICK_MS = 500;
	var CAPTURE_WAIT_MS = 15e3;
	var MIN_STABLE_MS = 300;
	var CAPTURE_TICK_MS = 100;
	var HIDDEN_TEXT_MAX = 2e4;
	var HIDDEN_MARKER = "=== TEXT FROM COLLAPSED / HIDDEN SECTIONS (may include menus) ===";
	/** Diagnostics in the airline page's DevTools console (filter: GDS). No passenger data beyond the PNR. */
	function log(...args) {
		console.info("[GDS]", ...args);
	}
	/**
	* Visible text of the page, including open shadow roots (web components) and same-origin
	* frames, which document.body.innerText leaves out. Modern booking apps render into these.
	*/
	function pageText(root = document) {
		const parts = [];
		const base = root instanceof Document ? root.body : root;
		if (base instanceof HTMLElement) parts.push(base.innerText);
		for (const el of Array.from(root.querySelectorAll("*"))) {
			const shadow = el.shadowRoot;
			if (shadow) {
				for (const child of Array.from(shadow.children)) if (child instanceof HTMLElement) parts.push(child.innerText);
				parts.push(pageText(shadow).trim());
			}
			if (el instanceof HTMLIFrameElement) try {
				const doc = el.contentDocument;
				if (doc?.body) parts.push(pageText(doc));
			} catch {}
		}
		return parts.filter(Boolean).join("\n");
	}
	function send(msg) {
		return chrome.runtime.sendMessage(msg).catch(() => null);
	}
	function status(text) {
		if (text !== lastStatus) {
			lastStatus = text;
			log("status:", text, "·", location.href);
			send({
				type: "status",
				text
			});
		}
	}
	/** On screen for a person: not display:none, not visibility:hidden, not inside a closed <details> etc. */
	function rendered(el) {
		return el.checkVisibility({ visibilityProperty: true });
	}
	/**
	* First element matching the selector, preferring one that is on screen: airline pages often
	* repeat a form (e.g. MH has the same Booking reference + Last name fields under "My booking",
	* "Check-in" and "MHupgrade", only one of them visible).
	*/
	function find(selector) {
		if (!isSelectorSet(selector)) return null;
		try {
			const all = Array.from(document.querySelectorAll(selector));
			return all.find(rendered) ?? all[0] ?? null;
		} catch {
			return null;
		}
	}
	var SKIP_TAGS = /* @__PURE__ */ new Set([
		"SCRIPT",
		"STYLE",
		"NOSCRIPT",
		"TEMPLATE",
		"SVG",
		"IFRAME"
	]);
	/**
	* Text that innerText leaves out because it isn't shown: collapsed accordions, closed tabs
	* ("Passenger details", "Contact details", e-tickets...). Only the top of each hidden subtree.
	*/
	function hiddenText(root) {
		const parts = [];
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
	function setValue(el, value) {
		const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
		const base = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
		(own?.set ?? base?.set)?.call(el, value);
		el.dispatchEvent(new Event("input", { bubbles: true }));
		el.dispatchEvent(new Event("change", { bubbles: true }));
		el.dispatchEvent(new FocusEvent("blur"));
		el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
	}
	function fill(item, surnameEl, pnrEl) {
		setValue(surnameEl, item.surname ?? "");
		setValue(pnrEl, item.pnr);
		const target = ins?.profile?.focus_after_fill === "surname" ? surnameEl : pnrEl;
		target.focus();
		try {
			target.setSelectionRange(target.value.length, target.value.length);
		} catch {}
		filledFor = {
			pnr: item.pnr,
			surnameEl,
			pnrEl
		};
	}
	function apply(next) {
		if (!next) return;
		ins = next;
		if (next.navigate && next.navigate !== location.href) {
			location.assign(next.navigate);
			return;
		}
		if (next.navigate) handled = false;
		schedule();
	}
	var sleep = (ms) => new Promise((r) => setTimeout(r, ms));
	function captureText() {
		const sel = ins?.profile?.capture.container_selector;
		const container = sel && sel !== "body" ? find(sel) : null;
		return (container ? pageText(container) : pageText()).trim();
	}
	/** What is uploaded: the visible text, plus collapsed sections under a clear marker. */
	function fullCapture(visible) {
		const sel = ins?.profile?.capture.container_selector;
		const container = (sel && sel !== "body" ? find(sel) : null) ?? document.body;
		const hidden = container ? hiddenText(container) : "";
		return hidden ? `${visible}\n\n${HIDDEN_MARKER}\n${hidden}` : visible;
	}
	async function capture(item, recapture) {
		const profile = ins?.profile;
		if (!profile) return;
		let text = captureText();
		const until = Date.now() + CAPTURE_WAIT_MS;
		const stableNeeded = Math.max(profile.settle_ms, MIN_STABLE_MS);
		let stableFor = 0;
		while (Date.now() < until && stableFor < stableNeeded) {
			status(pnrOnPage(text, item.pnr) ? "Reading result… waiting for the page to finish loading" : `Reading result… waiting for ${item.pnr} to appear`);
			await sleep(CAPTURE_TICK_MS);
			const next = captureText();
			stableFor = next === text ? stableFor + CAPTURE_TICK_MS : 0;
			text = next;
		}
		log("capture:", item.pnr, "visible chars", text.length, "PNR on page", pnrOnPage(text, item.pnr));
		if (profile.pnr_check && !pnrOnPage(text, item.pnr)) {
			const last = ins?.lastDone;
			if (!recapture && last && pnrOnPage(text, last.pnr)) return status(`This is ${last.pnr} (already captured). Alt+R re-captures it.`);
			status(`⚠ This page doesn't show ${item.pnr}`);
			apply(await send({
				type: "mismatch",
				pnr: item.pnr
			}));
			return;
		}
		const full = fullCapture(text);
		log("capture: sending", full.length, "chars (", full.length - text.length, "from hidden sections )");
		status(`Captured ✓ ${item.pnr}`);
		apply(await send({
			type: "result",
			pnr: item.pnr,
			text: full,
			url: location.href,
			recapture
		}));
	}
	function isResultPage() {
		const profile = ins?.profile;
		const rd = profile?.result_detect;
		if (!profile || !rd) return false;
		const urlSet = !!rd.url_contains;
		const selSet = isSelectorSet(rd.selector);
		if (rd.pnr_visible) {
			const formGone = !find(profile.fields.surname.selector) && !find(profile.fields.pnr.selector);
			const text = pageText();
			const shown = [ins?.item?.pnr, ins?.lastDone?.pnr].some((p) => !!p && pnrOnPage(text, p));
			if (!(formGone && shown)) return false;
			return (!urlSet || location.href.includes(rd.url_contains)) && (!selSet || !!find(rd.selector));
		}
		if (!urlSet && !selSet) return false;
		return (!urlSet || location.href.includes(rd.url_contains)) && (!selSet || !!find(rd.selector));
	}
	function evaluate() {
		scheduled = false;
		const profile = ins?.profile;
		if (!ins || !profile) return;
		if (!ins.running) {
			if (!ins.blocked) status(ins.item ? "Stopped" : "Not started");
			return;
		}
		const text = pageText();
		const blockWord = includesAny(text, profile.block_detect.text_contains);
		if (blockWord || find(profile.block_detect.selector)) {
			if (!handled) {
				handled = true;
				send({
					type: "blocked",
					pnr: ins.item?.pnr ?? null,
					reason: blockWord ?? "security check"
				}).then(apply);
			}
			return;
		}
		if (ins.paused) return status("Paused (Alt+P to continue)");
		const item = ins.item;
		if (!item) return status("Waiting for bookings…");
		const nf = profile.not_found_detect;
		if (ins.searched && (find(nf.selector) || includesAny(text, nf.text_contains))) {
			if (!handled) {
				handled = true;
				send({
					type: "notfound",
					pnr: item.pnr
				}).then(apply);
			}
			return;
		}
		if (ins.searched && isResultPage()) {
			if (!handled) {
				handled = true;
				log("result page detected");
				status("Reading result…");
				capture(item, !!item.recapture);
			}
			return;
		}
		const rowTimeout = profile.row_timeout_ms ?? 3e4;
		if (ins.searched && ins.lastSearchAt && rowTimeout > 0 && Date.now() - ins.lastSearchAt > rowTimeout) {
			if (!handled) {
				handled = true;
				log("row timed out after", rowTimeout, "ms:", item.pnr);
				status(`No result in ${Math.round(rowTimeout / 1e3)} s — skipping ${item.pnr}`);
				send({
					type: "timeout",
					pnr: item.pnr
				}).then(apply);
			}
			return;
		}
		const surnameEl = find(profile.fields.surname.selector);
		const pnrEl = find(profile.fields.pnr.selector);
		if (surnameEl && pnrEl) {
			if (!filledFor || filledFor.pnr !== item.pnr || filledFor.surnameEl !== surnameEl || filledFor.pnrEl !== pnrEl || pnrEl.value !== item.pnr) fill(item, surnameEl, pnrEl);
			if (!rendered(surnameEl) || !rendered(pnrEl)) {
				if (!ins.searched) openForm(pnrEl);
				return status(`Open the booking form (e.g. the "My booking" tab) · ${item.pnr}`);
			}
			status(scheduleContinue(item, surnameEl, pnrEl) ?? `Ready — press Enter · ${item.pnr}`);
			return;
		}
		if (!handled) status(`On ${location.hostname}: waiting for the result or the search form…`);
	}
	var OPEN_FORM_TRIES = 3;
	var OPEN_FORM_RETRY_MS = 1500;
	var openFormTries = 0;
	var openFormAt = 0;
	/** The profile's "open the form" control (e.g. MH's "My booking" tab), if it is safe to press. */
	function formOpener(pnrEl) {
		const spec = ins?.profile?.open_form;
		if (!spec || !isSelectorSet(spec.selector) && !spec.text?.trim()) return null;
		let candidates;
		try {
			candidates = isSelectorSet(spec.selector) ? Array.from(document.querySelectorAll(spec.selector)) : Array.from(document.querySelectorAll("button, [role=tab], a, li, span, div")).filter((el) => el.innerText?.trim() === spec.text.trim());
		} catch {
			return null;
		}
		candidates = candidates.filter((el) => !candidates.some((other) => other !== el && el.contains(other)));
		const searchForm = pnrEl.closest("form");
		return candidates.find((el) => {
			if (!rendered(el)) return false;
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
	function openForm(pnrEl) {
		if (!ins?.running || ins.paused || openFormTries >= OPEN_FORM_TRIES) return;
		if (Date.now() - openFormAt < OPEN_FORM_RETRY_MS) return;
		const tab = formOpener(pnrEl);
		if (!tab) return;
		openFormTries += 1;
		openFormAt = Date.now();
		log("opening the booking form tab:", tab.innerText?.trim() || tab.tagName);
		tab.click();
	}
	/** The booking on screen has been searched (by the person, or by Auto-continue): tell the background. */
	function noteSearched() {
		const pnr = ins?.item?.pnr;
		if (!ins?.running || !pnr || !filledFor || filledFor.pnr !== pnr || ins.searched) return;
		ins.searched = true;
		log("search noticed for", pnr);
		send({
			type: "searched",
			pnr
		});
	}
	var AUTO_SETTLE_MS = 800;
	var autoTimer;
	var autoFor = null;
	var autoGap = 0;
	/** Sites keep the search button disabled until both fields are valid (e.g. MH's "My booking"). */
	function disabled(button) {
		return button.disabled || button.getAttribute("aria-disabled") === "true";
	}
	/** The Continue/submit button of the form that holds the PNR field. */
	function continueButton(pnrEl) {
		const form = pnrEl.closest("form");
		if (!form) return null;
		const buttons = Array.from(form.querySelectorAll("button, input[type=submit]")).filter(rendered);
		const text = ins?.profile?.auto_submit?.button_text?.trim().toLowerCase();
		const label = (b) => (b.value || b.innerText || "").trim().toLowerCase();
		return buttons.find((b) => b.getAttribute("type") === "submit" || b.tagName === "BUTTON" && !b.getAttribute("type")) ?? (text ? buttons.find((b) => label(b) === text) : void 0) ?? null;
	}
	function autoReady(item, surnameEl, pnrEl) {
		return !!ins?.running && ins.autoSubmit && !ins.paused && !ins.blocked && !ins.searched && ins.item?.pnr === item.pnr && !item.recapture && filledFor?.pnr === item.pnr && pnrEl.value === item.pnr && rendered(surnameEl) && rendered(pnrEl);
	}
	/** Schedule the automatic Continue for the filled booking; returns the status line to show. */
	function scheduleContinue(item, surnameEl, pnrEl) {
		if (!autoReady(item, surnameEl, pnrEl)) return null;
		const button = continueButton(pnrEl);
		if (!button) return `Auto-continue: no Continue button found — press Enter · ${item.pnr}`;
		if (disabled(button)) return `Auto-continue: waiting for the site to accept the fields · ${item.pnr}`;
		if (autoFor !== item.pnr) {
			autoFor = item.pnr;
			autoGap = autoGapMs(ins.profile?.auto_submit);
			const wait = Math.max(AUTO_SETTLE_MS, (ins.lastSearchAt ?? 0) + autoGap - Date.now());
			clearTimeout(autoTimer);
			autoTimer = setTimeout(() => pressContinue(item, surnameEl, pnrEl), wait);
			log("auto-continue in", wait, "ms for", item.pnr, "(gap", autoGap, "ms)");
			return `Auto-continue in ${Math.ceil(wait / 1e3)} s · ${item.pnr} (Alt+P pauses)`;
		}
		const wait = Math.max(0, (ins.lastSearchAt ?? 0) + autoGap - Date.now());
		return `Auto-continue in ${Math.ceil(wait / 1e3)} s · ${item.pnr} (Alt+P pauses)`;
	}
	function pressContinue(item, surnameEl, pnrEl) {
		if (!autoReady(item, surnameEl, pnrEl)) {
			autoFor = null;
			return schedule();
		}
		const button = continueButton(pnrEl);
		if (!button || disabled(button)) {
			autoFor = null;
			return schedule();
		}
		noteSearched();
		log("auto-continue: pressing", button.value || button.innerText?.trim(), "for", item.pnr);
		button.click();
	}
	/** Re-evaluate on the next frame (DOM changes come in bursts). */
	function schedule() {
		if (!scheduled) {
			scheduled = true;
			requestAnimationFrame(evaluate);
		}
	}
	function main() {
		new MutationObserver(schedule).observe(document.documentElement, {
			childList: true,
			subtree: true,
			characterData: true
		});
		setInterval(schedule, TICK_MS);
		chrome.runtime.onMessage.addListener((msg) => {
			if (msg.type === "instruction" && msg.instruction) apply(msg.instruction);
			if (msg.type === "recapture") recapture();
		});
		window.addEventListener("keydown", (e) => {
			if (!e.altKey || e.ctrlKey || e.metaKey) return;
			const key = HOTKEYS[e.key.toLowerCase()];
			if (!key) return;
			e.preventDefault();
			if (key === "recapture") return recapture();
			send({
				type: "hotkey",
				key
			}).then(apply);
		}, true);
		const searched = noteSearched;
		window.addEventListener("keydown", (e) => e.key === "Enter" && !e.altKey && searched(), true);
		window.addEventListener("pointerup", (e) => {
			if (e.target?.closest?.("button, [type=submit], [role=button], a")) searched();
		}, true);
		window.addEventListener("submit", searched, true);
		window.addEventListener("pagehide", () => {
			searched();
			if (filledFor && ins?.running) send({
				type: "status",
				text: "Waiting for page…"
			});
		});
		log("loaded on", location.href);
		send({
			type: "hello",
			url: location.href
		}).then((next) => {
			if (!next) log("no answer from the extension: reload this page (or the extension)");
			else log("instruction:", {
				running: next.running,
				searched: next.searched,
				pnr: next.item?.pnr ?? null
			});
			apply(next);
		});
	}
	function recapture() {
		const last = ins?.lastDone;
		if (!last || !isResultPage()) return status("Re-capture works on a result page, right after a booking");
		capture(last, true);
	}
	if (!window.__gdsLookupLoaded) {
		window.__gdsLookupLoaded = true;
		main();
	}
	//#endregion
})();
