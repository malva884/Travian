"""Tema UI stile 'security console' (dark, denso, pill badge) per la dashboard.

Ispirato a console tipo Raven.io / Aperture: KPI card in alto, tabelle dense
con pill di stato colorate, sidebar a icon-rail, tab bar minimale.

Uso in app.py:
    import ui_theme as ui
    ui.inject_theme()
    ui.brand_block()
    ui.kpi_row([...])
    ui.console_table(headers, rows)
    ui.pill("Running", "success")
"""

from __future__ import annotations

import datetime
import html

import streamlit as st

_CSS = """
<style>
/* ============================ PALETTE ============================ */
:root {
  --tv-bg:        #0c0e12;
  --tv-panel:     #14181e;
  --tv-panel-2:   #191f27;
  --tv-border:    #232a34;
  --tv-text:      #e2e6ec;
  --tv-dim:       #8b93a1;
  --tv-accent:    #84cc16;
  --tv-green:     #4ade80;
  --tv-red:       #f87171;
  --tv-amber:     #fbbf24;
  --tv-blue:      #60a5fa;
  --tv-gray:      #9ca3af;
  --tv-radius:    10px;
  --tv-font-mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
}

/* ============================ BASE ============================ */
.stApp { background: var(--tv-bg); }
header[data-testid="stHeader"] { background: transparent; }
div[data-testid="stDecoration"] { display: none; }
footer { visibility: hidden; }

.block-container {
  padding-top: 2.2rem;
  padding-left: 2.2rem;
  padding-right: 2.2rem;
  max-width: none;
}

h1, h2, h3, h4, h5, h6 { letter-spacing: -0.01em; }
p, .stMarkdown { color: var(--tv-text); }
.stCaption, small { color: var(--tv-dim) !important; }
hr { border-color: var(--tv-border); }

/* ============================ SIDEBAR ============================ */
section[data-testid="stSidebar"] {
  background: #0a0c10;
  border-right: 1px solid var(--tv-border);
}
section[data-testid="stSidebar"] .block-container { padding: 1.2rem 1rem; }
section[data-testid="stSidebar"] h1 {
  font-size: 1.15rem;
  letter-spacing: 0.04em;
}

/* ============================ TABS ============================ */
div[data-testid="stTabs"] [data-baseweb="tab-list"] {
  gap: 4px;
  border-bottom: 1px solid var(--tv-border);
  background: transparent;
}
div[data-testid="stTabs"] button[data-baseweb="tab"] {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  color: var(--tv-dim);
  font-weight: 500;
  font-size: 0.88rem;
  padding: 0.55rem 0.9rem;
  height: auto;
}
div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
  color: var(--tv-text);
  background: transparent;
}
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
  color: var(--tv-accent);
  border-bottom-color: var(--tv-accent);
  background: transparent;
}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
div[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }

/* ============================ METRIC -> CARD ============================ */
div[data-testid="stMetric"] {
  background: var(--tv-panel);
  border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius);
  padding: 0.9rem 1.1rem;
}
div[data-testid="stMetric"] label { color: var(--tv-dim) !important; font-size: 0.8rem; }
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
  font-size: 1.9rem;
  font-weight: 700;
  font-family: var(--tv-font-mono);
}

/* ============================ CONTAINERS / EXPANDERS ============================ */
div[data-testid="stVerticalBlockBorderWrapper"],
div[data-testid="stExpander"] details {
  background: var(--tv-panel);
  border: 1px solid var(--tv-border) !important;
  border-radius: var(--tv-radius) !important;
}
div[data-testid="stExpander"] details summary {
  font-weight: 600;
  font-size: 0.95rem;
}
div[data-testid="stExpander"] details summary:hover { color: var(--tv-accent); }

/* ============================ INPUTS / BUTTONS ============================ */
div[data-baseweb="select"] > div,
div[data-baseweb="input"] > div,
textarea, input[type="text"], input[type="number"], input[type="password"] {
  background-color: var(--tv-panel-2) !important;
  border-color: var(--tv-border) !important;
  border-radius: 8px !important;
}
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="input"] > div:focus-within { border-color: var(--tv-accent) !important; }

button[kind="primary"], button[data-testid="stBaseButton-primary"] {
  background: var(--tv-accent) !important;
  border-color: var(--tv-accent) !important;
  color: #0c0e12 !important;
  font-weight: 600;
}
button[kind="secondary"], button[data-testid="stBaseButton-secondary"],
div[data-testid="stFormSubmitButton"] button {
  border-color: var(--tv-border) !important;
}

/* Toggle / checkbox / radio accento */
div[data-testid="stToggle"] span[aria-checked="true"],
input[type="checkbox"]:checked + div { background: var(--tv-accent) !important; }

/* ============================ CODE / LOG ============================ */
.stCode, pre, code {
  font-family: var(--tv-font-mono) !important;
}
pre {
  background: #0a0c10 !important;
  border: 1px solid var(--tv-border) !important;
  border-radius: var(--tv-radius) !important;
  font-size: 0.78rem !important;
  line-height: 1.55 !important;
}

/* ============================ DATAFRAME ============================ */
div[data-testid="stDataFrame"] {
  border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius);
  overflow: hidden;
}

/* ============================ SCROLLBAR ============================ */
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #2a3140; border-radius: 8px; }
::-webkit-scrollbar-thumb:hover { background: #39414f; }

/* ============================ COMPONENTI CUSTOM ============================ */
.tv-brand {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 2px 6px 2px;
}
.tv-brand .logo {
  width: 34px; height: 34px; border-radius: 8px;
  background: linear-gradient(135deg, var(--tv-accent), #4d7c0f);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px;
}
.tv-brand .name { font-weight: 700; font-size: 0.95rem; letter-spacing: 0.05em; }
.tv-brand .sub  { color: var(--tv-dim); font-size: 0.7rem; letter-spacing: 0.14em; text-transform: uppercase; }

.kpi-grid { display: flex; gap: 14px; margin: 6px 0 18px 0; flex-wrap: wrap; }
.kpi-card {
  flex: 1 1 0; min-width: 180px;
  background: var(--tv-panel);
  border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius);
  padding: 14px 18px;
}
.kpi-card .kpi-label {
  color: var(--tv-dim); font-size: 0.75rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.08em;
  display: flex; align-items: center; gap: 7px;
}
.kpi-card .kpi-value {
  font-family: var(--tv-font-mono); font-size: 2.1rem; font-weight: 700;
  color: var(--tv-text); line-height: 1.25;
}
.kpi-card .kpi-sub { font-size: 0.75rem; color: var(--tv-dim); }
.kpi-card .kpi-sub b.ok  { color: var(--tv-green); font-weight: 600; }
.kpi-card .kpi-sub b.bad { color: var(--tv-red); font-weight: 600; }
.kpi-card .kpi-sub b.warn{ color: var(--tv-amber); font-weight: 600; }

/* ---- Pill badge ---- */
.pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 0.72rem; font-weight: 600;
  padding: 2px 10px; border-radius: 999px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.pill.success { color: var(--tv-green); border-color: rgba(74,222,128,.35); background: rgba(74,222,128,.08); }
.pill.danger  { color: var(--tv-red);   border-color: rgba(248,113,113,.35); background: rgba(248,113,113,.08); }
.pill.warning { color: var(--tv-amber); border-color: rgba(251,191,36,.35);  background: rgba(251,191,36,.08); }
.pill.info    { color: var(--tv-blue);  border-color: rgba(96,165,250,.35);  background: rgba(96,165,250,.08); }
.pill.neutral { color: var(--tv-gray);  border-color: rgba(156,163,175,.3);  background: rgba(156,163,175,.07); }

/* ---- Console table ---- */
.tv-table-wrap {
  border: 1px solid var(--tv-border); border-radius: var(--tv-radius);
  overflow: hidden; background: var(--tv-panel); margin-bottom: 10px;
}
table.tv-table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
table.tv-table thead th {
  text-align: left; color: var(--tv-dim);
  font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.09em; padding: 10px 14px;
  border-bottom: 1px solid var(--tv-border);
  background: var(--tv-panel-2);
}
table.tv-table tbody td {
  padding: 9px 14px; border-bottom: 1px solid #1a2029;
  color: var(--tv-text); vertical-align: middle;
}
table.tv-table tbody tr:last-child td { border-bottom: none; }
table.tv-table tbody tr:hover td { background: #171c24; }
table.tv-table td.mono, .mono { font-family: var(--tv-font-mono); font-size: 0.78rem; }
table.tv-table td.dim, .dim { color: var(--tv-dim); }

/* ---- Toolbar / section headers ---- */
.tv-toolbar {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 0 4px 0;
}
.tv-section {
  font-size: 0.8rem; font-weight: 700; color: var(--tv-dim);
  text-transform: uppercase; letter-spacing: 0.1em;
  margin: 18px 0 8px 0;
}

/* ---- Village card ---- */
.vc {
  background: var(--tv-panel); border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius); padding: 14px 18px; margin-bottom: 12px;
}
.vc-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; gap: 10px; flex-wrap: wrap;
}
.vc-name { font-weight: 700; font-size: 1.0rem; margin-right: 10px; }
.vc-ts { color: var(--tv-dim); font-size: 0.72rem; font-family: var(--tv-font-mono); }
.vc-res {
  display: flex; gap: 20px; flex-wrap: wrap; align-items: center;
  font-family: var(--tv-font-mono); font-size: 0.82rem;
  margin: 4px 0 12px 0;
}
.vc-res b { font-weight: 600; }
.vc-bars { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 10px; }
.vc-label {
  display: flex; justify-content: space-between;
  font-size: 0.72rem; color: var(--tv-dim); margin-bottom: 4px;
}
.bar { height: 6px; background: #1a2029; border-radius: 4px; overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--tv-accent); border-radius: 4px; }
.bar.warn > i { background: var(--tv-amber); }
.bar.crit > i { background: var(--tv-red); }
.vc-foot {
  color: var(--tv-dim); font-size: 0.74rem;
  border-top: 1px solid #1a2029; padding-top: 8px;
}
.vc-foot code {
  background: #0a0c10; border: 1px solid var(--tv-border);
  padding: 1px 7px; border-radius: 5px; font-size: 0.72rem;
}
@media (max-width: 900px) { .vc-bars { grid-template-columns: 1fr; } }

/* ---- Page header ---- */
.ph { margin: -0.6rem 0 1.1rem 0; }
.ph-title { font-size: 1.6rem; font-weight: 700; letter-spacing: -0.01em; }
.ph-sub { color: var(--tv-dim); font-size: 0.85rem; margin-top: 3px; }

/* ---- KPI danger variant ---- */
.kpi-card.danger { border-color: rgba(248,113,113,.45); background: rgba(248,113,113,.05); }
.kpi-card.danger .kpi-value { color: var(--tv-red); }

/* ---- Sidebar nav: bottoni trasformati in voci di menu ---- */
section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
  background: transparent !important;
  border: 1px solid transparent !important;
  color: var(--tv-dim) !important;
  font-weight: 500; font-size: 0.85rem;
  text-align: left; justify-content: flex-start;
  padding: 0.38rem 0.7rem; border-radius: 8px;
}
section[data-testid="stSidebar"] div[data-testid="stButton"] > button:hover {
  color: var(--tv-text) !important;
  background: var(--tv-panel-2) !important;
}
section[data-testid="stSidebar"] div[data-testid="stButton"] > button[kind="primary"] {
  background: rgba(132,204,22,.12) !important;
  color: var(--tv-accent) !important;
  border-color: rgba(132,204,22,.28) !important;
}
</style>
"""

