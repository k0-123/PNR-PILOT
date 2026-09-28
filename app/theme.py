"""Visual theme for the Streamlit UI: paper-gray page, Inter type, glass gradient KPI cards and
LED-dot numbers. Same design language as the extension side panel (extension/public/sidepanel.css).
"""
from __future__ import annotations

import html

# 7-row bitmap glyphs for the LED-dot type. Characters without a glyph are drawn as plain text.
GLYPHS: dict[str, list[str]] = {
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
    "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
    "–": ["000", "000", "000", "111", "000", "000", "000"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "o": ["00000", "00000", "01110", "10001", "10001", "10001", "01110"],
    "k": ["10000", "10000", "10010", "10100", "11000", "10100", "10010"],
    "u": ["00000", "00000", "10001", "10001", "10001", "10011", "01101"],
    "p": ["00000", "00000", "11110", "10001", "11110", "10000", "10000"],
}


def dot_svg(text: str, pitch: int = 5, radius: float = 1.55) -> str:
    """Render `text` as an inline SVG of dots (fill = currentColor)."""
    x, circles = 0, []
    for ch in text:
        glyph = GLYPHS.get(ch)
        if glyph is None:
            x += pitch * 2  # unknown character: blank space
            continue
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    circles.append(f'<circle cx="{x + col * pitch + 1.55:g}" cy="{row * 4 + 1.55:g}" r="{radius}"/>')
        x += len(glyph[0]) * pitch + pitch  # glyph width + 1-column gap
    width = max(x - pitch, 1)
    return (f'<svg class="dot-svg" viewBox="0 0 {width} 28" style="aspect-ratio:{width}/28" '
            f'fill="currentColor" role="img" aria-label="{html.escape(text)}">{"".join(circles)}</svg>')


def _value_html(value: str) -> str:
    """Dots for the leading run of drawable characters, the rest (units like 'min') as text."""
    i = 0
    while i < len(value) and value[i] in GLYPHS:
        i += 1
    head, tail = value[:i], value[i:].strip()
    out = dot_svg(head) if head else ""
    if tail:
        out += f'<span class="kpi__unit">{html.escape(tail)}</span>'
    return out


TONES = ("speed", "context", "connections")  # crimson, violet, orange


def kpi_row(items: list[tuple[str, object]], animate: bool = False) -> str:
    """HTML for a row of glass KPI cards. items: (label, value). Tones cycle crimson/violet/orange."""
    cards = []
    for n, (label, value) in enumerate(items):
        tone = TONES[n % len(TONES)]
        cards.append(
            f'<div class="kpi kpi--{tone}" style="--i:{n}">'
            f'<div class="kpi__label">{html.escape(label)}</div>'
            f'<div class="kpi__value">{_value_html(str(value))}</div></div>')
    cls = "kpis kpis--enter" if animate else "kpis"
    return f'<div class="{cls}" style="--n:{len(items)}">{"".join(cards)}</div>'


def brand(size: str = "sm") -> str:
    """'GDS PNR Lookup' wordmark with the LED-dot 'Lookup'."""
    return (f'<div class="brand brand--{size}">GDS PNR'
            f'<span class="dot-word">{dot_svg("Lookup", pitch=4, radius=1.8)}</span></div>')


_GRAIN = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='220' height='220'>"
          "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.54' numOctaves='3' seed='27' "
          "stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter>"
          "<rect width='100%' height='100%' filter='url(%23n)' opacity='.55'/></svg>")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {
  --paper: #ececeb; --ink: #222222; --copy: #4a4a4a; --accent: #ad314d;
  --glass-line: rgba(255,255,255,.36); --card-radius: 17px;
}
html, body, .stApp { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.stApp { -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision; }

/* masthead-style titles */
h1 { font-weight: 400 !important; letter-spacing: .015em; color: #202020 !important; line-height: 1.22 !important; }
h1 span[data-testid="stHeaderActionElements"] { display: none; }
.block-container { padding-top: 3.2rem; animation: support-reveal .6s cubic-bezier(.22,1,.36,1) both; }

/* pill buttons: white "Learn More" style for secondary, crimson for primary */
.stButton button, .stDownloadButton button, .stFormSubmitButton button, [data-testid="stPopover"] button {
  border-radius: 999px !important; border: 0 !important; font-weight: 500; padding-inline: 12px;
  background: rgba(255,255,255,.97); color: #2d2d2d;
  box-shadow: 0 1px 0 rgba(255,255,255,.5) inset, 0 1px 3px rgba(58,25,39,.10);
  transition: transform .18s ease, box-shadow .18s ease;
}
.stButton button:hover, .stDownloadButton button:hover, .stFormSubmitButton button:hover,
[data-testid="stPopover"] button:hover {
  transform: translateY(-2px); box-shadow: 0 8px 20px rgba(58,25,39,.16); color: #2d2d2d;
}
button[kind="primary"], button[kind="primaryFormSubmit"], [data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primaryFormSubmit"] {
  background: linear-gradient(180deg, #bd4468 0%, #ad355b 45%, #8c1320 100%) !important; color: #fff !important;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.24), 0 2px 4px rgba(50,28,39,.30) !important;
}
button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover { color: #fff !important; }
button:focus-visible { outline: 3px solid rgba(173,49,77,.45) !important; outline-offset: 3px; }

/* forms, bordered containers, expanders: frosted white panels */
[data-testid="stForm"], [data-testid="stExpander"] details, div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
  border-radius: var(--card-radius) !important;
}
[data-testid="stForm"], [data-testid="stExpander"] details {
  background: rgba(255,255,255,.62); border: 1px solid rgba(255,255,255,.9) !important;
  box-shadow: 0 1px 2px rgba(50,28,39,.06), 0 10px 30px -18px rgba(50,28,39,.25);
}
[data-testid="stDataFrame"], [data-testid="stImage"] img { border-radius: 12px; overflow: hidden; }
.stProgress > div > div > div > div { background: linear-gradient(90deg, #bd4468, #e8703d) !important; }

/* sidebar */
section[data-testid="stSidebar"] { background: #e4e4e2; border-right: 1px solid rgba(0,0,0,.05); }
section[data-testid="stSidebar"] [role="radiogroup"] label { padding: 6px 10px; border-radius: 999px; }
section[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background: rgba(255,255,255,.85); box-shadow: 0 1px 3px rgba(58,25,39,.08); }

/* brand wordmark */
.brand { display: flex; align-items: center; gap: .32em; color: #202020; font-weight: 500; letter-spacing: .015em; white-space: nowrap; }
.brand--sm { font-size: 20px; margin-bottom: 8px; }
.brand--lg { font-size: clamp(26px, 3vw, 40px); font-weight: 400; justify-content: center; margin: 8px 0 4px; }
.brand .dot-word { color: var(--accent); display: inline-block; height: .78em; transform: translateY(.04em); }
.brand .dot-svg { height: 100%; width: auto; display: block; }
.intro { color: var(--copy); font-size: 15px; line-height: 1.62; letter-spacing: -.01em; margin: -.4rem 0 1.2rem; max-width: 44em; }

/* glass KPI cards */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: clamp(8px, 1.2vw, 16px); margin: 4px 0 18px; }
.kpi {
  position: relative; overflow: hidden; isolation: isolate; min-height: 118px; padding: 16px 16px 18px;
  border: 1px solid var(--glass-line); border-radius: var(--card-radius); color: #fff;
  background-origin: border-box;
  box-shadow: 0 2px 4px rgba(50,28,39,.30), inset 0 1px 0 rgba(255,255,255,.24);
  display: flex; flex-direction: column; justify-content: space-between;
}
.kpi::before {
  content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; mix-blend-mode: screen;
  background: linear-gradient(102deg, rgba(255,255,255,.10), transparent 28%, rgba(255,255,255,.08) 62%, transparent 88%),
              radial-gradient(ellipse 85% 34% at 54% 7%, rgba(255,255,255,.24), transparent 72%);
}
.kpi::after {
  content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; opacity: .5; mix-blend-mode: soft-light;
  background: url("GRAIN");
}
.kpi--speed {
  background: radial-gradient(ellipse 70% 45% at 104% -4%, rgba(255,238,233,.55), transparent 78%),
              radial-gradient(ellipse 68% 45% at -4% -3%, rgba(255,235,232,.5), transparent 77%),
              radial-gradient(ellipse 64% 49% at -8% 106%, rgba(255,222,199,.40), transparent 78%),
              linear-gradient(180deg, #bd4468 0%, #ad355b 38%, #a63b50 72%, #8c1320 100%);
  background-origin: border-box;
}
.kpi--context {
  background: radial-gradient(ellipse 78% 25% at 15% 8%, rgba(218,211,255,.54), transparent 79%),
              radial-gradient(ellipse 43% 42% at 105% 30%, rgba(245,200,210,.45), transparent 78%),
              radial-gradient(ellipse 70% 48% at 70% 110%, rgba(238,204,201,.5), transparent 77%),
              linear-gradient(164deg, #c9b5e1 0%, #ad80ca 29%, #9d4f72 64%, #793246 100%);
  background-origin: border-box;
}
.kpi--connections {
  background: radial-gradient(ellipse 38% 24% at 102% 8%, rgba(255,190,164,.3), transparent 75%),
              radial-gradient(ellipse 80% 36% at 62% 57%, rgba(255,141,36,.55), transparent 72%),
              radial-gradient(ellipse 60% 32% at 97% 101%, rgba(190,40,44,.42), transparent 76%),
              linear-gradient(177deg, #d84736 0%, #dd523c 24%, #e8703d 52%, #de5641 78%, #d34239 100%);
  background-origin: border-box;
}
.kpi__label { font-size: 14px; font-weight: 600; color: rgba(255,255,255,.96); text-shadow: 0 1px 1px rgba(72,28,48,.14); }
.kpi__value { display: flex; align-items: flex-end; gap: 6px; color: rgba(255,255,255,.97); filter: drop-shadow(0 1px 1px rgba(104,27,54,.12)); }
.kpi__value .dot-svg { height: 34px; width: auto; display: block; }
.kpi__unit { font-size: 20px; line-height: 1; transform: translateY(1px); }

/* entrance (dashboard only; live-refreshing rows don't animate) */
.kpis--enter .kpi { animation: card-establish .76s cubic-bezier(.16,1,.3,1) calc(.12s + var(--i) * .1s) both; }
.kpis--enter .kpi__value { animation: metric-resolve .66s cubic-bezier(.16,1,.3,1) calc(.4s + var(--i) * .1s) both; }
@keyframes card-establish { from { opacity: .52; translate: 0 10px; scale: .985; } to { opacity: 1; translate: none; scale: none; } }
@keyframes metric-resolve { from { opacity: 0; translate: 0 7px; filter: blur(2.5px); } to { opacity: 1; translate: none; filter: none; } }
@keyframes support-reveal { from { opacity: 0; translate: 0 8px; } to { opacity: 1; translate: none; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition-duration: .01ms !important; }
}
</style>
""".replace("GRAIN", _GRAIN)
