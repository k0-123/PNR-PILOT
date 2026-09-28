// LED-dot numbers for the side panel counters (same glyphs as the dashboard, app/theme.py).

const GLYPHS: Record<string, string[]> = {
  "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
  "1": ["010", "110", "010", "010", "010", "010", "111"],
  "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
  "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
  "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
  "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
  "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
  "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
  "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
  "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
  ".": ["0", "0", "0", "0", "0", "0", "1"],
  "–": ["000", "000", "000", "111", "000", "000", "000"],
  L: ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
  o: ["00000", "00000", "01110", "10001", "10001", "10001", "01110"],
  k: ["10000", "10000", "10010", "10100", "11000", "10100", "10010"],
  u: ["00000", "00000", "10001", "10001", "10001", "10011", "01101"],
  p: ["00000", "00000", "11110", "10001", "11110", "10000", "10000"],
};

const SVG = "http://www.w3.org/2000/svg";

function dotSvg(text: string, pitch = 5, radius = 1.55): SVGSVGElement {
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
export function setDots(el: HTMLElement, value: string, pitch = 5, radius = 1.55): void {
  if (el.dataset.value === value) return; // avoid redrawing every poll
  el.dataset.value = value;
  el.setAttribute("aria-label", value);
  let i = 0;
  while (i < value.length && GLYPHS[value[i]]) i++;
  const head = value.slice(0, i);
  const tail = value.slice(i).trim();
  const parts: Node[] = [];
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
export function fillDots(): void {
  for (const el of document.querySelectorAll<HTMLElement>("[data-dots]")) {
    const word = el.classList.contains("dot-word");
    setDots(el, el.dataset.dots!, word ? 4 : 5, word ? 1.8 : 1.55);
  }
}
