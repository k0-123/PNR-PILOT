(function() {
	//#region src/dots.ts
	var GLYPHS = {
		"0": [
			"01110",
			"10001",
			"10011",
			"10101",
			"11001",
			"10001",
			"01110"
		],
		"1": [
			"010",
			"110",
			"010",
			"010",
			"010",
			"010",
			"111"
		],
		"2": [
			"01110",
			"10001",
			"00001",
			"00010",
			"00100",
			"01000",
			"11111"
		],
		"3": [
			"11110",
			"00001",
			"00001",
			"01110",
			"00001",
			"00001",
			"11110"
		],
		"4": [
			"00010",
			"00110",
			"01010",
			"10010",
			"11111",
			"00010",
			"00010"
		],
		"5": [
			"11111",
			"10000",
			"10000",
			"11110",
			"00001",
			"00001",
			"11110"
		],
		"6": [
			"01110",
			"10000",
			"10000",
			"11110",
			"10001",
			"10001",
			"01110"
		],
		"7": [
			"11111",
			"00001",
			"00010",
			"00100",
			"01000",
			"01000",
			"01000"
		],
		"8": [
			"01110",
			"10001",
			"10001",
			"01110",
			"10001",
			"10001",
			"01110"
		],
		"9": [
			"01110",
			"10001",
			"10001",
			"01111",
			"00001",
			"00001",
			"01110"
		],
		".": [
			"0",
			"0",
			"0",
			"0",
			"0",
			"0",
			"1"
		],
		"–": [
			"000",
			"000",
			"000",
			"111",
			"000",
			"000",
			"000"
		],
		L: [
			"10000",
			"10000",
			"10000",
			"10000",
			"10000",
			"10000",
			"11111"
		],
		o: [
			"00000",
			"00000",
			"01110",
			"10001",
			"10001",
			"10001",
			"01110"
		],
		k: [
			"10000",
			"10000",
			"10010",
			"10100",
			"11000",
			"10100",
			"10010"
		],
		u: [
			"00000",
			"00000",
			"10001",
			"10001",
			"10001",
			"10011",
			"01101"
		],
		p: [
			"00000",
			"00000",
			"11110",
			"10001",
			"11110",
			"10000",
			"10000"
		]
	};
	var SVG = "http://www.w3.org/2000/svg";
	function dotSvg(text, pitch = 5, radius = 1.55) {
		const svg = document.createElementNS(SVG, "svg");
		let x = 0;
		for (const ch of text) {
			const glyph = GLYPHS[ch];
			if (!glyph) continue;
			glyph.forEach((bits, row) => {
				[...bits].forEach((bit, col) => {
					if (bit !== "1") return;
					const c = document.createElementNS(SVG, "circle");
					c.setAttribute("cx", String(x + col * pitch + 1.55));
					c.setAttribute("cy", String(row * 4 + 1.55));
					c.setAttribute("r", String(radius));
					svg.append(c);
				});
			});
			x += glyph[0].length * pitch + pitch;
		}
		const width = Math.max(x - pitch, 1);
		svg.setAttribute("viewBox", `0 0 ${width} 28`);
		svg.setAttribute("class", "dot-svg");
		svg.setAttribute("fill", "currentColor");
		svg.setAttribute("aria-hidden", "true");
		return svg;
	}
	/** Show `value` in `el` as dots; a trailing non-numeric part (e.g. "min") stays text. */
	function setDots(el, value, pitch = 5, radius = 1.55) {
		if (el.dataset.value === value) return;
		el.dataset.value = value;
		el.setAttribute("aria-label", value);
		let i = 0;
		while (i < value.length && GLYPHS[value[i]]) i++;
		const head = value.slice(0, i);
		const tail = value.slice(i).trim();
		const parts = [];
		if (head) parts.push(dotSvg(head, pitch, radius));
		if (tail) {
			const unit = document.createElement("span");
			unit.className = "unit";
			unit.textContent = tail;
			parts.push(unit);
		}
		el.replaceChildren(...parts);
	}
	/** Draw every [data-dots] element (the header word mark and the counters' starting values). */
	function fillDots() {
		for (const el of document.querySelectorAll("[data-dots]")) {
			const word = el.classList.contains("dot-word");
			setDots(el, el.dataset.dots, word ? 4 : 5, word ? 1.8 : 1.55);
		}
	}
	//#endregion
	//#region src/shared.ts
	var HOTKEYS = {
		n: "notfound",
		s: "skip",
		b: "back",
		p: "pause",
		r: "recapture"
	};
	//#endregion
	//#region src/sidepanel.ts
	var $ = (id) => document.getElementById(id);
	function bg(msg) {
		return chrome.runtime.sendMessage(msg);
	}
	var call = (path, method = "GET") => bg({
		type: "call",
		method,
		path
	});
	var state = null;
	var profileOrigins = [];
	async function loadSettings() {
		const s = await bg({ type: "getSettings" });
		$("api-url").value = s.apiUrl;
		$("token").value = s.token;
		return !!s.token;
	}
	async function saveSettings() {
		const apiUrl = $("api-url").value.trim().replace(/\/+$/, "");
		await bg({
			type: "saveSettings",
			apiUrl,
			token: $("token").value
		});
		try {
			const origin = new URL(apiUrl).origin + "/*";
			if (!await chrome.permissions.contains({ origins: [origin] })) await chrome.permissions.request({ origins: [origin] });
		} catch {}
		const me = await call("/api/me");
		$("settings-msg").textContent = me.ok ? `Connected as ${me.data.name} ✓` : `✗ ${me.error}`;
		if (me.ok) await loadLists();
	}
	async function loadLists() {
		const [jobs, profiles] = await Promise.all([call("/api/jobs"), call("/api/site-profiles")]);
		const jobSel = $("job");
		const profSel = $("profile");
		jobSel.replaceChildren();
		profSel.replaceChildren();
		if (!jobs.ok || !profiles.ok) {
			showWarning(jobs.error ?? profiles.error ?? "Can't load jobs");
			return;
		}
		for (const j of jobs.data) jobSel.add(new Option(`#${j.id} ${j.name} · ${j.remaining} left${j.status === "PAUSED" ? " · PAUSED" : ""}`, String(j.id)));
		if (!jobs.data.length) jobSel.add(new Option("No job is open for lookups (dashboard → Open for lookups)", ""));
		const saved = (await chrome.storage.local.get("profileName")).profileName;
		for (const p of profiles.data) profSel.add(new Option(p.title + (p.configured ? "" : " (not set up yet)"), p.name, false, p.name === saved));
		if (state?.jobId) jobSel.value = String(state.jobId);
		await loadProfileOrigins();
	}
	async function loadProfileOrigins() {
		const name = $("profile").value;
		if (!name) return;
		await chrome.storage.local.set({ profileName: name });
		const r = await call(`/api/site-profile/${encodeURIComponent(name)}`);
		profileOrigins = r.ok ? r.data.match_urls : [];
	}
	async function start() {
		const jobSel = $("job");
		if (!jobSel.value) return showWarning("Pick a job first.");
		if (profileOrigins.length && !await chrome.permissions.request({ origins: profileOrigins })) return showWarning("The extension needs permission for the airline website to fill the form.");
		const r = await bg({
			type: "start",
			jobId: Number(jobSel.value),
			jobName: jobSel.selectedOptions[0]?.text ?? "",
			profileName: $("profile").value
		});
		if (!r.ok) showWarning(r.error ?? "Could not start");
	}
	function hotkey(key) {
		bg({
			type: "hotkey",
			key
		});
	}
	/** Download the job's final Excel as it is now (the app builds it fresh). */
	async function downloadExcel() {
		const jobId = state?.jobId ?? Number($("job").value);
		if (!jobId) return showWarning("Pick a job first.");
		const msg = $("download-msg");
		msg.textContent = "Preparing…";
		const { apiUrl, token } = await bg({ type: "getSettings" });
		try {
			const res = await fetch(`${apiUrl}/api/jobs/${jobId}/final.xlsx`, { headers: { Authorization: `Bearer ${token}` } });
			if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
			const url = URL.createObjectURL(await res.blob());
			const a = Object.assign(document.createElement("a"), {
				href: url,
				download: `job_${jobId}_final.xlsx`
			});
			document.body.append(a);
			a.click();
			a.remove();
			setTimeout(() => URL.revokeObjectURL(url), 1e4);
			msg.textContent = "";
		} catch (e) {
			msg.textContent = `✗ ${e.message}`;
		}
	}
	function showWarning(text, withResume = false) {
		$("warning").hidden = !text;
		$("warning-text").textContent = text ?? "";
		$("resume").hidden = !withResume;
	}
	function render(s) {
		state = s;
		document.body.classList.toggle("running", s.running);
		const cur = s.current;
		$("cur-pnr").textContent = cur?.pnr ?? "–";
		$("cur-surname").textContent = cur?.surname ?? "";
		$("cur-pax").replaceChildren(...(cur?.passengers ?? []).map((p) => {
			const li = document.createElement("li");
			li.textContent = `${p.surname ?? "?"}/${p.first_name ?? ""}${p.title ? " " + p.title : ""}`;
			return li;
		}));
		$("status-line").textContent = s.statusLine;
		setDots($("c-done"), String(s.session.done));
		setDots($("c-nf"), String(s.session.notFound));
		setDots($("c-err"), String(s.session.problems));
		$("pause-btn").firstChild.textContent = s.paused ? "Continue " : "Pause ";
		$("auto").checked = s.autoSubmit;
		$("start").disabled = s.running;
		$("stop").disabled = !s.running;
		$("outbox").textContent = s.outboxPending ? `${s.outboxPending} upload(s) waiting…` : "";
		showWarning(s.warning, s.blocked);
	}
	async function pollProgress() {
		if (!state?.jobId) return;
		const r = await call(`/api/jobs/${state.jobId}/progress`);
		if (!r.ok) return;
		setDots($("c-left"), String(r.data.pnrs.left));
		setDots($("c-speed"), r.data.per_minute ? r.data.per_minute.toFixed(1) : "–");
		const eta = r.data.eta_minutes;
		setDots($("c-eta"), eta === null ? "–" : eta < 60 ? `${Math.round(eta)} min` : `${(eta / 60).toFixed(1)} h`);
	}
	chrome.runtime.onMessage.addListener((msg) => {
		if (msg.type === "state" && msg.state) render(msg.state);
	});
	$("toggle-settings").addEventListener("click", () => $("settings").hidden = !$("settings").hidden);
	$("save-settings").addEventListener("click", () => void saveSettings());
	$("start").addEventListener("click", () => void start());
	$("stop").addEventListener("click", () => void bg({ type: "stop" }));
	$("resume").addEventListener("click", () => void bg({ type: "resume" }));
	$("refresh").addEventListener("click", () => void loadLists());
	$("auto").addEventListener("change", () => void bg({
		type: "setAuto",
		on: $("auto").checked
	}));
	$("download").addEventListener("click", () => void downloadExcel());
	$("profile").addEventListener("change", () => void loadProfileOrigins());
	for (const b of document.querySelectorAll("[data-key]")) b.addEventListener("click", () => hotkey(b.dataset.key));
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
		render(await bg({ type: "getState" }));
		if (hasToken) await loadLists();
		setInterval(() => void pollProgress(), 5e3);
		pollProgress();
	})();
	//#endregion
})();