_TONES = ("success", "danger", "warning", "info", "neutral")


def inject_theme() -> None:
    """Inietta il CSS del tema console dark. Chiamare subito dopo set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)


def esc(value) -> str:
    return html.escape(str(value))


def pill(text: str, tone: str = "neutral", icon: str = "") -> str:
    """Badge HTML stile console. tone: success|danger|warning|info|neutral."""
    if tone not in _TONES:
        tone = "neutral"
    label = f"{icon} {esc(text)}" if icon else esc(text)
    return f'<span class="pill {tone}">{label}</span>'


def kpi_card(label: str, value, icon: str = "", sub_html: str = "", tone: str = "") -> str:
    """Card KPI in stile console (label piccola, numero grande, breakdown).

    `tone`: '' oppure 'danger' (bordo/tinta rossa tipo 'Recent violations').
    """
    ic = f"{icon} " if icon else ""
    sub = f'<div class="kpi-sub">{sub_html}</div>' if sub_html else ""
    cls = f"kpi-card {tone}" if tone else "kpi-card"
    return (
        f'<div class="{cls}">'
        f'<div class="kpi-label">{ic}{esc(label)}</div>'
        f'<div class="kpi-value">{esc(value)}</div>'
        f"{sub}</div>"
    )


def kpi_row(cards: list[str]) -> None:
    """Renderizza una riga di card KPI (passare il markup di kpi_card)."""
    st.markdown(
        f'<div class="kpi-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def console_table(headers: list[str], rows: list[list[str]]) -> str:
    """Tabella HTML densa stile console. Le celle `rows` accettano HTML."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="tv-table-wrap"><table class="tv-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    st.markdown(console_table(headers, rows), unsafe_allow_html=True)


