"""Design system for the anonymizer UI.

Graphite + teal, bank-grade: restrained, legible, information-dense but calm.
Everything is self-contained (system font stack, no web fonts or CDNs) so it
works in the air-gapped offline bundle. Light and dark are both first-class,
driven by CSS variables that flip on Quasar's `body--dark`.
"""

from __future__ import annotations

from nicegui import ui

# Quasar brand colors (teal accent on a graphite base).
PRIMARY = "#0d9488"  # teal-600
SECONDARY = "#334155"  # slate-700
ACCENT = "#14b8a6"  # teal-500
POSITIVE = "#0d9488"
NEGATIVE = "#dc2626"
WARNING = "#d97706"
INFO = "#0e7490"

# Trust tiers and sensitivity levels get consistent chip colors everywhere.
TIER_COLORS = {"high": "#0d9488", "medium": "#d97706", "low": "#64748b"}
SENSITIVITY_COLORS = {"high": "#e11d48", "medium": "#d97706", "low": "#64748b"}

ACTION_COLORS = {"pseudonymize": "#0d9488", "anonymize": "#e11d48", "skip": "#64748b"}

_CSS = """
:root {
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: "Cascadia Code", "SF Mono", "Consolas", "Liberation Mono", monospace;

  --bg: #f4f6f7;
  --surface: #ffffff;
  --surface-2: #eef1f3;
  --border: #dbe0e4;
  --text: #14201f;
  --text-muted: #5b6a72;
  --shadow: 0 1px 2px rgba(15,32,31,.04), 0 4px 16px rgba(15,32,31,.06);
}
.body--dark {
  --bg: #0e1416;
  --surface: #161e21;
  --surface-2: #1d272b;
  --border: #2a373c;
  --text: #e6edef;
  --text-muted: #8ea0a8;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.35);
}

body, .q-page, .nicegui-content { background: var(--bg) !important; color: var(--text); font-family: var(--font-sans); }

/* NiceGUI wraps page content in a padded, gapped, centered column by default,
   which fights our own layout (mis-aligned header, odd spacing). Neutralize it
   and let our containers own all spacing/width. */
.nicegui-content {
  padding: 0 !important;
  gap: 0 !important;
  align-items: stretch !important;
  max-width: none !important;
  width: 100%;
}
.q-page { min-height: 100vh; }
html, body { margin: 0; width: 100%; overflow-x: hidden; }

.az-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow); padding: 20px;
}
.az-hairline { border-top: 1px solid var(--border); }
.az-muted { color: var(--text-muted); }
.az-mono { font-family: var(--font-mono); }
.az-h1 { font-size: 1.25rem; font-weight: 700; letter-spacing: -.01em; color: var(--text); }
.az-h2 { font-size: .95rem; font-weight: 650; color: var(--text); }
.az-kicker { font-size: .68rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted); }

.az-header {
  background: var(--surface); border-bottom: 1px solid var(--border);
  backdrop-filter: saturate(1.2);
}

.az-chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border-radius: 999px;
  font-size: .68rem; font-weight: 650; line-height: 1.5; border: 1px solid transparent; white-space: nowrap;
}

.az-dropzone {
  border: 1.5px dashed var(--border); border-radius: 14px; background: var(--surface);
  transition: all .15s ease; cursor: pointer;
}
.az-dropzone:hover { background: var(--surface-2); border-color: var(--text-muted); }
.az-dropzone.az-drag { border-color: __ACCENT__; background: color-mix(in srgb, __ACCENT__ 8%, var(--surface)); }

.az-stat { display:flex; flex-direction:column; gap:2px; }
.az-stat .n { font-size: 1.4rem; font-weight: 700; line-height: 1; }
.az-stat .l { font-size: .7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing:.06em; }

.az-scroll { max-height: 58vh; overflow-y: auto; overflow-x: hidden; }
.az-scroll::-webkit-scrollbar { width: 10px; }
.az-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }

.q-expansion-item { border:1px solid var(--border); border-radius:12px; background:var(--surface); overflow:hidden; }
.az-row { border-top:1px solid var(--border); }
""".replace("__ACCENT__", ACCENT)


def install() -> None:
    """Sets Quasar brand colors and injects the design-system CSS. Call once per
    page (cheap; NiceGUI dedupes identical head html)."""
    ui.colors(
        primary=PRIMARY,
        secondary=SECONDARY,
        accent=ACCENT,
        positive=POSITIVE,
        negative=NEGATIVE,
        warning=WARNING,
        info=INFO,
    )
    ui.add_head_html(f"<style>{_CSS}</style>")


def chip(text: str, color: str, *, filled: bool = False) -> ui.html:
    """A small pill. Outlined by default; `filled` for a solid emphasis chip."""
    if filled:
        style = f"background:{color};color:#fff;border-color:{color};"
    else:
        style = f"color:{color};border-color:{color}55;background:{color}12;"
    return ui.html(f'<span class="az-chip" style="{style}">{text}</span>')


def tier_chip(tier: str) -> ui.html:
    labels = {"high": "auto-accept", "medium": "review", "low": "review"}
    return chip(labels.get(tier, tier), TIER_COLORS.get(tier, "#64748b"))


def sensitivity_chip(sensitivity: str) -> ui.html:
    return chip(sensitivity, SENSITIVITY_COLORS.get(sensitivity, "#64748b"))
