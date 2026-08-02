import streamlit as st
import sqlite3
import pandas as pd
import json
import pydeck as pdk
import numpy as np
import os
import requests
from datetime import date
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Licitaciones | Dashboard", layout="wide", page_icon="🏛️")

# --- RUTA EXACTA A LA BASE DE DATOS ---
DB_PATH = r"C:\Users\sobre\Landa Soluciones de Ingeniería Civil SC\LANDA - Documentos\11 Automatizaciones\Dashboard licitaciones\licitaciones.db"

# --- CONFIGURACIÓN DE GEMINI ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "TU_API_KEY_AQUI")

PROMPT_ANALISIS = """
Eres un director técnico y perito especializado en licitaciones de ingeniería civil en España. 
Tu objetivo es entregar una ficha de decisión rápida, limpia y milimétrica para el licitador. NO repitas presupuestos ni importes globales (ya figuran en la cabecera de la tarjeta). Ve directamente al grano.

INSTRUCCIÓN ESTRICTA DE FORMATO:
- **OBLIGATORIO**: Utiliza viñetas (•) y negritas para estructurar la información en CADA uno de los campos del JSON. Cero párrafos seguidos o densos. Cada idea debe ir en una línea separada con bullet.

Instrucciones de contenido:
- **alcance_tecnico**: Detalla con viñetas (•) el objeto principal, el ámbito o ubicación exacta y las fases o documentos clave a redactar.
- **criterios_puntuacion**: Con viñetas (•), detalla el total de puntos y desglosa los juicios de valor y las fórmulas u oferta económica.
- **solvencia_requerida**: Con viñetas (•), detalla solvencia económica (volumen anual mínimo), solvencia técnica (trabajos similares) y si exige clasificación.
- **equipo_y_titulaciones**: Con viñetas (•), indica las titulaciones requeridas para el director del contrato y personal clave.
- **seguro_rc**: Con viñetas (•), especifica la cobertura obligatoria y el importe mínimo exigido por siniestro.
- **garantia**: Con viñetas (•), detalla la garantía provisional y la definitiva (5% del PBL).
- **condicionantes_destacados**: Con viñetas (•), detalla el plazo de ejecución, penalizaciones diarias y aspectos críticos.

Devuelve ÚNICAMENTE un objeto JSON válido con esta estructura exacta y formato Markdown interno:
{
  "alcance_tecnico": "<Usa viñetas •>",
  "criterios_puntuacion": "<Usa viñetas •>",
  "solvencia_requerida": "<Usa viñetas •>",
  "equipo_y_titulaciones": "<Usa viñetas •>",
  "seguro_rc": "<Usa viñetas •>",
  "garantia": "<Usa viñetas •>",
  "condicionantes_destacados": "<Usa viñetas •>"
}
"""

def extraer_texto_de_enlaces(documentos_json, url_ficha_principal):
    texto_acumulado = ""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    try:
        docs = json.loads(documentos_json) if documentos_json else []
        for d in docs[:4]:
            url = d.get('url', '')
            if url:
                try:
                    resp = requests.get(url, headers=headers, timeout=6)
                    if resp.status_code == 200 and 'html' in resp.headers.get('content-type', '').lower():
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for script in soup(["script", "style"]):
                            script.decompose()
                        texto_acumulado += "\n--- " + str(d.get('nombre', 'Doc')) + " ---\n" + soup.get_text(separator=' ', strip=True)[:15000] + "\n"
                except Exception:
                    continue
        if not texto_acumulado and url_ficha_principal:
            resp = requests.get(url_ficha_principal, headers=headers, timeout=6)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                texto_acumulado += soup.get_text(separator=' ', strip=True)[:15000]
    except Exception:
        pass
    return texto_acumulado

