//#region src/outbox.ts
var DB_NAME = "gds-outbox";
var STORE = "items";
function open() {
	return new Promise((resolve, reject) => {
		const req = indexedDB.open(DB_NAME, 1);
		req.onupgradeneeded = () => req.result.createObjectStore(STORE, {
			keyPath: "id",
			autoIncrement: true
		});
		req.onsuccess = () => resolve(req.result);
		req.onerror = () => reject(req.error);
	});
}
async function run(mode, fn) {
	const db = await open();
	try {
		return await new Promise((resolve, reject) => {
			const tx = db.transaction(STORE, mode);
			const req = fn(tx.objectStore(STORE));
			tx.oncomplete = () => resolve(req.result);
			tx.onerror = () => reject(tx.error);
			tx.onabort = () => reject(tx.error);
		});
	} finally {
		db.close();
	}
}
var outbox = {
	add: (item) => run("readwrite", (s) => s.add(item)),
	all: () => run("readonly", (s) => s.getAll()),
	put: (item) => run("readwrite", (s) => s.put(item)),
	remove: (id) => run("readwrite", (s) => s.delete(id)),
	count: () => run("readonly", (s) => s.count())
};
//#endregion
//#region src/shared.ts
/** Chrome match pattern ("https://host/*", "http://127.0.0.1/*") -> does `url` match it? Ports are ignored. */
function matchesPattern(url, pattern) {
	const m = /^(\*|https?):\/\/([^/]+)(\/.*)$/.exec(pattern);
	if (!m) return false;
	let u;
	try {
		u = new URL(url);
	} catch {
		return false;
	}
	const [, scheme, host, path] = m;
	if (scheme !== "*" && u.protocol !== `${scheme}:`) return false;
	if (host !== "*" && !(host.startsWith("*.") ? u.hostname.endsWith(host.slice(1)) : u.hostname === host)) return false;
	return new RegExp("^" + path.split("*").map((p) => p.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$").test(u.pathname + u.search);
}
/** Exponential backoff with a cap, in ms: 1 s, 2 s, 4 s ... 60 s. */
function backoffMs(tries) {
	return Math.min(6e4, 1e3 * 2 ** Math.max(0, tries - 1));
}
//#endregion
//#region src/background.ts
var PREFETCH = 20;
var REFILL_BELOW = 10;
var HISTORY = 20;
var SCRIPT_ID = "gds-content";
var API_TIMEOUT_MS = 15e3;
var initialState = () => ({
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
	session: {
		done: 0,
		notFound: 0,
		problems: 0,
		startedAt: null
	},
	queue: [],
	history: [],
	doneLocal: [],
	searched: null,
	lastSearchAt: null,
	tabId: null
});
async function getSettings() {
	const s = await chrome.storage.local.get(["apiUrl", "token"]);
	return {
		apiUrl: String(s.apiUrl ?? "http://127.0.0.1:8000").replace(/\/+$/, ""),
		token: String(s.token ?? "")
	};
}
var chain = Promise.resolve();
async function load() {
	const { state } = await chrome.storage.session.get("state");
	return {
		...initialState(),
		...state
	};
}
function update(fn) {
	const next = chain.then(async () => {
		const s = await load();
		const out = await fn(s);
		s.current = s.queue[0] ?? null;
		s.queued = s.queue.length;
		await chrome.storage.session.set({ state: s });
		broadcast(s);
		return out;
	});
	chain = next.catch(() => void 0);
	return next;
}
function panelView(s) {
	const { profile: _p, queue: _q, history: _h, doneLocal: _d, searched: _s, lastSearchAt: _l, tabId: _t, ...view } = s;
	return view;
}
function broadcast(s) {
	chrome.runtime.sendMessage({
		type: "state",
		state: panelView(s)
	}).catch(() => void 0);
}
var ApiError = class extends Error {
	status;
	constructor(message, status) {
		super(message);
		this.status = status;
	}
};
async function api(method, path, body) {
	const { apiUrl, token } = await getSettings();
	if (!token) throw new ApiError("No token set: open ⚙ Settings in the side panel", 401);
	let res;
	try {
		res = await fetch(apiUrl + path, {
			method,
			headers: {
				Authorization: `Bearer ${token}`,
				"Content-Type": "application/json"
			},
			body: body === void 0 ? void 0 : JSON.stringify(body),
			signal: AbortSignal.timeout(API_TIMEOUT_MS)
		});
	} catch (e) {
		throw new ApiError(e.name === "TimeoutError" ? `The app at ${apiUrl} didn't answer in time` : `Can't reach the app at ${apiUrl}`, 0);
	}
	const data = await res.json().catch(() => ({}));
	if (!res.ok) throw new ApiError(String(data.detail ?? res.statusText), res.status);
	return data;
}
/** Claim more PNRs when the queue runs low (also renews the leases this browser holds). */
async function refill(s, force = false) {
	if (!s.running || s.jobId === null || !force && s.queue.length >= REFILL_BELOW) return;
	try {
		const { leases } = await api("POST", `/api/jobs/${s.jobId}/claim?n=${PREFETCH}`);
		const have = new Set(s.queue.map((x) => x.pnr));
		const done = new Set(s.doneLocal);
		for (const l of leases) if (!have.has(l.pnr) && !done.has(l.pnr)) s.queue.push(l);
		if (!s.queue.length) {
			s.statusLine = "All bookings of this job are done 🎉";
			s.running = false;
		}
	} catch (e) {
		const err = e;
		s.warning = err.message;
		if (err.status === 409) s.running = false;
	}
}
/** The booking on screen is finished: remember it and move to the next one. */
function advance(s) {
	s.searched = null;
	const done = s.queue.shift();
	if (done) {
		s.history.unshift({
			...done,
			recapture: false
		});
		s.history = s.history.slice(0, HISTORY);
		s.doneLocal = [...s.doneLocal.slice(-500), done.pnr];
	}
}
async function queueUpload(s, jobId, pnr, kind, body) {
	await outbox.add({
		jobId,
		pnr,
		kind,
		body,
		tries: 0,
		nextAt: Date.now()
	});
	s.outboxPending = await outbox.count();
	flushOutbox();
}
var flushing = false;
var flushAgain = false;
var flushTimer;
async function flushOutbox() {
	if (flushing) {
		flushAgain = true;
		return;
	}
	flushing = true;
	flushAgain = false;
	let soonest = Infinity;
	let offline = null;
	try {
		for (const item of await outbox.all()) {
			if (item.nextAt > Date.now()) {
				soonest = Math.min(soonest, item.nextAt);
				continue;
			}
			const path = `/api/jobs/${item.jobId}/lookups/${encodeURIComponent(item.pnr)}/${item.kind}`;
			try {
				await api("POST", path, item.body);
				await outbox.remove(item.id);
			} catch (e) {
				const err = e;
				if (err.status >= 400 && err.status < 500 && err.status !== 401 && err.status !== 408 && err.status !== 429) {
					await outbox.remove(item.id);
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
function instruction(s, extra = {}) {
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
		...extra
	};
}
async function pokeContent(key) {
	const s = await load();
	if (s.tabId === null) return;
	const ins = instruction(s, key ? toSearchForm(s, key) : {});
	chrome.tabs.sendMessage(s.tabId, {
		type: "instruction",
		instruction: ins
	}).catch(() => void 0);
}
/** Skip / Not found leave the booking on screen: go back to the search form, like a timeout does. */
function toSearchForm(s, key) {
	return key === "skip" || key === "notfound" ? { navigate: s.profile?.search_url } : {};
}
async function screenshot(s, textLength, windowId) {
	const mode = s.profile?.capture.screenshot ?? "fallback";
	if (mode === "never" || mode === "fallback" && textLength >= 200 || windowId === void 0) return null;
	try {
		return await chrome.tabs.captureVisibleTab(windowId, {
			format: "jpeg",
			quality: 70
		});
	} catch {
		return null;
	}
}
async function onContent(msg, sender) {
	const tab = sender.tab;
	return update(async (s) => {
		if (tab?.id !== void 0 && (msg.type === "hello" || s.tabId === null)) s.tabId = tab.id;
		const job = s.jobId;
		const current = s.queue[0];
		switch (msg.type) {
			case "hello": return instruction(s);
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
				if (!msg.recapture && current?.pnr === msg.pnr && s.searched !== msg.pnr) return instruction(s);
				const recapture = msg.recapture || current?.pnr === msg.pnr && !!current?.recapture;
				const shot = await screenshot(s, msg.text.length, tab?.windowId);
				await queueUpload(s, job, msg.pnr, "capture", {
					text: msg.text,
					url: msg.url,
					screenshot_b64: shot,
					recapture
				});
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
				const reason = msg.type === "notfound" ? msg.reason : void 0;
				await queueUpload(s, job, msg.pnr, "status", {
					status,
					note: reason ? `website says: ${reason}` : void 0
				});
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
			case "timeout":
				if (job === null || current?.pnr !== msg.pnr || s.searched !== msg.pnr) return instruction(s);
				await queueUpload(s, job, msg.pnr, "status", {
					status: "SKIPPED",
					note: "timed out: the page did not load a result in time"
				});
				advance(s);
				s.session.problems += 1;
				s.statusLine = `Skipped (timed out): ${msg.pnr}`;
				await refill(s);
				return instruction(s, { navigate: s.profile?.search_url });
			case "blocked":
				if (job !== null && msg.pnr && !s.blocked) await queueUpload(s, job, msg.pnr, "status", {
					status: "BLOCKED",
					note: msg.reason
				});
				s.blocked = true;
				s.running = false;
				s.autoSubmit = false;
				s.statusLine = "Stopped: the website is blocking";
				s.warning = `The website showed "${msg.reason}". Auto-continue was switched off and no bookings were lost. Solve it in the browser (or wait a while), then press Resume to carry on.`;
				return instruction(s);
			case "hotkey":
				await hotkey(s, msg.key);
				return instruction(s, toSearchForm(s, msg.key));
		}
	});
}
async function hotkey(s, key) {
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
			s.queue.unshift({
				...prev,
				recapture: true
			});
			s.searched = null;
			s.statusLine = `Back to ${prev.pnr}: press Enter to search it again`;
			return;
		}
		case "recapture": return;
	}
}
async function registerContentScript(profile) {
	if ((await chrome.scripting.getRegisteredContentScripts({ ids: [SCRIPT_ID] })).length) await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
	await chrome.scripting.registerContentScripts([{
		id: SCRIPT_ID,
		matches: profile.match_urls,
		js: ["content.js"],
		runAt: "document_idle",
		persistAcrossSessions: true
	}]);
}
async function start(jobId, jobName, profileName) {
	let profile;
	try {
		profile = await api("GET", `/api/site-profile/${encodeURIComponent(profileName)}`);
	} catch (e) {
		return {
			ok: false,
			error: e.message
		};
	}
	if (profile.configured === false) return {
		ok: false,
		error: `Site profile "${profile.name}" still has TODO selectors. Set them up first.`
	};
	if (!await chrome.permissions.contains({ origins: profile.match_urls })) return {
		ok: false,
		error: "Permission for the airline site is missing. Click Start again and allow it."
	};
	await registerContentScript(profile);
	await update(async (s) => {
		if (s.jobId !== jobId) {
			s.queue = [];
			s.history = [];
			s.doneLocal = [];
			s.session = {
				done: 0,
				notFound: 0,
				problems: 0,
				startedAt: Date.now()
			};
		}
		Object.assign(s, {
			running: true,
			paused: false,
			blocked: false,
			warning: null,
			jobId,
			jobName,
			profileName,
			profile
		});
		s.session.startedAt ??= Date.now();
		s.statusLine = "Starting…";
		await refill(s);
	});
	const tabs = await chrome.tabs.query({ url: profile.match_urls });
	const tab = tabs.find((t) => t.active) ?? tabs[0];
	if (tab?.id !== void 0) {
		await update((s) => void (s.tabId = tab.id));
		await chrome.scripting.executeScript({
			target: { tabId: tab.id },
			files: ["content.js"]
		}).catch(() => void 0);
		await pokeContent();
	} else {
		const created = await chrome.tabs.create({ url: profile.search_url });
		await update((s) => void (s.tabId = created.id ?? null));
	}
	return { ok: true };
}
async function onPanel(msg) {
	switch (msg.type) {
		case "getState": return panelView(await load());
		case "getSettings": return getSettings();
		case "saveSettings":
			await chrome.storage.local.set({
				apiUrl: msg.apiUrl.trim(),
				token: msg.token.trim()
			});
			return { ok: true };
		case "call": try {
			return {
				ok: true,
				data: await api("GET" === msg.method ? "GET" : "POST", msg.path)
			};
		} catch (e) {
			return {
				ok: false,
				error: e.message,
				status: e.status
			};
		}
		case "start": return start(msg.jobId, msg.jobName, msg.profileName);
		case "stop": {
			const s = await load();
			if (s.jobId !== null) {
				const pnrs = s.queue.map((x) => x.pnr);
				await api("POST", `/api/jobs/${s.jobId}/release`, { pnrs }).catch(() => void 0);
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
			if (s.jobId === null || !s.profileName) return {
				ok: false,
				error: "No job selected"
			};
			try {
				await api("POST", `/api/jobs/${s.jobId}/resume`);
			} catch (e) {
				if (e.status !== 409) return {
					ok: false,
					error: e.message
				};
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
				if (s.tabId !== null) chrome.tabs.sendMessage(s.tabId, { type: "recapture" }).catch(() => void 0);
			}
			await pokeContent(msg.key);
			return { ok: true };
	}
}
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
	(!(sender.url ?? "").startsWith(chrome.runtime.getURL("")) ? onContent(msg, sender) : onPanel(msg)).then(sendResponse, (e) => sendResponse({
		ok: false,
		error: String(e)
	}));
	return true;
});
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
	if (info.status !== "complete" || !tab.url) return;
	load().then((s) => {
		if (!s.running || !s.profile || !s.profile.match_urls.some((m) => matchesPattern(tab.url, m))) return;
		chrome.scripting.executeScript({
			target: { tabId },
			files: ["content.js"]
		}).catch(() => void 0);
	});
});
chrome.runtime.onInstalled.addListener(() => {
	chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => void 0);
});
chrome.alarms.create("renew", { periodInMinutes: 2 });
chrome.alarms.create("flush", { periodInMinutes: .5 });
chrome.alarms.onAlarm.addListener((a) => {
	if (a.name === "flush") flushOutbox();
	if (a.name === "renew") update((s) => refill(s, true));
});
flushOutbox();
//#endregion
