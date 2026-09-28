import { describe, expect, it } from "vitest";
import { autoGapMs, backoffMs, includesAny, isSelectorSet, matchesPattern, pnrOnPage } from "../src/shared";

describe("helpers", () => {
  it("pnrOnPage matches whole tokens only", () => {
    expect(pnrOnPage("Booking reference: ER7P5B confirmed", "er7p5b")).toBe(true);
    expect(pnrOnPage("Ref:ER7P5B.", "ER7P5B")).toBe(true);
    expect(pnrOnPage("XER7P5BX", "ER7P5B")).toBe(false);
    expect(pnrOnPage("Booking ZZZ999", "ER7P5B")).toBe(false);
  });

  it("includesAny is case-insensitive and ignores blanks", () => {
    expect(includesAny("Access Denied (error code 15)", ["captcha", "access denied"])).toBe("access denied");
    expect(includesAny("all fine", ["", "  "])).toBeNull();
    expect(includesAny("x", undefined)).toBeNull();
  });

  it("isSelectorSet rejects TODO and blanks", () => {
    expect(isSelectorSet("#pnr")).toBe(true);
    expect(isSelectorSet("TODO")).toBe(false);
    expect(isSelectorSet(" ")).toBe(false);
    expect(isSelectorSet(null)).toBe(false);
  });

  it("matchesPattern follows Chrome match patterns (ports ignored)", () => {
    expect(matchesPattern("http://127.0.0.1:8765/search?x=1", "http://127.0.0.1/*")).toBe(true);
    expect(matchesPattern("https://online.malaysiaairlines.com/a/b", "https://online.malaysiaairlines.com/*")).toBe(true);
    expect(matchesPattern("https://evil.com/", "https://online.malaysiaairlines.com/*")).toBe(false);
    expect(matchesPattern("https://x.malaysiaairlines.com/", "https://*.malaysiaairlines.com/*")).toBe(true);
  });

  it("backoff doubles up to a minute", () => {
    expect([1, 2, 3, 10].map(backoffMs)).toEqual([1000, 2000, 4000, 60000]);
  });

  it("autoGapMs jitters between min and max", () => {
    const auto = { min_gap_ms: 6000, max_gap_ms: 11000, button_text: null };
    expect(autoGapMs(auto, () => 0)).toBe(6000); // low end
    expect(autoGapMs(auto, () => 1)).toBe(11000); // high end
    expect(autoGapMs(auto, () => 0.5)).toBe(8500); // midpoint
  });

  it("autoGapMs is a fixed min when max is unset or not larger", () => {
    expect(autoGapMs({ min_gap_ms: 6000, button_text: null }, () => 0.9)).toBe(6000);
    expect(autoGapMs({ min_gap_ms: 6000, max_gap_ms: 3000, button_text: null }, () => 0.9)).toBe(6000);
    expect(autoGapMs(null, () => 0.9)).toBe(6000); // default when no profile
  });
});
