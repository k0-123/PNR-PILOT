// Rule: the code that runs on the airline page starts a search only in two ways: the staff member
// pressing Enter, or pressContinue() when staff switched on "Auto-continue" in the side panel.
// This test fails the build if any other code that could submit a form, click a button or fake a
// key press appears in content.ts (and what it imports), and checks that the automatic press
// stays behind the Auto-continue switch.
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const FORBIDDEN: [RegExp, string][] = [
  [/\.submit\s*\(/, "form.submit()"],
  [/requestSubmit/, "form.requestSubmit()"],
  [/\.click\s*\(/, "element.click()"],
  [/KeyboardEvent/, "synthetic keyboard events"],
  [/new\s+(Mouse|Pointer|Input)Event\s*\(/, "synthetic mouse/pointer/input events"],
  [/new\s+(Custom)?Event\s*\(\s*["'`](submit|click|key\w*)["'`]/i, "synthetic submit/click/key events"],
  [/dispatchEvent\s*\(\s*["'`]?(submit|click|key)/i, "dispatching submit/click/key"],
  [/\.form\s*\.\s*(submit|requestSubmit)/, "submitting the field's form"],
];

// The only allowed clicks, one per function (marked in the source; the bundle drops comments):
// openForm() shows a form behind a tab; pressContinue() is Auto-continue.
const ALLOWED: Record<string, string> = {
  openForm: "gds-allow: opens form tab, never a search",
  pressContinue: "gds-allow: auto-continue, switched on by staff",
};
const CLICK = /\.click\s*\(/g;

/** The function `name` in a source file: its header up to the matching closing brace. */
function functionBody(src: string, name: string): string {
  const start = src.search(new RegExp(`function ${name}\\(`));
  if (start < 0) return "";
  let i = src.indexOf("{", src.indexOf(")", start)); // the body's "{" after the parameter list
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  return src.slice(start);
}

function check(file: string): string[] {
  let src = readFileSync(file, "utf8");
  for (const name of Object.keys(ALLOWED)) {
    const body = functionBody(src, name);
    if ((body.match(CLICK) ?? []).length === 1) src = src.replace(body, ""); // the allowed click
  }
  src = src
    .split("\n")
    .filter((l) => !l.trim().startsWith("//")) // comments may *mention* what is forbidden
    .join("\n");
  return FORBIDDEN.filter(([re]) => re.test(src)).map(([, what]) => `${file}: ${what}`);
}

const files = () => ["src/content.ts", ...(existsSync("dist/content.js") ? ["dist/content.js"] : [])];

describe("the airline-page code searches only on Enter or with Auto-continue switched on", () => {
  it("content.ts and shared.ts contain no other submit / click / fake key code", () => {
    expect([...check("src/content.ts"), ...check("src/shared.ts")]).toEqual([]);
  });

  it("exactly one click in openForm and one in pressContinue, none elsewhere", () => {
    const src = readFileSync("src/content.ts", "utf8");
    for (const [name, marker] of Object.entries(ALLOWED)) {
      expect(src.split(marker).length - 1, `marker for ${name}`).toBe(1);
      expect(functionBody(src, name)).toContain(marker);
    }
    for (const file of files()) {
      const text = readFileSync(file, "utf8");
      const counts = [text, ...Object.keys(ALLOWED).map((n) => functionBody(text, n))]
        .map((t) => (t.match(CLICK) ?? []).length);
      expect(counts, `${file}: clicks total / openForm / pressContinue`).toEqual([2, 1, 1]);
    }
  });

  it("the automatic press only happens with Auto-continue on and the form filled", () => {
    for (const file of files()) {
      const text = readFileSync(file, "utf8");
      const press = functionBody(text, "pressContinue");
      expect(press, file).toMatch(/if \(!autoReady\(/);                 // re-checked right before pressing
      expect(functionBody(text, "autoReady"), file).toMatch(/ins\.autoSubmit/); // the staff switch
      expect(functionBody(text, "autoReady"), file).toMatch(/!ins\.blocked/);    // never into a CAPTCHA
    }
  });

  it("the built content.js too (if built)", () => {
    if (!existsSync("dist/content.js")) return;
    expect(check("dist/content.js")).toEqual([]);
  });

  it("the check itself catches forbidden code", () => {
    const bad = ["form.submit();", "f.requestSubmit()", "btn.click()", "new KeyboardEvent('keydown')",
      "el.dispatchEvent(new Event('submit'))", "new MouseEvent('click')"];
    for (const code of bad) expect(FORBIDDEN.some(([re]) => re.test(code)), code).toBe(true);
  });
});
