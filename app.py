
import html
import json
import math
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

st.set_page_config(
    page_title="Licitaciones | Dashboard", page_icon="ðŸ›ï¸", layout="wide",
    initial_sidebar_state="collapsed",
)

DB_PATH = Path(os.getenv("LICITACIONES_DB_PATH", "licitaciones.db")).expanduser()
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL_SECONDS", "300"))
PAGE_SIZE = 12
STATES = {"PUB": "En plazo / Publicada", "EV": "En evaluaciÃ³n", "ADJ": "Adjudicada", "RES": "Resuelta / Formalizada"}
TYPES = {"1": "Suministros", "2": "Servicios", "3": "Obras", "21": "ConcesiÃ³n de servicios", "31": "ConcesiÃ³n de obras"}

st.markdown("""
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem}
div[data-testid="stMetric"] {background:#fff;border:1px solid #e2e8f0;border-radius:.65rem;padding:.75rem 1rem}
@media (max-width: 768px) {
  .block-container {padding:1rem .75rem 2rem}
  div[data-testid="stHorizontalBlock"] {flex-direction:column;gap:.65rem}
  div[data-testid="column"] {width:100%!important;flex:1 1 100%!important}
  h1 {font-size:1.75rem!important}
}
</style>
""", unsafe_allow_html=True)


def valid_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    return value.strip() if parsed.scheme in {"http", "https"} and parsed.netloc else None


