import html
import streamlit as st
import streamlit.components.v1 as components
import altair as alt
import sqlite3
import pandas as pd
import json
import pydeck as pdk
import numpy as np
import os
import requests
from datetime import date
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from google import genai
from google.genai import types

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Licitaciones | Dashboard", layout="wide", page_icon="🏛️", initial_sidebar_state="collapsed")

# --- RUTA DE LA BASE DE DATOS ADAPTADA (LOCAL Y NUBE) ---
DB_PATH = os.getenv("LICITACIONES_DB_PATH", "licitaciones.db")

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
    .external-link-btn { color: #0d6efd; text-decoration: none; font-size: 0.9rem; font-weight: 600; background-color: #e7f1ff; padding: 3px 8px; border-radius: 6px; border: 1px solid #b6d4fe; display: inline-block; }
    .external-link-btn:hover { background-color: #0d6efd; color: white; }
    .maps-btn { color: #198754; text-decoration: none; font-size: 0.9rem; font-weight: 600; background-color: #e8f5e9; padding: 3px 8px; border-radius: 6px; border: 1px solid #c3e6cb; display: inline-block; }
    .maps-btn:hover { background-color: #198754; color: white; }
    
    .metric-box-grid { background-color: #ffffff; border-radius: 8px; padding: 12px 10px; text-align: center; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    .metric-val-grid { font-size: 1.35rem; font-weight: 800; color: #0d6efd; letter-spacing: -0.5px; }
    .metric-lbl-grid { font-size: 0.72rem; color: #475569; text-transform: uppercase; font-weight: 700; margin-top: 4px; }
    .card-metric { height: 100px !important; display: flex !important; flex-direction: column !important; justify-content: center !important; box-sizing: border-box !important; }
    .top-kpi { height: 112px !important; display: flex !important; flex-direction: column !important; justify-content: center !important; box-sizing: border-box !important; }
    .company-card { margin-top: 2rem; padding: 1.4rem; border: 1px solid #dbe3ec; border-radius: 12px; background: #ffffff; box-shadow: 0 2px 8px rgba(15,23,42,0.06); }
    .company-heading { display: flex; align-items: center; gap: 0.8rem; margin-bottom: 0.55rem; }
    .company-logo { width: 52px; height: 52px; flex: 0 0 52px; display: block; border-radius: 10px; object-fit: cover; }
    .company-name { margin: 0; color: #1e293b; font-size: 1.15rem; font-weight: 800; }
    .company-copy { margin: 0.35rem 0 1rem; color: #475569; line-height: 1.5; }
    .company-actions { display: flex; flex-wrap: wrap; gap: 0.55rem; }
    .company-action { min-height: 42px; display: inline-flex; align-items: center; justify-content: center; padding: 0.6rem 0.9rem; border: 1px solid #bfd2ea; border-radius: 8px; background: #f8fbff; color: #0b5ed7 !important; text-decoration: none !important; font-weight: 700; box-sizing: border-box; }
    .company-action:hover { background: #0d6efd; border-color: #0d6efd; color: white !important; }
    .dashboard-top-header { display: grid; grid-template-columns: minmax(0, 1fr) minmax(460px, 0.95fr); align-items: center; gap: 1.5rem; margin-bottom: 1rem; }
    .dashboard-top-header .company-card { margin-top: 0; padding: 1rem; }
    .dashboard-top-header .company-copy { margin: 0.25rem 0 0.75rem; }
    .dashboard-top-header .company-action { min-height: 38px; padding: 0.45rem 0.7rem; font-size: 0.82rem; }

    .row-widget.stHorizontal { align-items: stretch !important; }
    div[data-testid="stVerticalBlock"]:has(> div.stContainer) { height: 100%; }
    div[data-testid="stContainer"] { height: 100% !important; display: flex !important; flex-direction: column !important; justify-content: space-between !important; }

    div[data-testid="stExpander"] { margin-top: 14px !important; }
    .stMarkdown a.anchor-link, [data-testid="stHeaderActionElements"] { display: none !important; }
    @media (max-width: 768px) {
        .block-container { padding: 2.5rem 0.75rem 2rem !important; }
        div[data-testid="stHorizontalBlock"] { flex-direction: column !important; gap: 0.45rem !important; }
        div[data-testid="stHorizontalBlock"]:has(.metric-box-grid) > div[data-testid="stColumn"] { padding-bottom: 0.45rem !important; box-sizing: border-box !important; }
        .metric-box-grid { border: 3px solid #f8f9fa !important; box-shadow: inset 0 0 0 1px #e2e8f0 !important; }
        h1 { margin-top: 0 !important; line-height: 1.2 !important; }
        .company-card { padding: 1rem !important; }
        .company-actions { flex-direction: column !important; }
        .company-action { width: 100% !important; }
        .dashboard-top-header { grid-template-columns: minmax(0, 1fr) !important; gap: 0.85rem !important; }
        div[data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; max-width: 100% !important; flex: 0 0 auto !important; }\n        div[data-testid="stHorizontalBlock"], .metric-box-grid, div[data-testid="stExpander"] { width: 100% !important; max-width: 100% !important; box-sizing: border-box !important; }
        h1 { font-size: 1.8rem !important; }
    }
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

def texto_seguro(valor, fallback="N/A"):
    if valor is None or (not isinstance(valor, (list, dict)) and pd.isna(valor)):
        valor = fallback
    return html.escape(str(valor))

def url_externa_segura(valor):
    if not isinstance(valor, str):
        return None
    try:
        parsed = urlparse(valor.strip())
    except ValueError:
        return None
    return valor.strip() if parsed.scheme in {"http", "https"} and parsed.netloc else None

def formato_eur(valor):
    if pd.isna(valor):
        return "No especificado"
    return f"{float(valor):,.2f}".translate(str.maketrans({",": ".", ".": ","})) + " €"

def formato_fecha(valor):
    fecha = pd.to_datetime(valor, errors="coerce")
    return "No especificada" if pd.isna(fecha) else fecha.strftime("%d/%m/%Y · %H:%M")

def texto_dias_restantes(valor):
    fecha = pd.to_datetime(valor, errors="coerce")
    if pd.isna(fecha):
        return "Plazo no disponible"
    dias = (fecha.date() - date.today()).days
    if dias > 1:
        return f"Faltan {dias} días"
    if dias == 1:
        return "Falta 1 día"
    if dias == 0:
        return "Finaliza hoy"
    if dias == -1:
        return "Finalizó hace 1 día"
    return f"Finalizó hace {abs(dias)} días"

def texto_antiguedad(valor):
    if pd.isna(valor):
        return "Actualización no disponible"
    dias = max(0, (date.today() - valor.date()).days)
    if dias == 0:
        return "Actualizada hoy"
    if dias == 1:
        return "Hace 1 día"
    return f"Hace {dias} días"

@st.cache_data(ttl=300, show_spinner="Cargando licitaciones…")
def cargar_datos(db_mtime):
    del db_mtime
    uri = f"file:{os.path.abspath(DB_PATH).replace('\\', '/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        df = pd.read_sql_query("SELECT * FROM licitaciones", conn)

    df['fecha_limite_dt'] = pd.to_datetime(df['fecha_limite'].str.slice(0, 10), errors='coerce', utc=True)
    df['fecha_act_dt'] = pd.to_datetime(df['fecha_actualizacion'], errors='coerce', utc=True)
    df['tipo_contrato_desc'] = df['tipo_contrato'].map(MAPA_TIPOS).fillna('Otros')
    return df

try:
    df = cargar_datos(os.path.getmtime(DB_PATH))
except Exception as e:
    st.error(f"❌ No se pudo conectar a la base de datos en {DB_PATH}. Error: {e}")
    st.stop()

max_pbl_value = df['pbl_sin_iva'].max()
max_pbl_db = float(max_pbl_value) if pd.notnull(max_pbl_value) and max_pbl_value > 0 else 200000.0

fechas_validas = df['fecha_limite_dt'].dropna()
f_min_db = fechas_validas.min().date() if not fechas_validas.empty else date.today()
f_max_db = fechas_validas.max().date() if not fechas_validas.empty else date.today()
hoy = date.today()
f_inicio_default = hoy if hoy <= f_max_db else f_min_db

if "f_cpv" not in st.session_state:
    st.session_state["f_cpv"] = "71"
if "f_estado" not in st.session_state:
    st.session_state["f_estado"] = ["PUB"]
if "f_pbl_max" not in st.session_state:
    st.session_state["f_pbl_max"] = 200000.0
if "f_fecha" not in st.session_state:
    st.session_state["f_fecha"] = (f_inicio_default, f_max_db)

st.sidebar.title("🎛️ Filtros Avanzados")

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

busqueda_texto = st.sidebar.text_input("🔍 Palabras clave (título, expediente...):", key="f_texto")
tipos_list = sorted([x for x in df['tipo_contrato_desc'].unique() if x])
tipo_sel = st.sidebar.multiselect("📦 Tipo de Contrato:", tipos_list, key="f_tipo")
cpv_2dig = st.sidebar.text_input("🏷️ CPV (2 dígitos):", max_chars=2, key="f_cpv")

estados_unicos = df['estado'].dropna().unique().tolist()
opciones_estado = {c: MAPA_ESTADOS.get(c, (c, ''))[0] for c in estados_unicos}
estados_sel = st.sidebar.multiselect("📌 Estado:", list(opciones_estado.keys()), format_func=lambda x: opciones_estado[x], key="f_estado")

ccaa_list = sorted([x for x in df['comunidad_autonoma'].dropna().unique() if x])
ccaa_sel = st.sidebar.multiselect("🗺️ Comunidad Autónoma:", ccaa_list, key="f_ccaa")

prov_list = sorted([x for x in df[df['comunidad_autonoma'].isin(ccaa_sel)]['provincia'].dropna().unique() if x]) if ccaa_sel else sorted([x for x in df['provincia'].dropna().unique() if x])
prov_sel = st.sidebar.multiselect("📍 Provincia:", prov_list, key="f_prov")

muni_list = sorted([x for x in df[df['provincia'].isin(prov_sel)]['municipio'].dropna().unique() if x]) if prov_sel else sorted([x for x in df['municipio'].dropna().unique() if x])
muni_sel = st.sidebar.multiselect("🏙️ Municipio:", muni_list, key="f_muni")

st.sidebar.markdown("💶 **Presupuesto Base sin IVA (€):**")
pbl_min_val = st.sidebar.number_input("Mínimo €", min_value=0.0, step=10000.0, key="f_pbl_min")
pbl_max_val = st.sidebar.number_input("Máximo €", min_value=0.0, value=200000.0, step=50000.0, key="f_pbl_max")

if not fechas_validas.empty:
    fecha_rango = st.sidebar.date_input("📅 Fecha Límite Presentación:", min_value=f_min_db, max_value=f_max_db, key="f_fecha")
else:
    fecha_rango = None

organos = sorted([x for x in df['organo_contratante'].dropna().unique() if x])
organo_sel = st.sidebar.multiselect("🏛️ Órgano de Contratación:", organos, key="f_organo")

df_f = df.copy()

if busqueda_texto.strip():
    q = busqueda_texto.lower().strip()
    df_f = df_f[df_f['titulo'].str.lower().str.contains(q, na=False, regex=False) | df_f['expediente'].str.lower().str.…9287 tokens truncated…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AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38Ampg7zo38KYMNouD/2Q==" alt="Logotipo de Landa Consultoría y Proyectos">
        <div>
            <p class="company-name">Landa Consultoría y Proyectos</p>
            <span style="color:#64748b; font-size:0.82rem;">Expertos en proyectos y obras de ingeniería civil</span>
        </div>
    </div>
    <p class="company-copy"><b>Te ayudamos a preparar tus licitaciones y ejecutar tus proyectos.</b><br>Cuéntanos qué necesitas y estudiaremos cómo ayudarte.</p>
    <div class="company-actions">
        <a class="company-action" href="mailto:info@landaconsultores.com">✉️ info@landaconsultores.com</a>
        <a class="company-action" href="tel:+34681881782">📞 681 881 782</a>
        <a class="company-action" href="https://www.landaconsultores.com" target="_blank" rel="noopener noreferrer">🌐 Visitar la web</a>
    </div>
</div>
""", unsafe_allow_html=True)

# Mantiene una navegación vertical predecible tras los reruns de Streamlit.
components.html("""
<script>
(() => {
    const parentWindow = window.parent;
    const parentDocument = parentWindow.document;
    const storageKey = "dashboard_licitaciones_scroll";
    const scrollContainer = parentDocument.querySelector('[data-testid="stMain"]');

    const arrangeTopHeader = () => {
        const heading = Array.from(parentDocument.querySelectorAll("h1"))
            .find((element) => (element.textContent || "").includes("Monitor de Licitaciones"));
        const companyCard = parentDocument.querySelector(".company-card");
        if (!heading || !companyCard || companyCard.closest(".dashboard-top-header")) return;

        const titleBlock = heading.closest('[data-testid="stElementContainer"]') || heading.parentElement;
        const captionBlock = titleBlock?.nextElementSibling;
        if (!titleBlock || !titleBlock.parentNode) return;

        const wrapper = parentDocument.createElement("div");
        wrapper.className = "dashboard-top-header";
        const titleGroup = parentDocument.createElement("div");
        titleGroup.className = "dashboard-title-group";

        titleBlock.parentNode.insertBefore(wrapper, titleBlock);
        wrapper.appendChild(titleGroup);
        titleGroup.appendChild(titleBlock);
        if (captionBlock && (captionBlock.textContent || "").includes("Plataforma de Contratación")) {
            titleGroup.appendChild(captionBlock);
        }
        wrapper.appendChild(companyCard);
    };

    arrangeTopHeader();
    parentWindow.setTimeout(arrangeTopHeader, 150);

    const savedScroll = parentWindow.sessionStorage.getItem(storageKey);

    const restoreScroll = () => {
        if (!scrollContainer || !savedScroll) return;
        const saved = savedScroll;

        if (saved === "tarjetas") {
            const pageIndicator = Array.from(parentDocument.querySelectorAll("p"))
                .find((element) => /^Página \d+ de \d+/.test((element.textContent || "").trim()));
            const target = pageIndicator?.closest('[data-testid="stHorizontalBlock"]') || pageIndicator;
            if (target) {
                const targetTop = target.getBoundingClientRect().top
                    - scrollContainer.getBoundingClientRect().top
                    + scrollContainer.scrollTop;
                scrollContainer.scrollTo({ top: targetTop, behavior: "auto" });
            }
        } else {
            const position = Number(saved);
            if (Number.isFinite(position)) scrollContainer.scrollTo({ top: position, behavior: "auto" });
        }
    };

    const bindButtons = () => {
        parentDocument.querySelectorAll("button").forEach((button) => {
            if (button.dataset.licitacionesScrollBound === "1") return;
            const label = (button.innerText || button.textContent || "").trim();

            if (label.includes("Siguiente") || label.includes("Anterior")) {
                button.addEventListener("pointerdown", () => {
                    parentWindow.sessionStorage.setItem(storageKey, "tarjetas");
                }, { capture: true });
                button.dataset.licitacionesScrollBound = "1";
            }
        });
    };

    requestAnimationFrame(() => requestAnimationFrame(restoreScroll));
    setTimeout(restoreScroll, 250);
    setTimeout(restoreScroll, 700);
    setTimeout(restoreScroll, 1400);
    setTimeout(() => parentWindow.sessionStorage.removeItem(storageKey), 1600);
    bindButtons();
    const observer = new MutationObserver(bindButtons);
    observer.observe(parentDocument.body, { childList: true, subtree: true });
    setTimeout(() => observer.disconnect(), 5000);
})();
</script>
""", height=0, scrolling=False)

