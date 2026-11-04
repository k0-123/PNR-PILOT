// Runs inside the Register page (WebView). Port of extension/src/signup.ts fillRegisterForm.
// __DATA__ is replaced by the person JSON from the server. Reports back via PNRApp.done(json).
(async function (d) {
  const report = (o) => { try { PNRApp.done(JSON.stringify(o)); } catch (e) {} };
  try {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const visible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
    const bodyText = () => (document.body ? document.body.innerText : "").toLowerCase();
    const BLOCK = ["pardon our interruption", "incapsula", "access denied", "request unsuccessful"];

    // Angular renders the form after load: wait up to 20 s for a visible text box.
    let inputs = [];
    for (let i = 0; i < 40; i++) {
      if (BLOCK.some((w) => bodyText().includes(w))) {
        return report({ blocked: true, filled: [], missing: [], errors: ["Blocked by bot protection page"],
                        url: location.href });
      }
      inputs = Array.from(document.querySelectorAll("input")).filter(visible);
      if (inputs.length) break;
      await sleep(500);
    }
    if (!inputs.length) {
      return report({ filled: [], missing: [], errors: ["No form found on this page after 20 s"], url: location.href });
    }

    const filled = [];
    const describe = (el) => {
      const parts = ["name", "id", "formcontrolname", "placeholder", "aria-label", "autocomplete", "data-placeholder"]
        .map((a) => el.getAttribute(a) || "");
      const id = el.getAttribute("id");
      if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); parts.push(l ? l.textContent : ""); }
      const field = el.closest("mat-form-field, .mat-mdc-form-field, label, .form-group, .field");
      if (field) { const l = field.querySelector("label, mat-label, .mat-mdc-floating-label"); parts.push(l ? l.textContent : ""); }
      return parts.join(" ").toLowerCase();
    };
    const setValue = (el, value) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      el.focus();
      setter.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
    };
    const rules = [
      [/first|given|fname/, /last|middle/, d.first_name, "first name"],
      [/last|family|surname|lname/, null, d.last_name, "last name"],
      [/e-?mail/, null, d.email, "email"],
      [/middle/, null, d.middle_name, "middle name"],
      [/national id|iqama|passport|document|id ?number/, null, d.id_number, "ID"],
      [/phone|mobile/, null, d.phone, "phone"],
      [/birth|dob/, null, d.dob, "date of birth"],
      [/city/, null, d.city, "city"],
    ];
    const fieldsSeen = [];
    for (const el of Array.from(document.querySelectorAll("input"))) {
      if (!visible(el) || el.disabled || el.readOnly) continue;
      if (["password", "checkbox", "radio", "hidden", "submit", "button"].includes(el.type)) continue;
      const desc = describe(el);
      fieldsSeen.push(desc.replace(/\s+/g, " ").trim().slice(0, 60));
      for (const [re, not, value, label] of rules) {
        if (value && re.test(desc) && !(not && not.test(desc))) {
          if (el.value !== value) setValue(el, value);
          filled.push(label);
          break;
        }
      }
    }

    const norm = (t) => (t || "").replace(/[^A-Z]/gi, "").toUpperCase();
    const drops = [
      [/title|salutation|prefix/, d.title, "title"],
      [/gender|sex/, d.gender, "gender"],
      [/nationality|citizenship/, d.nationality, "nationality"],
      [/country|residence/, d.country, "country"],
    ];
    for (const [re, value, label] of drops) {
      const want = norm(value);
      if (!want) continue;
      const ok = (t) => norm(t) === want || (want.length > 3 && norm(t).startsWith(want));
      for (const sel of Array.from(document.querySelectorAll("select"))) {
        if (!re.test(describe(sel)) || (label === "country" && /nationality/.test(describe(sel)))) continue;
        const opt = Array.from(sel.options).find((o) => ok(o.text) || ok(o.value));
        if (opt) { sel.value = opt.value; sel.dispatchEvent(new Event("change", { bubbles: true })); filled.push(label); }
      }
      for (const ms of Array.from(document.querySelectorAll("mat-select, [role=combobox]"))) {
        const desc = describe(ms);
        if (!visible(ms) || !re.test(desc) || (label === "country" && /nationality/.test(desc))) continue;
        ms.click();
        await sleep(500);
        const opt = Array.from(document.querySelectorAll("mat-option, [role=option]")).find((o) => ok(o.textContent));
        if (opt) { opt.click(); filled.push(label); }
        else document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        await sleep(300);
        break;
      }
    }

    const wanted = { "first name": d.first_name, "last name": d.last_name, email: d.email, title: d.title };
    const uniq = Array.from(new Set(filled));
    const missing = Object.keys(wanted).filter((k) => wanted[k] && !uniq.includes(k));
    report({ filled: uniq, missing: missing, errors: pageErrors(), fields: fieldsSeen.slice(0, 25), url: location.href });
  } catch (e) {
    report({ filled: [], missing: [], errors: ["Script error: " + e], url: location.href });
  }

  function pageErrors() {
    const out = [];
    for (const el of Array.from(document.querySelectorAll(
        "mat-error, .mat-mdc-form-field-error, [role=alert], .error, .error-message, .invalid-feedback"))) {
      const t = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (t && (el.offsetParent !== null || el.getClientRects().length) && !out.includes(t)) out.push(t.slice(0, 200));
    }
    return out.slice(0, 10);
  }
})(__DATA__);