def eur(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "No especificado"
    if math.isnan(amount):
        return "No especificado"
    return f"{amount:,.2f}".translate(str.maketrans({",": ".", ".": ","})) + " â‚¬"


def date_es(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return "No especificada" if pd.isna(parsed) else parsed.strftime("%d/%m/%Y Â· %H:%M")


def freshness(value):
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return ""
    days = max(0, (datetime.now(timezone.utc) - parsed.to_pydatetime()).days)
    return "hoy" if days == 0 else ("hace 1 dÃ­a" if days == 1 else f"hace {days} dÃ­as")


@st.cache_data(ttl=CACHE_TTL, show_spinner="Cargando licitacionesâ€¦")
def load_data(path, mtime_ns):
    del mtime_ns
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        frame = pd.read_sql_query("SELECT * FROM licitaciones", connection)
    required = {
        "id", "expediente", "titulo", "organo_contratante", "tipo_contrato", "estado",
        "pbl_sin_iva", "cpv", "municipio", "provincia", "comunidad_autonoma", "latitud",
        "longitud", "fecha_limite", "fecha_actualizacion", "url_licitacion",
        "documentos_adjuntos", "resumen_ia",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("Faltan columnas: " + ", ".join(sorted(missing)))
    frame["fecha_limite_dt"] = pd.to_datetime(frame["fecha_limite"], errors="coerce", utc=True)
    frame["fecha_act_dt"] = pd.to_datetime(frame["fecha_actualizacion"], errors="coerce", utc=True)
    frame["tipo_desc"] = frame["tipo_contrato"].astype(str).map(TYPES).fillna("Otros")
    frame["pbl_sin_iva"] = pd.to_numeric(frame["pbl_sin_iva"], errors="coerce")
    return frame


if not DB_PATH.is_file():
    st.error(f"No se encuentra la base de datos: {DB_PATH}")
    st.stop()
try:
    df = load_data(str(DB_PATH), DB_PATH.stat().st_mtime_ns)
except Exception as error:
    st.error(f"No se pudo leer la base de datos: {error}")
    st.stop()
if df.empty:
    st.warning("La base de datos no contiene licitaciones.")
    st.stop()

deadlines = df["fecha_limite_dt"].dropna()
min_date = deadlines.min().date() if not deadlines.empty else date.today()
max_date = deadlines.max().date() if not deadlines.empty else date.today()
start_date = date.today() if date.today() <= max_date else min_date
max_amount = df["pbl_sin_iva"].max(skipna=True)
max_amount = float(max_amount) if pd.notna(max_amount) and max_amount > 0 else 200_000.0

defaults = {"f_cpv": "71", "f_estado": ["PUB"], "f_max": 200_000.0, "f_fecha": (start_date, max_date), "page": 1}
for key, value in defaults.items():
    st.session_state.setdefault(key, value)


def reset(initial):
    st.session_state.update({
        "f_texto": "", "f_tipo": [], "f_cpv": "71" if initial else "",
        "f_estado": ["PUB"] if initial else [], "f_ccaa": [], "f_prov": [], "f_muni": [],
        "f_min": 0.0, "f_max": 200_000.0 if initial else max_amount,
        "f_fecha": (start_date, max_date) if initial else (min_date, max_date),
        "f_organo": [], "page": 1,
    })


st.sidebar.title("ðŸŽ›ï¸ Filtros")
a1, a2 = st.sidebar.columns(2)
if a1.button("Quitar", use_container_width=True):
    reset(False); st.rerun()
if a2.button("Iniciales", use_container_width=True):
    reset(True); st.rerun()

query = st.sidebar.text_input("Palabras clave", key="f_texto", placeholder="TÃ­tulo, expediente u organismo")
type_sel = st.sidebar.multiselect("Tipo de contrato", sorted(df["tipo_desc"].dropna().unique()), key="f_tipo", placeholder="Selecciona tipos")
cpv = st.sidebar.text_input("CPV (2 dÃ­gitos)", max_chars=2, key="f_cpv")
state_sel = st.sidebar.multiselect("Estado", sorted(df["estado"].dropna().unique()), format_func=lambda x: STATES.get(x, x), key="f_estado", placeholder="Selecciona estados")
regions = st.sidebar.multiselect("Comunidad autÃ³noma", sorted(df["comunidad_autonoma"].dropna().unique()), key="f_ccaa", placeholder="Selecciona comunidades")
province_source = df[df["comunidad_autonoma"].isin(regions)] if regions else df
provinces = st.sidebar.multiselect("Provincia", sorted(province_source["provincia"].dropna().unique()), key="f_prov", placeholder="Selecciona provincias")
municipality_source = df[df["provincia"].isin(provinces)] if provinces else df
municipalities = st.sidebar.multiselect("Municipio", sorted(municipality_source["municipio"].dropna().unique()), key="f_muni", placeholder="Selecciona municipios")
amount_min = st.sidebar.number_input("Presupuesto mÃ­nimo (â‚¬)", min_value=0.0, step=10_000.0, key="f_min")
amount_max = st.sidebar.number_input("Presupuesto mÃ¡ximo (â‚¬)", min_value=0.0, step=50_000.0, key="f_max")
date_range = st.sidebar.date_input("Fecha lÃ­mite", min_value=min_date, max_value=max_date, key="f_fecha", format="DD/MM/YYYY")
buyers = st.sidebar.multiselect("Ã“rgano de contrataciÃ³n", sorted(df["organo_contratante"].dropna().unique()), key="f_organo", placeholder="Selecciona organismos")

result = df.copy()
if query.strip():
    q = query.strip()
    mask = (result["titulo"].str.contains(q, case=False, na=False, regex=False)
            | result["expediente"].str.contains(q, case=False, na=False, regex=False)
            | result["organo_contratante"].str.contains(q, case=False, na=False, regex=False))
    result = result[mask]
if state_sel: result = result[result["estado"].isin(state_sel)]
if type_sel: result = result[result["tipo_desc"].isin(type_sel)]
result = result[result["pbl_sin_iva"].between(amount_min, amount_max, inclusive="both")]
if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    result = result[result["fecha_limite_dt"].dt.date.between(*date_range)]
if regions: result = result[result["comunidad_autonoma"].isin(regions)]
if provinces: result = result[result["provincia"].isin(provinces)]
if municipalities: result = result[result["municipio"].isin(municipalities)]
if buyers: result = result[result["organo_contratante"].isin(buyers)]
if cpv.strip():
    prefix = cpv.strip()
    result = result[result["cpv"].fillna("").apply(lambda x: any(p.strip().startswith(prefix) for p in str(x).split(",")))]

st.title("ðŸ›ï¸ Monitor de Licitaciones")
st.caption("Plataforma de ContrataciÃ³n del Sector PÃºblico")
latest = df["fecha_act_dt"].max()
kpis = st.columns(4)
kpis[0].metric("Licitaciones filtradas", len(result))
kpis[1].metric("Volumen total sin IVA", eur(result["pbl_sin_iva"].sum()))
kpis[2].metric("Presupuesto medio sin IVA", eur(result["pbl_sin_iva"].mean()))
kpis[3].metric("Ãšltima actualizaciÃ³n", date_es(latest), freshness(latest))
active = ([f"CPV {cpv.strip()}"] if cpv.strip() else []) + [STATES.get(x, x) for x in state_sel] + list(regions)
if active: st.caption("Filtros activos: " + " Â· ".join(active))
if result.empty:
    st.warning("No hay licitaciones que coincidan con los filtros."); st.stop()

cards, map_view = st.tabs(["ðŸ—‚ï¸ Licitaciones", "ðŸ—ºï¸ Mapa"])
with map_view:
    points = result.dropna(subset=["latitud", "longitud"]).copy()
    if points.empty:
        st.info("No hay coordenadas disponibles para estos resultados.")
    else:
        rng = np.random.default_rng(42)
        points["lat_map"] = points["latitud"] + rng.normal(0, .00008, len(points))
        points["lon_map"] = points["longitud"] + rng.normal(0, .00008, len(points))
        points["pbl_fmt"] = points["pbl_sin_iva"].map(eur)
        points["title_safe"] = points["titulo"].fillna("Sin tÃ­tulo").astype(str).map(html.escape)
        points["municipio_safe"] = points["municipio"].fillna("N/D").astype(str).map(html.escape)
        points["provincia_safe"] = points["provincia"].fillna("N/D").astype(str).map(html.escape)
        layer = pdk.Layer("ScatterplotLayer", points, get_position="[lon_map, lat_map]", get_color="[13,110,253,170]", get_radius=5, radius_units="pixels", pickable=True)
        tooltip = {"html": "<b>{title_safe}</b><br>{municipio_safe}, {provincia_safe}<br>{pbl_fmt}", "style": {"backgroundColor": "#1a252c", "color": "white"}}
        st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=pdk.ViewState(latitude=40.4168, longitude=-3.7038, zoom=5.2), tooltip=tooltip, map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"), use_container_width=True)

with cards:
    order = st.selectbox("Ordenar por", ["Fecha mÃ¡s cercana", "Fecha mÃ¡s lejana", "Mayor presupuesto", "Menor presupuesto"])
    if order == "Fecha mÃ¡s cercana": result = result.sort_values("fecha_limite_dt", na_position="last")
    elif order == "Fecha mÃ¡s lejana": result = result.sort_values("fecha_limite_dt", ascending=False, na_position="last")
    elif order == "Mayor presupuesto": result = result.sort_values("pbl_sin_iva", ascending=False, na_position="last")
    else: result = result.sort_values("pbl_sin_iva", na_position="last")

    pages = max(1, math.ceil(len(result) / PAGE_SIZE)); st.session_state.page = min(st.session_state.page, pages)
    n1, n2, n3 = st.columns([1, 2, 1])
    if n1.button("â† Anterior", disabled=st.session_state.page == 1, use_container_width=True): st.session_state.page -= 1; st.rerun()
    n2.markdown(f"<p style='text-align:center'>PÃ¡gina {st.session_state.page} de {pages} Â· {len(result)} resultados</p>", unsafe_allow_html=True)
    if n3.button("Siguiente â†’", disabled=st.session_state.page == pages, use_container_width=True): st.session_state.page += 1; st.rerun()
    start = (st.session_state.page - 1) * PAGE_SIZE
    page = result.iloc[start:start + PAGE_SIZE]

    for offset in range(0, len(page), 3):
        columns = st.columns(3)
        for column, (_, tender) in zip(columns, page.iloc[offset:offset + 3].iterrows()):
            with column, st.container(border=True):
                st.caption(STATES.get(tender["estado"], str(tender["estado"] or "Estado desconocido")))
                st.markdown("##### " + (tender["titulo"] or "Sin tÃ­tulo"))
                st.write("ðŸ›ï¸ " + (tender["organo_contratante"] or "Organismo no especificado"))
                st.caption(f"ðŸ“ {tender['municipio'] or 'N/D'} ({tender['provincia'] or 'N/D'}) Â· ðŸ“¦ {tender['tipo_desc']}")
                st.caption(f"Expediente: {tender['expediente'] or 'N/D'} Â· CPV: {tender['cpv'] or 'N/D'}")
                b1, b2 = st.columns(2); b1.metric("PBL sin IVA", eur(tender["pbl_sin_iva"])); b2.metric("PresentaciÃ³n", date_es(tender["fecha_limite"]))
                l1, l2 = st.columns(2)
                official = valid_url(tender["url_licitacion"])
                if official: l1.link_button("Ficha oficial â†—", official, use_container_width=True)
                place = f"{tender['municipio'] or ''}, {tender['provincia'] or ''}".strip(", ") or "EspaÃ±a"
                l2.link_button("Ver mapa â†—", f"https://www.google.com/maps/search/?api=1&query={quote(place)}", use_container_width=True)
                with st.expander("DocumentaciÃ³n"):
                    try: docs = json.loads(tender["documentos_adjuntos"] or "[]")
                    except (TypeError, json.JSONDecodeError): docs = []
                    docs = [d for d in docs if isinstance(d, dict) and valid_url(d.get("url"))]
                    if not docs: st.write("Sin documentos adjuntos directos.")
                    for index, doc in enumerate(docs, 1):
                        st.link_button(f"{doc.get('tipo', 'Documento')} â€” {doc.get('nombre', index)}", valid_url(doc["url"]), use_container_width=True)
                with st.expander("Resumen tÃ©cnico IA"):
                    try: summary = json.loads(tender.get("resumen_ia") or "null")
                    except (TypeError, json.JSONDecodeError): summary = None
                    if not isinstance(summary, dict):
                        st.info("Pendiente de anÃ¡lisis privado. La web pÃºblica no ejecuta modelos de IA.")
                    else:
                        for title, key in {"Alcance tÃ©cnico":"alcance_tecnico", "Criterios de puntuaciÃ³n":"criterios_puntuacion", "Solvencia":"solvencia_requerida", "Equipo y titulaciones":"equipo_y_titulaciones", "Seguro RC":"seguro_rc", "GarantÃ­as":"garantia", "Condicionantes":"condicionantes_destacados"}.items():
                            st.markdown(f"**{title}**"); st.write(summary.get(key, "No especificado / consultar el pliego original"))