def analizar_licitacion_directo(licitacion_dict):
    api_key_a_usar = GEMINI_API_KEY
    if not api_key_a_usar or api_key_a_usar == "TU_API_KEY_AQUI":
        return None, "⚠️ Falta configurar la API Key de Gemini."

    try:
        client = genai.Client(api_key=api_key_a_usar)

        texto_pliegos = extraer_texto_de_enlaces(
            licitacion_dict.get('documentos_adjuntos'), 
            licitacion_dict.get('url_licitacion')
        )

        contenido_input = f"""
Título: {licitacion_dict.get('titulo', 'No especificado')}
Organismo: {licitacion_dict.get('organo_contratante', 'No especificado')}
Tipo: {licitacion_dict.get('tipo_contrato', 'No especificado')} | CPV: {licitacion_dict.get('cpv', 'No especificado')}
PBL sin IVA: {licitacion_dict.get('pbl_sin_iva', 0)} €
Ubicación: {licitacion_dict.get('municipio', '')}, {licitacion_dict.get('provincia', '')}
Enlace Oficial: {licitacion_dict.get('url_licitacion', 'No especificado')}

TEXTO / ENLACES DISPONIBLES DE LA PLATAFORMA Y PLIEGOS:
{texto_pliegos if texto_pliegos else "Basarse en los metadatos generales proporcionados."}
"""

        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=f"{PROMPT_ANALISIS}\n\nDATOS:\n{contenido_input}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        if response is None or not response.text:
            return None, "⚠️ Gemini devolvió una respuesta vacía."

        texto_limpio = response.text.strip()
        marcas_cif = chr(96) * 3
        if texto_limpio.startswith(marcas_cif + "json"):
            texto_limpio = texto_limpio[7:]
        if texto_limpio.endswith(marcas_cif):
            texto_limpio = texto_limpio[:-3]
        texto_limpio = texto_limpio.strip()

        try:
            res_json = json.loads(texto_limpio)
        except json.JSONDecodeError:
            res_json = {
                "alcance_tecnico": response.text,
                "criterios_puntuacion": "• No se pudo estructurar automáticamente (error de sintaxis en origen).",
                "solvencia_requerida": "• Ver pliego original.",
                "equipo_y_titulaciones": "• No especificado",
                "seguro_rc": "• No especificado",
                "garantia": "• No especificado",
                "condicionantes_destacados": "• No especificado"
            }

        campos_esperados = [
            "alcance_tecnico", "criterios_puntuacion", "solvencia_requerida",
            "equipo_y_titulaciones", "seguro_rc", "garantia", "condicionantes_destacados"
        ]

        for campo in campos_esperados:
            if not res_json.get(campo):
                res_json[campo] = "• No especificado / Consultar pliego original"

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE licitaciones
                SET analizado_ia = 1,
                    resumen_ia = ?
                WHERE id = ?
            """, (
                json.dumps(res_json, ensure_ascii=False),
                licitacion_dict["id"]
            ))

            if cursor.rowcount != 1:
                conn.rollback()
                return None, f"❌ No se encontró la licitación ID {licitacion_dict['id']} en SQLite."

            conn.commit()

        return res_json, None

    except Exception as exc:
        return None, f"❌ Error: {str(exc)}"

# --- ESTILOS CSS ---
st.markdown("""
<style>
    .stApp { background-color: #f8f9fa; color: #212529; }
    .badge-pub { background-color: #198754; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
    .badge-ev { background-color: #fd7e14; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
    .badge-adj { background-color: #0d6efd; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
    .badge-res { background-color: #6c757d; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
    .external-link-btn { color: #0d6efd; text-decoration: none; font-size: 0.85rem; font-weight: 600; background-color: #e7f1ff; padding: 2px 8px; border-radius: 6px; border: 1px solid #b6d4fe; }
    .external-link-btn:hover { background-color: #0d6efd; color: white; }
    
    .metric-box-grid { background-color: #ffffff; border-radius: 8px; padding: 12px 10px; text-align: center; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    .metric-val-grid { font-size: 1.35rem; font-weight: 800; color: #0d6efd; letter-spacing: -0.5px; }
    .metric-lbl-grid { font-size: 0.72rem; color: #475569; text-transform: uppercase; font-weight: 700; margin-top: 4px; }

    /* Altura igualada para tarjetas por fila */
    .row-widget.stHorizontal { align-items: stretch !important; }
    div[data-testid="stVerticalBlock"]:has(> div.stContainer) { height: 100%; }
    div[data-testid="stContainer"] { height: 100% !important; display: flex !important; flex-direction: column !important; justify-content: space-between !important; }

    div[data-testid="stExpander"] { margin-top: 14px !important; }
    .stMarkdown a.anchor-link, [data-testid="stHeaderActionElements"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

MAPA_ESTADOS = {
    'PUB': ('En plazo / Publicada', 'badge-pub'),
    'EV':  ('En Evaluación', 'badge-ev'),
    'ADJ': ('Adjudicada', 'badge-adj'),
    'RES': ('Resuelta / Formalizada', 'badge-res')
}

MAPA_TIPOS = {
    '1': 'Suministros', '2': 'Servicios', '3': 'Obras',
    '21': 'Concesión de Servicios', '31': 'Concesión de Obras'
}

@st.cache_data
def cargar_datos():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM licitaciones", conn)
    conn.close()
    
    df['fecha_limite_dt'] = pd.to_datetime(df['fecha_limite'].str.slice(0, 10), errors='coerce', utc=True)
    df['fecha_act_dt'] = pd.to_datetime(df['fecha_actualizacion'], errors='coerce', utc=True)
    df['tipo_contrato_desc'] = df['tipo_contrato'].map(MAPA_TIPOS).fillna('Otros')
    return df

try:
    df = cargar_datos()
except Exception as e:
    st.error(f"❌ No se encontró 'licitaciones.db' en la ruta {DB_PATH}. Error: {e}")
    st.stop()

# Determinar valores máximos y fechas por defecto sensatas
max_pbl_db = float(df['pbl_sin_iva'].max() or 200000.0)
if max_pbl_db <= 0:
    max_pbl_db = 200000.0

fechas_validas = df['fecha_limite_dt'].dropna()
f_min_db = fechas_validas.min().date() if not fechas_validas.empty else date.today()
f_max_db = fechas_validas.max().date() if not fechas_validas.empty else date.today()
hoy = date.today()
f_inicio_default = hoy if hoy <= f_max_db else f_min_db

# --- INICIALIZAR FILTROS POR DEFECTO AL ARRANCAR ---
if "f_cpv" not in st.session_state:
    st.session_state["f_cpv"] = "71"
if "f_estado" not in st.session_state:
    st.session_state["f_estado"] = ["PUB"]
if "f_pbl_max" not in st.session_state:
    st.session_state["f_pbl_max"] = 200000.0
if "f_fecha" not in st.session_state:
    st.session_state["f_fecha"] = (f_inicio_default, f_max_db)

# --- SIDEBAR: FILTROS ORDENADOS ---
st.sidebar.title("🎛️ Filtros Avanzados")

# Botones de control de filtros (uno arriba del otro ocupando todo el ancho)
if st.sidebar.button("🗑️ Quitar filtros", use_container_width=True):
    st.session_state["f_texto"] = ""
    st.session_state["f_tipo"] = []
    st.session_state["f_cpv"] = ""
    st.session_state["f_estado"] = []
    st.session_state["f_ccaa"] = []
    st.session_state["f_prov"] = []
    st.session_state["f_muni"] = []
    st.session_state["f_pbl_min"] = 0.0
    st.session_state["f_pbl_max"] = max_pbl_db
    st.session_state["f_fecha"] = (f_min_db, f_max_db)
    st.session_state["f_organo"] = []
    st.rerun()

if st.sidebar.button("↩️ Filtros iniciales", use_container_width=True):
    st.session_state["f_texto"] = ""
    st.session_state["f_tipo"] = []
    st.session_state["f_cpv"] = "71"
    st.session_state["f_estado"] = ["PUB"]
    st.session_state["f_ccaa"] = []
    st.session_state["f_prov"] = []
    st.session_state["f_muni"] = []
    st.session_state["f_pbl_min"] = 0.0
    st.session_state["f_pbl_max"] = 200000.0
    st.session_state["f_fecha"] = (f_inicio_default, f_max_db)
    st.session_state["f_organo"] = []
    st.rerun()

st.sidebar.divider()

# 1. Palabras clave
busqueda_texto = st.sidebar.text_input("🔍 Palabras clave (título, expediente...):", key="f_texto")

# 2. Tipo de Contrato
tipos_list = sorted([x for x in df['tipo_contrato_desc'].unique() if x])
tipo_sel = st.sidebar.multiselect("📦 Tipo de Contrato:", tipos_list, key="f_tipo")

# 3. CPV
cpv_2dig = st.sidebar.text_input("🏷️ CPV (2 dígitos):", max_chars=2, key="f_cpv")

# Estado
estados_unicos = df['estado'].dropna().unique().tolist()
opciones_estado = {c: MAPA_ESTADOS.get(c, (c, ''))[0] for c in estados_unicos}
estados_sel = st.sidebar.multiselect("📌 Estado:", list(opciones_estado.keys()), format_func=lambda x: opciones_estado[x], key="f_estado")

# 4. Las ubicaciones (CCAA, Provincia, Municipio)
ccaa_list = sorted([x for x in df['comunidad_autonoma'].dropna().unique() if x])
ccaa_sel = st.sidebar.multiselect("🗺️ Comunidad Autónoma:", ccaa_list, key="f_ccaa")

prov_list = sorted([x for x in df[df['comunidad_autonoma'].isin(ccaa_sel)]['provincia'].dropna().unique() if x]) if ccaa_sel else sorted([x for x in df['provincia'].dropna().unique() if x])
prov_sel = st.sidebar.multiselect("📍 Provincia:", prov_list, key="f_prov")

muni_list = sorted([x for x in df[df['provincia'].isin(prov_sel)]['municipio'].dropna().unique() if x]) if prov_sel else sorted([x for x in df['municipio'].dropna().unique() if x])
muni_sel = st.sidebar.multiselect("🏙️ Municipio:", muni_list, key="f_muni")

# 5. Los presupuestos y fecha de presentación
st.sidebar.markdown("💶 **Presupuesto Base sin IVA (€):**")
pbl_min_val = st.sidebar.number_input("Mínimo €", min_value=0.0, step=10000.0, key="f_pbl_min")
pbl_max_val = st.sidebar.number_input("Máximo €", min_value=0.0, value=200000.0, step=50000.0, key="f_pbl_max")

if not fechas_validas.empty:
    fecha_rango = st.sidebar.date_input("📅 Fecha Límite Presentación:", min_value=f_min_db, max_value=f_max_db, key="f_fecha")
else:
    fecha_rango = None

# 6. El órgano de contratación
organos = sorted([x for x in df['organo_contratante'].dropna().unique() if x])
organo_sel = st.sidebar.multiselect("🏛️ Órgano de Contratación:", organos, key="f_organo")

# --- APLICAR FILTROS ---
df_f = df.copy()

if busqueda_texto.strip():
    q = busqueda_texto.lower().strip()
    df_f = df_f[df_f['titulo'].str.lower().str.contains(q, na=False) | df_f['expediente'].str.lower().str.contains(q, na=False) | df_f['organo_contratante'].str.lower().str.contains(q, na=False)]

if estados_sel: df_f = df_f[df_f['estado'].isin(estados_sel)]
if tipo_sel: df_f = df_f[df_f['tipo_contrato_desc'].isin(tipo_sel)]
df_f = df_f[(df_f['pbl_sin_iva'] >= pbl_min_val) & (df_f['pbl_sin_iva'] <= pbl_max_val)]

if fecha_rango and len(fecha_rango) == 2:
    df_f = df_f[(df_f['fecha_limite_dt'].dt.date >= fecha_rango[0]) & (df_f['fecha_limite_dt'].dt.date <= fecha_rango[1])]

if ccaa_sel: df_f = df_f[df_f['comunidad_autonoma'].isin(ccaa_sel)]
if prov_sel: df_f = df_f[df_f['provincia'].isin(prov_sel)]
if muni_sel: df_f = df_f[df_f['municipio'].isin(muni_sel)]
if organo_sel: df_f = df_f[df_f['organo_contratante'].isin(organo_sel)]

if cpv_2dig.strip():
    prefijo = cpv_2dig.strip()
    df_f = df_f[df_f['cpv'].apply(lambda x: any(c.strip().startswith(prefijo) for c in str(x).split(',')) if x else False)]

# --- CABECERA Y 4 KPIS ENMARCADOS ---
st.title("🏛️ Monitor de Licitaciones")
st.caption("Plataforma de Contratación del Sector Público")

ultima_act = df['fecha_act_dt'].max()
fecha_act_fmt = ultima_act.strftime("%d/%m/%Y %H:%M") if pd.notnull(ultima_act) else "No disponible"
volumen_total = df_f['pbl_sin_iva'].sum() if not df_f.empty and 'pbl_sin_iva' in df_f.columns else 0.0
presupuesto_medio = df_f['pbl_sin_iva'].mean() if not df_f.empty and len(df_f) > 0 and 'pbl_sin_iva' in df_f.columns else 0.0

kpi1, kpi2, kpi3, kpi4 = st.columns(4)

with kpi1:
    st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid">{len(df_f)}</div><div class="metric-lbl-grid">Licitaciones Filtradas</div></div>', unsafe_allow_html=True)
with kpi2:
    st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid">{volumen_total:,.2f} €</div><div class="metric-lbl-grid">Volumen Total (sin IVA)</div></div>', unsafe_allow_html=True)
with kpi3:
    st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid">{presupuesto_medio:,.2f} €</div><div class="metric-lbl-grid">Presupuesto Medio (sin IVA)</div></div>', unsafe_allow_html=True)
with kpi4:
    st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid" style="font-size:1.15rem; margin-top:2px;">{fecha_act_fmt}</div><div class="metric-lbl-grid">Última Actualización BD</div></div>', unsafe_allow_html=True)

st.divider()

# --- VERIFICACIÓN DE RESULTADOS VACÍOS ---
if df_f.empty:
    st.warning("⚠️ No se ha encontrado ninguna licitación que coincida con los filtros aplicados. Prueba a relajar los criterios de búsqueda.")
else:
    st.write("")

    # --- VISTA POR PESTAÑAS ---
    tab_tarjetas, tab_mapa = st.tabs(["🗂️ Vista Tarjetas", "🗺️ Mapa Geográfico"])

    with tab_mapa:
        st.subheader("📍 Ubicación de las licitaciones")
        df_mapa = df_f.dropna(subset=['latitud', 'longitud']).copy()
        
        if not df_mapa.empty:
            df_mapa['pbl_fmt'] = df_mapa['pbl_sin_iva'].apply(lambda x: f"{x:,.2f} €" if pd.notnull(x) else "No especificado")
            df_mapa['fecha_limite_fmt'] = df_mapa['fecha_limite'].fillna('No especificada')
            df_mapa['municipio_clean'] = df_mapa['municipio'].fillna('No especificado')
            df_mapa['provincia_clean'] = df_mapa['provincia'].fillna('No especificada')
            df_mapa['ccaa_clean'] = df_mapa['comunidad_autonoma'].fillna('No especificada')
            df_mapa['expediente_clean'] = df_mapa['expediente'].fillna('N/A')
            
            # --- SEPARAR PUNTOS SOLAPADOS (JITTER GEOGRÁFICO) ---
            np.random.seed(42)
            df_mapa['lat_j'] = df_mapa['latitud'] + np.random.normal(0, 0.00008, size=len(df_mapa))
            df_mapa['lon_j'] = df_mapa['longitud'] + np.random.normal(0, 0.00008, size=len(df_mapa))

            # --- TAMAÑO FINO Y DINÁMICO POR PERCENTILES (1px a 8px) ---
            valid_pbl = df_mapa['pbl_sin_iva'].replace(0, np.nan).dropna()
            if not valid_pbl.empty and len(valid_pbl) > 1:
                p5, p95 = np.percentile(valid_pbl, [5, 95])
                def calcular_radio_percentil(val):
                    if pd.isnull(val) or val <= 0:
                        return 2.0
                    clipped = max(p5, min(p95, val))
                    norm = (clipped - p5) / (p95 - p5) if p95 > p5 else 0.5
                    return 1.0 + norm * 7.0
                df_mapa['radius_px'] = df_mapa['pbl_sin_iva'].apply(calcular_radio_percentil)
            else:
                df_mapa['radius_px'] = 3.0
            
            layer = pdk.Layer(
                "ScatterplotLayer", 
                data=df_mapa, 
                get_position=["lon_j", "lat_j"], 
                get_color="[13, 110, 253, 160]", 
                get_line_color="[10, 80, 200, 230]", 
                line_width_min_pixels=0.8, 
                stroked=True, 
                radius_units="'pixels'", 
                get_radius="radius_px", 
                radius_min_pixels=1, 
                radius_max_pixels=10, 
                pickable=True, 
                auto_highlight=True
            )
            
            view_state = pdk.ViewState(latitude=40.4168, longitude=-3.7038, zoom=5.4, pitch=0)
            
            # --- TOOLTIP CON LA INFORMACIÓN EXACTA REQUERIDA ---
            tooltip = {
                "html": "<b>{titulo}</b><br/>"
                        "🏛️ <b>Órgano de contratación:</b> {organo_contratante}<br/>"
                        "📍 <b>Ubicación:</b> {municipio_clean} ({provincia_clean}, {ccaa_clean})<br/>"
                        "💶 <b>PBL sin IVA:</b> {pbl_fmt}<br/>"
                        "📅 <b>Fecha presentación oferta:</b> {fecha_limite_fmt}<br/>"
                        "📁 <b>Código expediente:</b> {expediente_clean}",
                "style": {"backgroundColor": "#1a252c", "color": "white", "fontSize": "12px", "padding": "10px", "borderRadius": "6px", "maxWidth": "340px"}
            }
            
            r = pdk.Deck(layers=[layer], initial_view_state=view_state, tooltip=tooltip, map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json")
            st.pydeck_chart(r)

            # --- PANEL DE ACCESO RÁPIDO BAJO EL MAPA ---
            with st.expander("📋 Ver listado y accesos directos de las licitaciones en este mapa"):
                st.write("Selecciona una licitación para acceder directamente a su ficha oficial:")
                for _, r_map in df_mapa.iterrows():
                    col_m1, col_m2 = st.columns([5, 1])
                    with col_m1:
                        st.markdown(f"**{r_map['titulo']}** — *{r_map['organo_contratante']}* ({r_map['pbl_fmt']})")
                    with col_m2:
                        if r_map['url_licitacion']:
                            st.markdown(f'<a href="{r_map["url_licitacion"]}" target="_blank" class="external-link-btn">🔗 Abrir</a>', unsafe_allow_html=True)
        else:
            st.info("No hay licitaciones con coordenadas geográficas disponibles para mostrar en el mapa.")

    with tab_tarjetas:
        # --- BARRA DE ORDENACIÓN ---
        col_ord1, col_ord2 = st.columns([2, 3])
        with col_ord1:
            criterio_orden = st.selectbox(
                "🔃 Ordenar tarjetas por:",
                ["Fecha límite (Más cercana)", "Fecha límite (Más lejana)", "Presupuesto (Mayor a menor)", "Presupuesto (Menor a mayor)"],
                key="select_orden"
            )
        
        if "Fecha límite (Más cercana)" in criterio_orden:
            df_f = df_f.sort_values(by='fecha_limite_dt', ascending=True, na_position='last')
        elif "Fecha límite (Más lejana)" in criterio_orden:
            df_f = df_f.sort_values(by='fecha_limite_dt', ascending=False, na_position='last')
        elif "Presupuesto (Mayor a menor)" in criterio_orden:
            df_f = df_f.sort_values(by='pbl_sin_iva', ascending=False, na_position='last')
        elif "Presupuesto (Menor a mayor)" in criterio_orden:
            df_f = df_f.sort_values(by='pbl_sin_iva', ascending=True, na_position='last')

        st.write("")

        # --- LÓGICA DE PAGINACIÓN ---
        ITEMS_POR_PAGINA = 12
        total_items = len(df_f)
        total_paginas = max(1, (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA)
        
        if 'pagina_actual' not in st.session_state:
            st.session_state.pagina_actual = 1
            
        if st.session_state.pagina_actual > total_paginas:
            st.session_state.pagina_actual = total_paginas

        # --- BARRA DE PAGINACIÓN SUPERIOR ---
        col_p1, col_p2, col_p3 = st.columns([1, 2, 1])
        with col_p1:
            if st.button("⬅️ Anterior", key="btn_ant_sup", use_container_width=True, disabled=(st.session_state.pagina_actual <= 1)):
                st.session_state.pagina_actual -= 1
                st.rerun()
        with col_p2:
            st.markdown(f"<p style='text-align:center; font-weight:600; margin-top: 6px;'>Página {st.session_state.pagina_actual} de {total_paginas} (Total: {total_items} licitaciones)</p>", unsafe_allow_html=True)
        with col_p3:
            if st.button("Siguiente ➡️", key="btn_sig_sup", use_container_width=True, disabled=(st.session_state.pagina_actual >= total_paginas)):
                st.session_state.pagina_actual += 1
                st.rerun()

        st.divider()

        inicio = (st.session_state.pagina_actual - 1) * ITEMS_POR_PAGINA
        fin = inicio + ITEMS_POR_PAGINA
        df_pagina = df_f.iloc[inicio:fin]

        for i in range(0, len(df_pagina), 3):
            cols = st.columns(3)
            lote = df_pagina.iloc[i:i+3]
            
            for col, (_, r) in zip(cols, lote.iterrows()):
                st_txt, badge_cls = MAPA_ESTADOS.get(r['estado'], (r['estado'], 'badge-res'))
                link_html = f'<a href="{r["url_licitacion"]}" target="_blank" class="external-link-btn" title="Ver ficha en la Plataforma">🔗</a>' if r['url_licitacion'] else ''
                
                with col:
                    with st.container(border=True):
                        st.markdown(f"""
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
                            <span class="{badge_cls}">{st_txt}</span>
                            <div>{link_html}</div>
                        </div>
                        <h5 style="margin: 10px 0 6px 0; color: #1a252c; line-height: 1.35; font-size: 0.95rem; min-height: 2.7em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
                            {r['titulo'] or 'Sin título'}
                        </h5>
                        <p style="margin: 0; font-size: 0.8rem; color: #495057; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                            🏛️ <b>{r['organo_contratante'] or 'Organismo N/A'}</b>
                        </p>
                        <p style="margin: 2px 0 4px 0; font-size: 0.78rem; color: #6c757d;">
                            📍 {r['municipio'] or 'N/A'} ({r['provincia'] or 'N/A'}) | 📦 <b>{r['tipo_contrato_desc']}</b>
                        </p>
                        <p style="margin: 0 0 10px 0; font-size: 0.75rem; color: #8b949e;">
                            <b>Exp:</b> {r['expediente'] or 'N/A'} | <b>CPV:</b> {r['cpv'] or 'N/A'}
                        </p>
                        """, unsafe_allow_html=True)
                        
                        mc1, mc2 = st.columns(2)
                        with mc1:
                            st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid" style="font-size: 0.85rem;">{r["pbl_sin_iva"]:,.2f} €</div><div class="metric-lbl-grid">PBL SIN IVA</div></div>', unsafe_allow_html=True)
                        with mc2:
                            st.markdown(f'<div class="metric-box-grid"><div class="metric-val-grid" style="font-size: 0.82rem; color: #495057;">{r["fecha_limite"] or "No especificada"}</div><div class="metric-lbl-grid">FECHA PRESENTACIÓN</div></div>', unsafe_allow_html=True)

                        # DESPLEGABLE 1: DOCUMENTACIÓN
                        with st.expander("📄 Ver documentación"):
                            docs = json.loads(r['documentos_adjuntos']) if r['documentos_adjuntos'] else []
                            if docs:
                                st.write("**Documentos descargables:**")
                                for d in docs:
                                    st.markdown(f"📥 [{d['tipo']} - {d['nombre']}]({d['url']})")
                            else:
                                st.write("Sin documentos adjuntos directos.")

                        # DESPLEGABLE 2: RESUMEN IA 🧠
                        with st.expander("🧠 Resumen Técnico IA"):
                            raw_resumen = r.get('resumen_ia')
                            
                            tiene_resumen = False
                            if pd.notnull(raw_resumen):
                                s_val = str(raw_resumen).strip().lower()
                                if s_val and s_val != 'nan' and s_val != 'none':
                                    tiene_resumen = True

                            if tiene_resumen:
                                try:
                                    if isinstance(raw_resumen, dict):
                                        res_ia = raw_resumen
                                    else:
                                        cleaned_r = str(raw_resumen).strip()
                                        if cleaned_r.startswith("```json"):
                                            cleaned_r = cleaned_r[7:]
                                        if cleaned_r.endswith("```"):
                                            cleaned_r = cleaned_r[:-3]
                                        res_ia = json.loads(cleaned_r.strip())
                                        
                                    if isinstance(res_ia.get('alcance_tecnico'), str) and res_ia.get('alcance_tecnico').strip().startswith('{'):
                                        try:
                                            nested = json.loads(res_ia.get('alcance_tecnico'))
                                            if isinstance(nested, dict):
                                                res_ia = nested
                                        except:
                                            pass

                                    st.markdown(f"🏗️ **Alcance Técnico:**\n{res_ia.get('alcance_tecnico', '• No especificado')}")
                                    st.divider()
                                    st.markdown(f"⚖️ **Criterios de Puntuación:**\n{res_ia.get('criterios_puntuacion', '• No especificado')}")
                                    st.markdown(f"💼 **Solvencia Requerida / Clasificación:**\n{res_ia.get('solvencia_requerida', '• No especificado')}")
                                    st.markdown(f"👨‍💼 **Equipo Técnico y Titulaciones:**\n{res_ia.get('equipo_y_titulaciones', '• No especificado')}")
                                    st.markdown(f"🛡️ **Seguro RC:**\n{res_ia.get('seguro_rc', '• No especificado')}")
                                    st.markdown(f"🏦 **Garantías y Depósitos:**\n{res_ia.get('garantia', '• No especificado')}")
                                    st.markdown(f"⚠️ **Condicionantes y Plazos:**\n{res_ia.get('condicionantes_destacados', '• No especificado')}")
                                except Exception as json_err:
                                    st.markdown(f"🏗️ **Alcance Técnico:**\n{raw_resumen}")
                            else:
                                st.write("Licitación pendiente de análisis ejecutivo estricto. Revisa si hay texto descargado.")
                                btn_key = f"btn_directo_{r['id']}"
                                
                                if st.button("🚀 Lanzar Análisis Ejecutivo IA", key=btn_key, use_container_width=True):
                                    msg_container = st.empty()
                                    msg_container.info("⏳ Extrayendo ponderaciones numéricas exactas...")
                                    
                                    resultado, error_msg = analizar_licitacion_directo(r.to_dict())
                                    
                                    if error_msg:
                                        msg_container.error(error_msg)
                                    elif resultado:
                                        msg_container.success("¡Análisis de puntos completado!")
                                        st.cache_data.clear()
                                        st.rerun()
                                    else:
                                        msg_container.error("⚠️ La IA finalizó pero no devolvió datos.")

        # --- BARRA DE PAGINACIÓN INFERIOR ---
        st.divider()
        col_inf1, col_inf2, col_inf3 = st.columns([1, 2, 1])
        with col_inf1:
            if st.button("⬅️ Anterior", key="btn_ant_inf", use_container_width=True, disabled=(st.session_state.pagina_actual <= 1)):
                st.session_state.pagina_actual -= 1
                st.rerun()
        with col_inf2:
            st.markdown(f"<p style='text-align:center; font-weight:600; margin-top: 6px;'>Página {st.session_state.pagina_actual} de {total_paginas} (Total: {total_items} licitaciones)</p>", unsafe_allow_html=True)
        with col_inf3:
            if st.button("Siguiente ➡️", key="btn_sig_inf", use_container_width=True, disabled=(st.session_state.pagina_actual >= total_paginas)):
                st.session_state.pagina_actual += 1
                st.rerun()