def brand_block(name: str = "TRAVIAN AGENT", sub: str = "OPS CONSOLE") -> None:
    """Logo + nome nella sidebar, stile icon-rail delle console."""
    st.markdown(
        '<div class="tv-brand">'
        '<div class="logo">🛡️</div>'
        f'<div><div class="name">{esc(name)}</div>'
        f'<div class="sub">{esc(sub)}</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )


def section(text: str) -> None:
    """Titoletto di sezione in stile console (uppercase, dim)."""
    st.markdown(f'<div class="tv-section">{esc(text)}</div>', unsafe_allow_html=True)


def page_header(title: str, sub: str = "") -> None:
    """Header pagina stile console: titolo grande + sottotitolo dim."""
    sub_html = f'<div class="ph-sub">{esc(sub)}</div>' if sub else ""
    st.markdown(
        f'<div class="ph"><div class="ph-title">{esc(title)}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def _bar_tone(pct: float) -> str:
    if pct >= 0.9:
        return "crit"
    if pct >= 0.75:
        return "warn"
    return ""


def progress_bar(pct: float) -> str:
    """Barra di avanzamento HTML (pct 0..1) con tono colore per soglia."""
    pct = min(1.0, max(0.0, pct))
    cls = _bar_tone(pct)
    cls_attr = f' class="bar {cls}"' if cls else ' class="bar"'
    return f"<div{cls_attr}><i style='width:{pct * 100:.0f}%'></i></div>"


_ROLE_STYLE: dict[str, tuple[str, str]] = {
    "hammer": ("Hammer", "danger"),
    "defensive": ("Defensive", "info"),
    "croplog": ("Croplog", "success"),
    "feeder": ("Feeder", "info"),
    "chiefing": ("Chiefing", "warning"),
    "early_cropper_founder": ("Founder", "warning"),
    "standard": ("Standard", "neutral"),
}


def village_card(name: str, role: str, target: str, snap: dict) -> str:
    """Card villaggio in HTML puro: risorse, barre capienza, attacchi, decisione.

    `snap` e' il record history dell'ultimo ciclo (puo' essere vuoto/None).
    """
    snap = snap or {}
    role_label, tone = _ROLE_STYLE.get(role, (role or "standard", "neutral"))

    res = snap.get("resources", {})
    wood = int(res.get("wood", 0) or 0)
    clay = int(res.get("clay", 0) or 0)
    iron = int(res.get("iron", 0) or 0)
    crop = int(res.get("crop", 0) or 0)
    wh_cap = int(res.get("warehouse_cap", 0) or 0) or 1
    gr_cap = int(res.get("granary_cap", 0) or 0) or 1
    wh_fill = max(wood, clay, iron)
    wh_pct = wh_fill / wh_cap
    gr_pct = crop / gr_cap

    queue = snap.get("build_queue", 0)
    attacks = int(snap.get("incoming_attacks", 0) or 0)

    if snap.get("ts"):
        ts_str = datetime.datetime.fromtimestamp(snap["ts"]).strftime("%H:%M:%S")
    else:
        ts_str = "in attesa di scansione"

    role_pill = pill(role_label, tone)
    target_html = (
        f"<span class='dim'>➡ {esc(target)}</span>" if target else ""
    )
    atk_html = (
        pill(str(attacks), "danger", "⚔")
        if attacks
        else "<span class='dim'>0</span>"
    )

    if snap:
        res_html = (
            f"<span>🪵 <b>{wood:,}</b></span>"
            f"<span>🧱 <b>{clay:,}</b></span>"
            f"<span>⛓️ <b>{iron:,}</b></span>"
            f"<span>🌾 <b>{crop:,}</b></span>"
            f"<span class='dim'>🔨 coda {queue}</span>"
            f"<span>⚔️ {atk_html}</span>"
        )
        bars_html = (
            '<div class="vc-bars">'
            "<div><div class='vc-label'><span>Magazzino</span>"
            f"<span>{wh_fill:,} / {wh_cap:,} · {wh_pct * 100:.0f}%</span></div>"
            f"{progress_bar(wh_pct)}</div>"
            "<div><div class='vc-label'><span>Granaio</span>"
            f"<span>{crop:,} / {gr_cap:,} · {gr_pct * 100:.0f}%</span></div>"
            f"{progress_bar(gr_pct)}</div>"
            "</div>"
        )
        action = esc(snap.get("action") or "-")
        reason = esc(snap.get("reason") or "-")
        foot_html = (
            f'<div class="vc-foot">ultima decisione '
            f"<code>{action}</code> — {reason}</div>"
        )
    else:
        res_html = "<span class='dim'>nessun dato — avvia l'agente</span>"
        bars_html = ""
        foot_html = ""

    return (
        '<div class="vc">'
        '<div class="vc-head">'
        f"<div><span class='vc-name'>🏰 {esc(name)}</span>"
        f"{role_pill} {target_html}</div>"
        f"<span class='vc-ts'>{esc(ts_str)}</span>"
        "</div>"
        f'<div class="vc-res">{res_html}</div>'
        f"{bars_html}{foot_html}</div>"
    )
