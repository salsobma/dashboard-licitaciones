import html
import hashlib
# Revisión de despliegue: filtro del Radar protegido ante resultados vacíos.
import streamlit as st
import streamlit.components.v1 as components
import altair as alt
import sqlite3
import xml.etree.ElementTree as ET
import pandas as pd
import json
import pydeck as pdk
import numpy as np
import os
import requests
import textwrap
import time
import unicodedata
from datetime import date
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from google import genai
from google.genai import types
from feed_parser import extraer_adjudicacion

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(
    page_title="LandAI Licitaciones",
    layout="wide",
    page_icon="static/icons/icon-192.png",
    initial_sidebar_state="collapsed",
)

# Metadatos de instalación: Chrome/Android usa este manifiesto al crear el acceso directo.
components.html("""
<script>
(() => {
    // El componente vive dentro de un iframe; el manifiesto debe estar en el documento superior.
    const hostDocument = window.top.document;
    const assetBase = new URL("app/static/", window.top.location.origin + "/").toString();
    const ensureLink = (rel, href, extras = {}) => {
        let element = rel === "manifest"\n            ? hostDocument.querySelector('link[rel="manifest"]')\n            : hostDocument.querySelector(`link[data-landai="${rel}"]`);
        if (!element) { element = hostDocument.createElement("link"); element.dataset.landai = rel; hostDocument.head.appendChild(element); }
        element.rel = rel; element.href = href;
        Object.entries(extras).forEach(([key, value]) => element.setAttribute(key, value));
    };
    ensureLink("manifest", assetBase + "manifest.json");
    ensureLink("icon", assetBase + "icons/icon-192.png", { sizes: "192x192", type: "image/png" });
    ensureLink("apple-touch-icon", assetBase + "icons/icon-192.png", { sizes: "192x192" });
    let theme = hostDocument.querySelector('meta[name="theme-color"][data-landai-theme]');
    if (!theme) { theme = hostDocument.createElement("meta"); theme.name = "theme-color"; theme.dataset.landaiTheme = "true"; hostDocument.head.appendChild(theme); }
    theme.content = "#3d3739";
})();
</script>
""", height=0)


# --- RUTA DE LA BASE DE DATOS ADAPTADA (LOCAL Y NUBE) ---
DB_PATH = os.getenv("LICITACIONES_DB_PATH", "licitaciones.db")

# --- ACCESO PREMIUM Y FAVORITOS COMPARTIDOS ---
def _secreto(nombre, defecto=""):
    """Lee configuración desde Streamlit Secrets o variables de entorno."""
    try:
        premium = st.secrets.get("premium", {})
        valor = premium.get(nombre, defecto)
    except Exception:
        valor = defecto
    return os.getenv(f"PREMIUM_{nombre.upper()}", str(valor or defecto)).strip()


def _secreto_microsoft(nombre, defecto=""):
    """Reutiliza las credenciales OIDC para las llamadas de Microsoft Graph."""
    try:
        auth = st.secrets.get("auth", {})
        microsoft = auth.get("microsoft", {})
        valor = microsoft.get(nombre, defecto)
    except Exception:
        valor = defecto
    return str(valor or defecto).strip()


PREMIUM_EMAIL = _secreto("allowed_email").lower()
PREMIUM_LIST_URL = _secreto("sharepoint_list_url")
GRAPH_TENANT_ID = _secreto("tenant_id")
GRAPH_CLIENT_ID = _secreto("graph_client_id")
GRAPH_CLIENT_SECRET = _secreto_microsoft("client_secret") or _secreto(
    "graph_client_secret"
)
GRAPH_SITE_ID = _secreto("site_id")
GRAPH_LIST_ID = _secreto("list_id")


def _correo_usuario():
    if not getattr(st.user, "is_logged_in", False):
        return ""
    for campo in ("email", "preferred_username", "upn"):
        valor = getattr(st.user, campo, "")
        if valor:
            return str(valor).strip().lower()
    return ""


USUARIO_CONECTADO = bool(getattr(st.user, "is_logged_in", False))
CORREO_USUARIO = _correo_usuario()
ES_PREMIUM = bool(
    USUARIO_CONECTADO and PREMIUM_EMAIL and CORREO_USUARIO == PREMIUM_EMAIL
)
LISTS_CONFIGURADO = all(
    [GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_SITE_ID, GRAPH_LIST_ID]
)


def _token_graph():
    respuesta = requests.post(
        f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": GRAPH_CLIENT_ID,
            "client_secret": GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    respuesta.raise_for_status()
    return respuesta.json()["access_token"]


def _graph_headers():
    return {
        "Authorization": f"Bearer {_token_graph()}",
        "Content-Type": "application/json",
    }


def _graph_lista_base():
    """Construye la ruta de Graph desde la URL visible de la lista.

    La referencia por ruta evita problemas con identificadores compuestos de sitio.
    """
    url_lista = urlparse(PREMIUM_LIST_URL)
    ruta_sitio = url_lista.path.split("/Lists/", 1)[0].rstrip("/")
    if url_lista.netloc and ruta_sitio:
        referencia_sitio = f"{url_lista.netloc}:{ruta_sitio}:"
    else:
        referencia_sitio = GRAPH_SITE_ID
    return (
        f"https://graph.microsoft.com/v1.0/sites/{referencia_sitio}/lists/"
        f"{GRAPH_LIST_ID}/items"
    )


def _detalle_error_graph(error):
    """Resume un error de Graph sin exponer tokens ni secretos."""
    respuesta = getattr(error, "response", None)
    if respuesta is None:
        return type(error).__name__
    codigo_http = getattr(respuesta, "status_code", "")
    try:
        payload = respuesta.json()
        detalle = payload.get("error", {}) if isinstance(payload, dict) else {}
        codigo = str(detalle.get("code", "")).strip()
        mensaje = str(detalle.get("message", "")).strip()
        partes = [str(codigo_http), codigo, mensaje]
        return " · ".join(parte for parte in partes if parte)[:500]
    except (ValueError, AttributeError):
        return str(codigo_http) or type(error).__name__


@st.cache_data(ttl=30, show_spinner=False)
def cargar_favoritos_compartidos():
    """Devuelve {id_licitacion: id_elemento_lista}."""
    if not LISTS_CONFIGURADO:
        return {}
    url = _graph_lista_base()
    favoritos = {}
    while url:
        respuesta = requests.get(
            url,
            headers=_graph_headers(),
            params={"$expand": "fields", "$top": "999"} if not favoritos else None,
            timeout=15,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
        for elemento in datos.get("value", []):
            licitacion_id = str(elemento.get("fields", {}).get("LicitacionId", "")).strip()
            if licitacion_id:
                favoritos[licitacion_id] = str(elemento["id"])
        url = datos.get("@odata.nextLink")
    return favoritos


def alternar_favorito(licitacion):
    licitacion_id = str(licitacion.get("id") or "").strip()
    if not licitacion_id or not LISTS_CONFIGURADO:
        return
    favoritos = cargar_favoritos_compartidos()
    base = _graph_lista_base()
    if licitacion_id in favoritos:
        respuesta = requests.delete(
            f"{base}/{favoritos[licitacion_id]}", headers=_graph_headers(), timeout=15
        )
    else:
        titulo = str(licitacion.get("titulo") or licitacion.get("expediente") or licitacion_id)
        campos_base = {
            "Title": titulo[:255],
            "LicitacionId": licitacion_id,
        }
        pbl_sin_iva = pd.to_numeric(licitacion.get("pbl_sin_iva"), errors="coerce")
        estado_lista = MAPA_ESTADOS.get(
            licitacion.get("estado"), (licitacion.get("estado") or "", "")
        )[0]
        campos_ampliados = {
            **campos_base,
            "PblSinIva": float(pbl_sin_iva) if pd.notna(pbl_sin_iva) else 0,
            "Estado": str(estado_lista)[:255],
            "LinkPlataforma": str(licitacion.get("url_licitacion") or ""),
        }
        respuesta = requests.post(
            base,
            headers=_graph_headers(),
            json={"fields": campos_ampliados},
            timeout=15,
        )
        # Hasta que se creen las columnas ampliadas, el favorito sigue funcionando.
        if not respuesta.ok:
            respuesta = requests.post(
                base,
                headers=_graph_headers(),
                json={"fields": campos_base},
                timeout=15,
            )
    respuesta.raise_for_status()
    cargar_favoritos_compartidos.clear()

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
    div[class*="st-key-acciones_"] [data-testid="stButton"] button,
    div[class*="st-key-acciones_"] [data-testid="stLinkButton"] a {
        min-height: 32px !important; height: 32px !important; padding: 2px 8px !important;
        border-radius: 6px !important; line-height: 1 !important;
        display: flex !important; align-items: center !important; justify-content: center !important;
    }
    div[class*="st-key-acciones_"] [data-testid="stButton"] p,
    div[class*="st-key-acciones_"] [data-testid="stLinkButton"] p {
        font-size: 1rem !important; line-height: 1 !important;
        margin: 0 !important; width: 100% !important; text-align: center !important;
    }
    div[class*="st-key-fav_activo_"] button {
        background: #198754 !important; border-color: #198754 !important;
        color: #ffd43b !important;
    }
    
    .metric-box-grid { background-color: #ffffff; border-radius: 8px; padding: 12px 10px; text-align: center; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    .metric-val-grid { font-size: 1.35rem; font-weight: 800; color: #0d6efd; letter-spacing: -0.5px; }
    .metric-lbl-grid { font-size: 0.72rem; color: #475569; text-transform: uppercase; font-weight: 700; margin-top: 4px; }
    .card-metric { height: 100px !important; display: flex !important; flex-direction: column !important; justify-content: center !important; box-sizing: border-box !important; margin-bottom: 0.65rem !important; }
    .top-kpi { height: 112px !important; display: flex !important; flex-direction: column !important; justify-content: center !important; box-sizing: border-box !important; }
    .company-card { margin-top: 0; margin-bottom: 1rem; padding: 1.4rem; border: 1px solid #dbe3ec; border-radius: 12px; background: #ffffff; box-shadow: 0 2px 8px rgba(15,23,42,0.06); }
    .company-heading { display: flex; align-items: center; gap: 0.8rem; margin-bottom: 0.55rem; }
    .company-logo { width: 52px; height: 52px; flex: 0 0 52px; display: block; border-radius: 10px; object-fit: cover; }
    .company-name { margin: 0; color: #1e293b; font-size: 1.15rem; font-weight: 800; }
    .company-copy { margin: 0.35rem 0 1rem; color: #475569; line-height: 1.5; }
    .company-actions { display: flex; flex-wrap: wrap; gap: 0.55rem; }
    .company-action { min-height: 42px; display: inline-flex; align-items: center; justify-content: center; padding: 0.6rem 0.9rem; border: 1px solid #bfd2ea; border-radius: 8px; background: #f8fbff; color: #0b5ed7 !important; text-decoration: none !important; font-weight: 700; box-sizing: border-box; }
    .company-action:hover { background: #0d6efd; border-color: #0d6efd; color: white !important; }
    .data-source { margin: -0.35rem 0 1rem; color: #64748b; font-size: 0.82rem; }
    .data-source a { color: #0b5ed7 !important; font-weight: 700; text-decoration: none !important; }
    .data-source a:hover { text-decoration: underline !important; }
    .legal-note { margin-top: 1.5rem; padding: 0.9rem 1rem; border-top: 1px solid #dbe3ec; color: #64748b; font-size: 0.78rem; line-height: 1.45; }

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
        div[data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; max-width: 100% !important; flex: 0 0 auto !important; }\n        div[data-testid="stHorizontalBlock"], .metric-box-grid, div[data-testid="stExpander"] { width: 100% !important; max-width: 100% !important; box-sizing: border-box !important; }
        h1 { font-size: 1.8rem !important; }
    }
</style>
""", unsafe_allow_html=True)

MAPA_ESTADOS = {
    'PUB': ('En plazo / Publicada', 'badge-pub'),
    'PRE': ('Preanuncio', 'badge-res'),
    'EV':  ('En Evaluación', 'badge-ev'),
    'PARCIAL': ('Parcialmente resuelta', 'badge-ev'),
    'ADJ': ('Adjudicada', 'badge-adj'),
    'RES': ('Resuelta / Formalizada', 'badge-res'),
    'ANUL': ('Anulada', 'badge-res'),
    'CERR': ('Cerrada / Archivada', 'badge-res'),
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

def calcular_baja(pbl_con_iva, adjudicacion_con_iva):
    try:
        pbl = float(pbl_con_iva)
        adjudicacion = float(adjudicacion_con_iva)
    except (TypeError, ValueError):
        return None, None
    if (
        pd.isna(pbl)
        or pd.isna(adjudicacion)
        or pbl <= 0
        or adjudicacion < 0
        or adjudicacion > pbl
    ):
        return None, None
    diferencia = pbl - adjudicacion
    return diferencia, diferencia * 100 / pbl

def formato_fecha(valor):
    fecha = pd.to_datetime(valor, errors="coerce")
    return "No disponible" if pd.isna(fecha) else fecha.strftime("%d/%m/%Y · %H:%M")

def formato_fecha_corta(valor):
    fecha = pd.to_datetime(valor, errors="coerce")
    return "No disponible" if pd.isna(fecha) else fecha.strftime("%d/%m/%Y")

def texto_dias_restantes(valor):
    fecha = pd.to_datetime(valor, errors="coerce")
    if pd.isna(fecha):
        return ""
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

def normalizar_estado_vigente(tabla):
    """Conserva el estado ATOM y calcula el estado operativo por fecha límite."""
    resultado = tabla.copy()
    if resultado.empty or not {"estado", "fecha_limite"}.issubset(resultado.columns):
        return resultado

    resultado["estado_fuente"] = resultado["estado"]
    campos_adjudicacion = [
        campo for campo in (
            "adjudicatario",
            "fecha_adjudicacion",
            "importe_adjudicacion_con_iva",
        )
        if campo in resultado.columns
    ]
    if campos_adjudicacion:
        tiene_resultado_adjudicacion = pd.Series(False, index=resultado.index)
        for campo in campos_adjudicacion:
            valores = resultado[campo]
            if pd.api.types.is_numeric_dtype(valores):
                tiene_resultado_adjudicacion |= valores.notna()
            else:
                tiene_resultado_adjudicacion |= (
                    valores.notna() & valores.astype(str).str.strip().ne("")
                )
        resultado.loc[
            resultado["estado"].eq("EV") & tiene_resultado_adjudicacion,
            "estado",
        ] = "PARCIAL"
    texto_limite = resultado["fecha_limite"].fillna("").astype(str).str.strip()
    limite_local = pd.to_datetime(texto_limite.str.slice(0, 19), errors="coerce")

    # Si la fuente sólo facilita una fecha, el plazo vence al terminar ese día.
    solo_fecha = texto_limite.str.fullmatch(r"\d{4}-\d{2}-\d{2}")
    limite_local = limite_local.where(
        ~solo_fecha,
        limite_local + pd.Timedelta(days=1) - pd.Timedelta(seconds=1),
    )
    ahora_local = pd.Timestamp.now(tz="Europe/Madrid").tz_localize(None)
    plazo_vencido = (
        resultado["estado"].eq("PUB")
        & limite_local.notna()
        & limite_local.lt(ahora_local)
    )
    resultado.loc[plazo_vencido, "estado"] = "EV"
    return resultado

FEED_RECIENTE_URLS = (
    (
        "https://contrataciondelsectorpublico.gob.es/sindicacion/"
        "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
    ),
    (
        "https://contrataciondelestado.es/sindicacion/"
        "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
    ),
)

# La portada del feed es sólo la página más reciente. Se agregan también las
# tres páginas enlazadas siguientes para no perder cambios cuando rota la portada.
FEED_MAX_PAGINAS = 4
FEED_CACHE_DIR = Path(__file__).resolve().parent / "feed_cache"

FEED_RECIENTE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://contrataciondelsectorpublico.gob.es/datosabiertos",
    "Cache-Control": "no-cache",
}

NAMESPACES_ATOM = {
    "atom": "http://www.w3.org/2005/Atom",
    "at": "http://purl.org/atompub/tombstones/1.0",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}

def _texto_xml(elemento, ruta):
    if elemento is None:
        return None
    nodo = elemento.find(ruta, NAMESPACES_ATOM)
    return nodo.text.strip() if nodo is not None and nodo.text else None

@st.cache_data(ttl=900, show_spinner=False)
def _cargar_feed_reciente_portada_legacy():
    respuesta = None
    errores = []
    for ronda in range(2):
        for url_feed in FEED_RECIENTE_URLS:
            try:
                candidata = requests.get(
                    url_feed,
                    headers=FEED_RECIENTE_HEADERS,
                    timeout=45,
                )
                candidata.raise_for_status()
                inicio = candidata.content[:4096]
                if b"Web Application Firewall" in inicio:
                    raise RuntimeError(
                        "el cortafuegos de la plataforma rechazó temporalmente la consulta"
                    )
                if b"<feed" not in inicio and b"<?xml" not in inicio:
                    tipo = candidata.headers.get("content-type", "desconocido")
                    raise RuntimeError(
                        f"la plataforma no devolvió un feed XML ({tipo})"
                    )
                respuesta = candidata
                break
            except Exception as error:
                errores.append(f"{url_feed}: {error}")
        if respuesta is not None:
            break
        if ronda == 0:
            time.sleep(2)

    if respuesta is None:
        detalle = errores[-1] if errores else "error desconocido"
        raise RuntimeError(
            "El feed oficial no está disponible temporalmente. "
            f"Último intento: {detalle}"
        )

    raiz = ET.fromstring(respuesta.content)
    if raiz.tag != f"{{{NAMESPACES_ATOM['atom']}}}feed":
        raise RuntimeError("La respuesta oficial no contiene un feed ATOM válido.")
    actualizado_feed = _texto_xml(raiz, "atom:updated")
    filas = []

    for entrada in raiz.findall("atom:entry", NAMESPACES_ATOM):
        lic_id = _texto_xml(entrada, "atom:id")
        enlace = entrada.find("atom:link", NAMESPACES_ATOM)
        status = entrada.find("cac-place-ext:ContractFolderStatus", NAMESPACES_ATOM)
        if status is None:
            continue

        party = status.find("cac-place-ext:LocatedContractingParty/cac:Party", NAMESPACES_ATOM)
        proyecto = status.find("cac:ProcurementProject", NAMESPACES_ATOM)
        if proyecto is None:
            continue

        def numero(ruta):
            valor = _texto_xml(proyecto, ruta)
            try:
                return float(valor) if valor else None
            except (TypeError, ValueError):
                return None

        codigo_postal = _texto_xml(proyecto, "cac:RealizedLocation/cac:Address/cbc:PostalZone")
        municipio = _texto_xml(proyecto, "cac:RealizedLocation/cac:Address/cbc:CityName")
        if not codigo_postal:
            codigo_postal = _texto_xml(party, "cac:PostalAddress/cbc:PostalZone")
        if not municipio:
            municipio = _texto_xml(party, "cac:PostalAddress/cbc:CityName")

        cpvs = [
            nodo.text.strip()
            for nodo in proyecto.findall(
                "cac:RequiredCommodityClassification/cbc:ItemClassificationCode",
                NAMESPACES_ATOM,
            )
            if nodo.text
        ]

        fecha_limite = _texto_xml(
            status,
            "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate",
        )
        hora_limite = _texto_xml(
            status,
            "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndTime",
        )
        if fecha_limite and hora_limite:
            fecha_limite = f"{fecha_limite} {hora_limite}"

        documentos = []
        referencias = [
            ("PPT", "cac:TechnicalDocumentReference"),
            ("PCAP", "cac:LegalDocumentReference"),
        ]
        for tipo_doc, etiqueta in referencias:
            doc = status.find(etiqueta, NAMESPACES_ATOM)
            if doc is not None:
                uri = _texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
                if uri:
                    documentos.append({
                        "tipo": tipo_doc,
                        "nombre": _texto_xml(doc, "cbc:ID") or tipo_doc,
                        "url": uri,
                    })
        for doc in status.findall("cac:AdditionalDocumentReference", NAMESPACES_ATOM):
            uri = _texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
            if uri:
                documentos.append({
                    "tipo": "ANEXO",
                    "nombre": _texto_xml(doc, "cbc:ID") or "Anexo adicional",
                    "url": uri,
                })

        filas.append({
            "id": lic_id,
            "id_licitacion_corta": lic_id.split("/")[-1] if lic_id else None,
            "expediente": _texto_xml(status, "cbc:ContractFolderID"),
            "titulo": _texto_xml(proyecto, "cbc:Name"),
            "organo_contratante": _texto_xml(party, "cac:PartyName/cbc:Name"),
            "tipo_contrato": _texto_xml(proyecto, "cbc:TypeCode"),
            "estado": (
                "PARCIAL"
                if _texto_xml(status, "cbc-place-ext:ContractFolderStatusCode") == "EV"
                and status.findall("cac:TenderResult", NAMESPACES_ATOM)
                else _texto_xml(status, "cbc-place-ext:ContractFolderStatusCode")
            ),
            "pbl_sin_iva": numero("cac:BudgetAmount/cbc:TaxExclusiveAmount"),
            "pbl_con_iva": numero("cac:BudgetAmount/cbc:TotalAmount"),
            "valor_estimado": numero("cac:BudgetAmount/cbc:EstimatedOverallContractAmount"),
            "cpv": ",".join(cpvs),
            "codigo_postal": codigo_postal,
            "municipio": municipio,
            "provincia": _texto_xml(
                proyecto, "cac:RealizedLocation/cbc:CountrySubentity"
            ),
            "comunidad_autonoma": None,
            "latitud": None,
            "longitud": None,
            "fecha_limite": fecha_limite,
            "fecha_actualizacion": _texto_xml(entrada, "atom:updated"),
            "url_licitacion": enlace.attrib.get("href") if enlace is not None else None,
            "documentos_adjuntos": json.dumps(documentos, ensure_ascii=False),
            "resumen_ia": None,
        })

    radar = pd.DataFrame(filas)
    if not radar.empty:
        radar = radar.drop_duplicates(subset=["id"], keep="first")
        radar["fecha_limite_dt"] = pd.to_datetime(
            radar["fecha_limite"].astype(str).str.slice(0, 10),
            errors="coerce",
            utc=True,
        )
        radar["fecha_act_dt"] = pd.to_datetime(
            radar["fecha_actualizacion"], errors="coerce", utc=True
        )
        radar["tipo_contrato_desc"] = (
            radar["tipo_contrato"].map(MAPA_TIPOS).fillna("Otros")
        )
    return radar, actualizado_feed

def _variantes_url_feed(url_feed):
    variantes = [url_feed]
    dominios = (
        "contrataciondelsectorpublico.gob.es",
        "contrataciondelestado.es",
    )
    for origen in dominios:
        if origen in url_feed:
            for destino in dominios:
                candidata = url_feed.replace(origen, destino)
                if candidata not in variantes:
                    variantes.append(candidata)
    return variantes

def _descargar_pagina_feed(url_feed):
    errores = []
    for ronda in range(2):
        for candidata_url in _variantes_url_feed(url_feed):
            try:
                respuesta = requests.get(
                    candidata_url,
                    headers=FEED_RECIENTE_HEADERS,
                    timeout=45,
                )
                respuesta.raise_for_status()
                inicio = respuesta.content[:4096]
                if b"Web Application Firewall" in inicio:
                    raise RuntimeError(
                        "el cortafuegos de la plataforma rechazó temporalmente la consulta"
                    )
                if b"<feed" not in inicio and b"<?xml" not in inicio:
                    tipo = respuesta.headers.get("content-type", "desconocido")
                    raise RuntimeError(
                        f"la plataforma no devolvió un feed XML ({tipo})"
                    )
                raiz = ET.fromstring(respuesta.content)
                if raiz.tag != f"{{{NAMESPACES_ATOM['atom']}}}feed":
                    raise RuntimeError("la respuesta no contiene un feed ATOM válido")
                return raiz, candidata_url
            except Exception as error:
                errores.append(f"{candidata_url}: {error}")
        if ronda == 0:
            time.sleep(2)
    detalle = errores[-1] if errores else "error desconocido"
    raise RuntimeError(
        "El feed oficial no está disponible temporalmente. "
        f"Último intento: {detalle}"
    )

def _fila_desde_entrada_feed(entrada):
    lic_id = _texto_xml(entrada, "atom:id")
    enlace = entrada.find("atom:link", NAMESPACES_ATOM)
    status = entrada.find("cac-place-ext:ContractFolderStatus", NAMESPACES_ATOM)
    if status is None:
        return None

    party = status.find(
        "cac-place-ext:LocatedContractingParty/cac:Party", NAMESPACES_ATOM
    )
    proyecto = status.find("cac:ProcurementProject", NAMESPACES_ATOM)
    if proyecto is None:
        return None
    adjudicatario, fecha_adjudicacion, importe_adjudicacion_con_iva = extraer_adjudicacion(status)

    def numero(ruta):
        valor = _texto_xml(proyecto, ruta)
        try:
            return float(valor) if valor else None
        except (TypeError, ValueError):
            return None

    codigo_postal = _texto_xml(
        proyecto, "cac:RealizedLocation/cac:Address/cbc:PostalZone"
    )
    municipio = _texto_xml(
        proyecto, "cac:RealizedLocation/cac:Address/cbc:CityName"
    )
    if not codigo_postal:
        codigo_postal = _texto_xml(party, "cac:PostalAddress/cbc:PostalZone")
    if not municipio:
        municipio = _texto_xml(party, "cac:PostalAddress/cbc:CityName")

    cpvs = [
        nodo.text.strip()
        for nodo in proyecto.findall(
            "cac:RequiredCommodityClassification/cbc:ItemClassificationCode",
            NAMESPACES_ATOM,
        )
        if nodo.text
    ]
    fecha_limite = _texto_xml(
        status,
        "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate",
    )
    hora_limite = _texto_xml(
        status,
        "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndTime",
    )
    if fecha_limite and hora_limite:
        fecha_limite = f"{fecha_limite} {hora_limite}"

    documentos = []
    referencias = (
        ("PPT", "cac:TechnicalDocumentReference"),
        ("PCAP", "cac:LegalDocumentReference"),
    )
    for tipo_doc, etiqueta in referencias:
        doc = status.find(etiqueta, NAMESPACES_ATOM)
        if doc is not None:
            uri = _texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
            if uri:
                documentos.append({
                    "tipo": tipo_doc,
                    "nombre": _texto_xml(doc, "cbc:ID") or tipo_doc,
                    "url": uri,
                })
    for doc in status.findall("cac:AdditionalDocumentReference", NAMESPACES_ATOM):
        uri = _texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
        if uri:
            documentos.append({
                "tipo": "ANEXO",
                "nombre": _texto_xml(doc, "cbc:ID") or "Anexo adicional",
                "url": uri,
            })

    return {
        "id": lic_id,
        "id_licitacion_corta": lic_id.split("/")[-1] if lic_id else None,
        "expediente": _texto_xml(status, "cbc:ContractFolderID"),
        "titulo": _texto_xml(proyecto, "cbc:Name"),
        "organo_contratante": _texto_xml(party, "cac:PartyName/cbc:Name"),
        "tipo_contrato": _texto_xml(proyecto, "cbc:TypeCode"),
        "estado": (
            "PARCIAL"
            if _texto_xml(status, "cbc-place-ext:ContractFolderStatusCode") == "EV"
            and status.findall("cac:TenderResult", NAMESPACES_ATOM)
            else _texto_xml(status, "cbc-place-ext:ContractFolderStatusCode")
        ),
        "pbl_sin_iva": numero("cac:BudgetAmount/cbc:TaxExclusiveAmount"),
        "pbl_con_iva": numero("cac:BudgetAmount/cbc:TotalAmount"),
        "valor_estimado": numero("cac:BudgetAmount/cbc:EstimatedOverallContractAmount"),
        "cpv": ",".join(cpvs),
        "codigo_postal": codigo_postal,
        "municipio": municipio,
        "provincia": _texto_xml(
            proyecto, "cac:RealizedLocation/cbc:CountrySubentity"
        ),
        "comunidad_autonoma": None,
        "latitud": None,
        "longitud": None,
        "fecha_limite": fecha_limite,
        "fecha_actualizacion": _texto_xml(entrada, "atom:updated"),
        "adjudicatario": adjudicatario,
        "fecha_adjudicacion": fecha_adjudicacion,
        "importe_adjudicacion_con_iva": importe_adjudicacion_con_iva,
        "url_licitacion": enlace.attrib.get("href") if enlace is not None else None,
        "documentos_adjuntos": json.dumps(documentos, ensure_ascii=False),
        "resumen_ia": None,
    }

def _bajas_desde_pagina_feed(raiz):
    bajas = []
    for eliminada in raiz.findall("at:deleted-entry", NAMESPACES_ATOM):
        lic_id = eliminada.attrib.get("ref")
        fecha = eliminada.attrib.get("when")
        comentario = " ".join(
            texto.strip()
            for texto in eliminada.itertext()
            if texto and texto.strip()
        )
        tipo = eliminada.attrib.get("type", "")
        detalle = f"{tipo} {comentario}".upper()
        estado = "ANUL" if "ANUL" in detalle else "CERR"
        bajas.append({
            "id": lic_id,
            "fecha_actualizacion": fecha,
            "estado": estado,
        })
    return bajas

def _cargar_paginas_feed_guardadas():
    manifiesto_path = FEED_CACHE_DIR / "manifest.json"
    if not manifiesto_path.is_file():
        return None
    manifiesto = json.loads(manifiesto_path.read_text(encoding="utf-8"))
    if manifiesto.get("version") != 1:
        raise RuntimeError("La versión del feed guardado no es compatible.")
    paginas = manifiesto.get("paginas")
    if not isinstance(paginas, list) or not paginas:
        raise RuntimeError("El feed guardado no contiene páginas.")
    snapshot = json.loads((FEED_CACHE_DIR / "feed.json").read_text(encoding="utf-8"))
    filas = snapshot.get("filas")
    bajas = snapshot.get("bajas")
    if not isinstance(filas, list) or not isinstance(bajas, list):
        raise RuntimeError("El contenido del feed guardado no es válido.")
    return filas, bajas, manifiesto


@st.cache_data(ttl=300, show_spinner=False)
def cargar_feed_reciente():
    filas = []
    bajas = []
    paginas_guardadas = _cargar_paginas_feed_guardadas()

    if paginas_guardadas is not None:
        filas, bajas, manifiesto_feed = paginas_guardadas
        actualizado_feed = manifiesto_feed.get("fecha_feed")
        paginas_leidas = len(manifiesto_feed["paginas"])
        parcial = not bool(manifiesto_feed.get("completo"))
        siguiente = None
    else:
        siguiente = FEED_RECIENTE_URLS[0]
        actualizado_feed = None
        paginas_leidas = 0
        parcial = False

    visitadas = set()

    while siguiente and paginas_leidas < FEED_MAX_PAGINAS:
        if siguiente in visitadas:
            break
        visitadas.add(siguiente)
        try:
            raiz, url_real = _descargar_pagina_feed(siguiente)
        except Exception:
            if paginas_leidas == 0:
                raise
            parcial = True
            break

        paginas_leidas += 1
        if actualizado_feed is None:
            actualizado_feed = _texto_xml(raiz, "atom:updated")

        for entrada in raiz.findall("atom:entry", NAMESPACES_ATOM):
            fila = _fila_desde_entrada_feed(entrada)
            if fila:
                filas.append(fila)
        bajas.extend(_bajas_desde_pagina_feed(raiz))

        enlace_siguiente = raiz.find("atom:link[@rel='next']", NAMESPACES_ATOM)
        href = enlace_siguiente.attrib.get("href") if enlace_siguiente is not None else None
        siguiente = urljoin(url_real, href) if href else None

    radar = pd.DataFrame(filas)
    if not radar.empty:
        radar["fecha_limite_dt"] = pd.to_datetime(
            radar["fecha_limite"].astype(str).str.slice(0, 10),
            errors="coerce",
            utc=True,
        )
        radar["fecha_act_dt"] = pd.to_datetime(
            radar["fecha_actualizacion"], errors="coerce", utc=True
        )
        radar = (
            radar.sort_values("fecha_act_dt", ascending=False, na_position="last")
            .drop_duplicates(subset=["id"], keep="first")
        )
        radar["tipo_contrato_desc"] = (
            radar["tipo_contrato"].map(MAPA_TIPOS).fillna("Otros")
        )
        radar = normalizar_estado_vigente(radar)

    bajas_df = pd.DataFrame(bajas)
    if not bajas_df.empty:
        bajas_df["fecha_act_dt"] = pd.to_datetime(
            bajas_df["fecha_actualizacion"], errors="coerce", utc=True
        )
        bajas_df = (
            bajas_df.sort_values("fecha_act_dt", ascending=False, na_position="last")
            .drop_duplicates(subset=["id"], keep="first")
        )

    metadata = {
        "paginas": paginas_leidas,
        "bajas": len(bajas_df),
        "parcial": parcial,
    }
    return radar, actualizado_feed, bajas_df, metadata

def _clave_ubicacion(valor):
    if valor is None or pd.isna(valor):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor))
    return "".join(
        caracter for caracter in texto if not unicodedata.combining(caracter)
    ).strip().lower()

def _codigo_postal_normalizado(valor):
    if valor is None or pd.isna(valor):
        return ""
    texto = str(valor).strip().split(".")[0]
    digitos = "".join(caracter for caracter in texto if caracter.isdigit())
    return digitos.zfill(5) if digitos else ""

@st.cache_data(ttl=3600)
def cargar_maestro_ubicaciones():
    maestro = pd.read_excel(
        "provincias.xlsx",
        sheet_name="municipios datos",
        dtype={"codigo_postal": str},
    )
    maestro["cp_clave"] = maestro["codigo_postal"].apply(
        _codigo_postal_normalizado
    )
    maestro["municipio_clave"] = maestro["nucleo_nombre"].apply(
        _clave_ubicacion
    )
    maestro = maestro.dropna(subset=["Latitud", "Longitud"])
    por_cp = (
        maestro.drop_duplicates(subset=["cp_clave"])
        .set_index("cp_clave")
    )
    municipios_unicos = maestro[
        ~maestro["municipio_clave"].duplicated(keep=False)
    ]
    por_municipio = municipios_unicos.set_index("municipio_clave")
    return por_cp, por_municipio

def completar_ubicaciones_feed(df_feed):
    if df_feed.empty:
        return df_feed
    enriquecido = df_feed.copy()
    enriquecido["origen_coordenadas"] = None
    try:
        por_cp, por_municipio = cargar_maestro_ubicaciones()
    except Exception:
        return enriquecido

    correspondencias = {
        "municipio": "nucleo_nombre",
        "provincia": "PROVINCIA",
        "comunidad_autonoma": "COMUNIDAD",
        "latitud": "Latitud",
        "longitud": "Longitud",
    }
    for indice, fila in enriquecido.iterrows():
        cp = _codigo_postal_normalizado(fila.get("codigo_postal"))
        referencia = None
        origen = None
        if cp and cp in por_cp.index:
            referencia = por_cp.loc[cp]
            origen = "Código postal"
        else:
            municipio = _clave_ubicacion(fila.get("municipio"))
            if municipio and municipio in por_municipio.index:
                referencia = por_municipio.loc[municipio]
                origen = "Municipio"

        if referencia is not None:
            for destino, fuente in correspondencias.items():
                enriquecido.at[indice, destino] = referencia[fuente]
            enriquecido.at[indice, "origen_coordenadas"] = origen
    return enriquecido

def incorporar_bajas_feed(df_feed, bajas_feed, historico):
    if bajas_feed.empty:
        return df_feed

    fuentes = pd.concat([df_feed, historico], ignore_index=True, sort=False)
    fuentes = fuentes.dropna(subset=["id"]).drop_duplicates(subset=["id"], keep="first")
    por_id = fuentes.set_index(fuentes["id"].astype(str), drop=False)
    filas = []

    for _, baja in bajas_feed.iterrows():
        lic_id = str(baja.get("id") or "").strip()
        if not lic_id:
            continue
        if lic_id in por_id.index:
            fila = por_id.loc[lic_id].copy()
            if isinstance(fila, pd.DataFrame):
                fila = fila.iloc[0].copy()
        else:
            fila = pd.Series({columna: None for columna in fuentes.columns})
            fila["id"] = lic_id
            fila["id_licitacion_corta"] = lic_id.split("/")[-1]
            fila["titulo"] = "Expediente retirado de la plataforma"
            fila["documentos_adjuntos"] = "[]"
            fila["tipo_contrato_desc"] = "Otros"

        fila["estado_fuente"] = baja["estado"]
        fila["estado"] = baja["estado"]
        fila["fecha_actualizacion"] = baja["fecha_actualizacion"]
        fila["fecha_act_dt"] = baja["fecha_act_dt"]
        fila["movimiento"] = "Actualizada"
        filas.append(fila.to_dict())

    if not filas:
        return df_feed
    resultado = pd.concat([df_feed, pd.DataFrame(filas)], ignore_index=True, sort=False)
    return (
        resultado.sort_values("fecha_act_dt", ascending=False, na_position="last")
        .drop_duplicates(subset=["id"], keep="first")
    )

@st.cache_data(ttl=300, show_spinner="Cargando licitaciones…")
def cargar_datos(db_mtime):
    del db_mtime
    uri = f"file:{os.path.abspath(DB_PATH).replace('\\', '/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        df = pd.read_sql_query("SELECT * FROM licitaciones", conn)

    df['fecha_limite_dt'] = pd.to_datetime(df['fecha_limite'].astype(str).str.slice(0, 10), errors='coerce', utc=True)
    df['fecha_act_dt'] = pd.to_datetime(df['fecha_actualizacion'], errors='coerce', utc=True)
    df['tipo_contrato_desc'] = df['tipo_contrato'].map(MAPA_TIPOS).fillna('Otros')
    return normalizar_estado_vigente(df)

try:
    df = cargar_datos(os.path.getmtime(DB_PATH))
except Exception as e:
    st.error(f"❌ No se pudo conectar a la base de datos en {DB_PATH}. Error: {e}")
    st.stop()

try:
    (
        df_radar_catalogo,
        fecha_feed_catalogo,
        df_bajas_catalogo,
        metadata_feed_catalogo,
    ) = cargar_feed_reciente()
    df_radar_catalogo = completar_ubicaciones_feed(df_radar_catalogo)
    df_radar_catalogo = incorporar_bajas_feed(
        df_radar_catalogo, df_bajas_catalogo, df
    )
    error_feed_catalogo = None
except Exception as error_feed_inicial:
    df_radar_catalogo = pd.DataFrame(columns=df.columns)
    fecha_feed_catalogo = None
    metadata_feed_catalogo = {"paginas": 0, "bajas": 0, "parcial": False}
    error_feed_catalogo = str(error_feed_inicial)

df_catalogo_filtros = pd.concat(
    [df, df_radar_catalogo], ignore_index=True, sort=False
)
max_pbl_value = df_catalogo_filtros['pbl_sin_iva'].max()
max_pbl_db = float(max_pbl_value) if pd.notnull(max_pbl_value) and max_pbl_value > 0 else 200000.0

fechas_validas = df_catalogo_filtros['fecha_limite_dt'].dropna()
f_min_db = fechas_validas.min().date() if not fechas_validas.empty else date.today()
f_max_db = fechas_validas.max().date() if not fechas_validas.empty else date.today()
hoy = date.today()
f_inicio_default = hoy if hoy <= f_max_db else f_min_db

if "f_cpv" not in st.session_state:
    st.session_state["f_cpv"] = "71"
if "f_estado" not in st.session_state:
    st.session_state["f_estado"] = ["PUB"]
if "f_ccaa" not in st.session_state:
    st.session_state["f_ccaa"] = ["Comunitat Valenciana"]
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
    st.session_state["f_adjudicatario"] = []
    st.rerun()

if st.sidebar.button("↩️ Filtros iniciales", use_container_width=True):
    st.session_state["f_texto"] = ""
    st.session_state["f_tipo"] = []
    st.session_state["f_cpv"] = "71"
    st.session_state["f_estado"] = ["PUB"]
    st.session_state["f_ccaa"] = ["Comunitat Valenciana"]
    st.session_state["f_prov"] = []
    st.session_state["f_muni"] = []
    st.session_state["f_pbl_min"] = 0.0
    st.session_state["f_pbl_max"] = 200000.0
    st.session_state["f_fecha"] = (f_inicio_default, f_max_db)
    st.session_state["f_organo"] = []
    st.session_state["f_adjudicatario"] = []
    st.rerun()

st.sidebar.divider()

busqueda_texto = st.sidebar.text_input("🔍 Palabras clave (título, expediente...):", key="f_texto")
tipos_list = sorted([x for x in df_catalogo_filtros['tipo_contrato_desc'].dropna().unique() if x])
tipo_sel = st.sidebar.multiselect("📦 Tipo de Contrato:", tipos_list, key="f_tipo")
cpv_2dig = st.sidebar.text_input("🏷️ CPV (2 dígitos):", max_chars=2, key="f_cpv")

estados_unicos = df_catalogo_filtros['estado'].dropna().unique().tolist()
opciones_estado = {c: MAPA_ESTADOS.get(c, (c, ''))[0] for c in estados_unicos}
estados_sel = st.sidebar.multiselect("📌 Estado:", list(opciones_estado.keys()), format_func=lambda x: opciones_estado[x], key="f_estado")

ccaa_list = sorted([x for x in df_catalogo_filtros['comunidad_autonoma'].dropna().unique() if x])
ccaa_sel = st.sidebar.multiselect("🗺️ Comunidad Autónoma:", ccaa_list, key="f_ccaa")

prov_list = sorted([x for x in df_catalogo_filtros[df_catalogo_filtros['comunidad_autonoma'].isin(ccaa_sel)]['provincia'].dropna().unique() if x]) if ccaa_sel else sorted([x for x in df_catalogo_filtros['provincia'].dropna().unique() if x])
prov_sel = st.sidebar.multiselect("📍 Provincia:", prov_list, key="f_prov")

muni_list = sorted([x for x in df_catalogo_filtros[df_catalogo_filtros['provincia'].isin(prov_sel)]['municipio'].dropna().unique() if x]) if prov_sel else sorted([x for x in df_catalogo_filtros['municipio'].dropna().unique() if x])
muni_sel = st.sidebar.multiselect("🏙️ Municipio:", muni_list, key="f_muni")

st.sidebar.markdown("💶 **Presupuesto Base sin IVA (€):**")
pbl_min_val = st.sidebar.number_input("Mínimo €", min_value=0.0, step=10000.0, key="f_pbl_min")
pbl_max_val = st.sidebar.number_input("Máximo €", min_value=0.0, value=200000.0, step=50000.0, key="f_pbl_max")

if not fechas_validas.empty:
    fecha_rango = st.sidebar.date_input("📅 Fecha Límite Presentación:", min_value=f_min_db, max_value=f_max_db, key="f_fecha")
else:
    fecha_rango = None
organos = sorted([x for x in df_catalogo_filtros['organo_contratante'].dropna().unique() if x])
organo_sel = st.sidebar.multiselect("🏛️ Órgano de Contratación:", organos, key="f_organo")

adjudicatarios = sorted([
    x for x in df_catalogo_filtros['adjudicatario'].dropna().unique()
    if str(x).strip()
])
adjudicatario_sel = st.sidebar.multiselect(
    "🏆 Adjudicatario:", adjudicatarios, key="f_adjudicatario"
)

df_f = df.copy()

if busqueda_texto.strip():
    q = busqueda_texto.lower().strip()
    df_f = df_f[df_f['titulo'].str.lower().str.contains(q, na=False, regex=False) | df_f['expediente'].str.lower().str.contains(q, na=False, regex=False) | df_f['organo_contratante'].str.lower().str.contains(q, na=False, regex=False)]

if estados_sel: df_f = df_f[df_f['estado'].isin(estados_sel)]
if tipo_sel: df_f = df_f[df_f['tipo_contrato_desc'].isin(tipo_sel)]
df_f = df_f[(df_f['pbl_sin_iva'] >= pbl_min_val) & (df_f['pbl_sin_iva'] <= pbl_max_val)]

if fecha_rango and len(fecha_rango) == 2:
    dentro_del_rango = (
        (df_f['fecha_limite_dt'].dt.date >= fecha_rango[0])
        & (df_f['fecha_limite_dt'].dt.date <= fecha_rango[1])
    )
    dentro_del_rango = dentro_del_rango | df_f['fecha_limite_dt'].isna()
    df_f = df_f[dentro_del_rango]

if ccaa_sel: df_f = df_f[df_f['comunidad_autonoma'].isin(ccaa_sel)]
if prov_sel: df_f = df_f[df_f['provincia'].isin(prov_sel)]
if muni_sel: df_f = df_f[df_f['municipio'].isin(muni_sel)]
if organo_sel: df_f = df_f[df_f['organo_contratante'].isin(organo_sel)]
if adjudicatario_sel: df_f = df_f[df_f['adjudicatario'].isin(adjudicatario_sel)]

if cpv_2dig.strip():
    prefijo = cpv_2dig.strip()
    df_f = df_f[df_f['cpv'].apply(lambda x: any(c.strip().startswith(prefijo) for c in str(x).split(',')) if x else False)]

def aplicar_filtros_al_feed(df_entrada):
    filtrado = df_entrada.copy()
    if filtrado.empty:
        return filtrado
    historico_ids = set(df["id"].dropna().astype(str))
    filtrado["movimiento"] = filtrado.apply(
        lambda fila: (
            "Actualizada"
            if fila.get("estado") in {"ANUL", "CERR"}
            or str(fila.get("id")) in historico_ids
            else "Nueva licitación"
        ),
        axis=1,
    )
    if busqueda_texto.strip():
        consulta = busqueda_texto.lower().strip()
        filtrado = filtrado[
            filtrado["titulo"].str.lower().str.contains(
                consulta, na=False, regex=False
            )
            | filtrado["expediente"].str.lower().str.contains(
                consulta, na=False, regex=False
            )
            | filtrado["organo_contratante"].str.lower().str.contains(
                consulta, na=False, regex=False
            )
        ]
    if estados_sel:
        filtrado = filtrado[filtrado["estado"].isin(estados_sel)]
    if tipo_sel:
        filtrado = filtrado[filtrado["tipo_contrato_desc"].isin(tipo_sel)]
    filtrado = filtrado[
        (filtrado["pbl_sin_iva"].fillna(0) >= pbl_min_val)
        & (filtrado["pbl_sin_iva"].fillna(0) <= pbl_max_val)
    ]
    if fecha_rango and len(fecha_rango) == 2:
        dentro_del_rango = (
            (filtrado["fecha_limite_dt"].dt.date >= fecha_rango[0])
            & (filtrado["fecha_limite_dt"].dt.date <= fecha_rango[1])
        )
        dentro_del_rango = dentro_del_rango | filtrado["fecha_limite_dt"].isna()
        filtrado = filtrado[dentro_del_rango]
    if ccaa_sel:
        filtrado = filtrado[
            filtrado["comunidad_autonoma"].isin(ccaa_sel)
        ]
    if prov_sel:
        filtrado = filtrado[filtrado["provincia"].isin(prov_sel)]
    if muni_sel:
        filtrado = filtrado[filtrado["municipio"].isin(muni_sel)]
    if organo_sel:
        filtrado = filtrado[
            filtrado["organo_contratante"].isin(organo_sel)
        ]
    if adjudicatario_sel:
        filtrado = filtrado[
            filtrado["adjudicatario"].isin(adjudicatario_sel)
        ]
    if cpv_2dig.strip():
        prefijo_feed = cpv_2dig.strip()
        filtrado = filtrado[
            filtrado["cpv"].apply(
                lambda valor: any(
                    codigo.strip().startswith(prefijo_feed)
                    for codigo in str(valor).split(",")
                ) if valor else False
            )
        ]
    if filtrado.empty or "fecha_act_dt" not in filtrado.columns:
        return filtrado
    return filtrado.sort_values(
        "fecha_act_dt", ascending=False, na_position="last"
    )

df_radar_filtrado = aplicar_filtros_al_feed(df_radar_catalogo)
df_combinado = pd.concat(
    [df_f, df_radar_filtrado], ignore_index=True, sort=False
)
if "id" in df_combinado.columns:
    df_combinado = df_combinado.drop_duplicates(subset=["id"], keep="last")

st.title("🏛️ LandAI Licitaciones")
st.caption("Dashboard de oportunidades y análisis para proyectos de ingeniería civil.")

if ES_PREMIUM:
    acceso_col, lista_col, salir_col = st.columns([3, 2, 1])
    with acceso_col:
        st.success(f"✨ Acceso Premium activo · {CORREO_USUARIO}")
    with lista_col:
        if PREMIUM_LIST_URL:
            st.link_button(
                "📋 Abrir seguimiento",
                PREMIUM_LIST_URL,
                use_container_width=True,
            )
    with salir_col:
        st.button("Cerrar sesión", on_click=st.logout, use_container_width=True)
elif USUARIO_CONECTADO:
    st.error("Esta cuenta de Microsoft no tiene acceso Premium.")
    st.button("Cambiar de cuenta", on_click=st.logout)
else:
    try:
        auth_configurado = bool(st.secrets.get("auth", {}))
    except Exception:
        auth_configurado = False
    if auth_configurado:
        st.button(
            "🔐 Acceso Premium",
            on_click=st.login,
            args=("microsoft",),
            type="primary",
        )
    else:
        st.button(
            "🔐 Acceso Premium",
            disabled=True,
            help="Pendiente de configurar el inicio de sesión con Microsoft.",
        )

if not ES_PREMIUM:
    st.markdown("""
<div class="company-card">
    <div class="company-heading">
        <img class="company-logo" src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/4gHYSUNDX1BST0ZJTEUAAQEAAAHIAAAAAAQwAABtbnRyUkdCIFhZWiAH4AABAAEAAAAAAABhY3NwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAA9tYAAQAAAADTLQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAlkZXNjAAAA8AAAACRyWFlaAAABFAAAABRnWFlaAAABKAAAABRiWFlaAAABPAAAABR3dHB0AAABUAAAABRyVFJDAAABZAAAAChnVFJDAAABZAAAAChiVFJDAAABZAAAAChjcHJ0AAABjAAAADxtbHVjAAAAAAAAAAEAAAAMZW5VUwAAAAgAAAAcAHMAUgBHAEJYWVogAAAAAAAAb6IAADj1AAADkFhZWiAAAAAAAABimQAAt4UAABjaWFlaIAAAAAAAACSgAAAPhAAAts9YWVogAAAAAAAA9tYAAQAAAADTLXBhcmEAAAAAAAQAAAACZmYAAPKnAAANWQAAE9AAAApbAAAAAAAAAABtbHVjAAAAAAAAAAEAAAAMZW5VUwAAACAAAAAcAEcAbwBvAGcAbABlACAASQBuAGMALgAgADIAMAAxADb/2wBDAAkGBwgHBgkIBwgKCgkLDRYPDQwMDRsUFRAWIB0iIiAdHx8kKDQsJCYxJx8fLT0tMTU3Ojo6Iys/RD84QzQ5Ojf/2wBDAQoKCg0MDRoPDxo3JR8lNzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzf/wAARCAUABQADASIAAhEBAxEB/8QAHAABAQACAwEBAAAAAAAAAAAAAAMHCAQFBgEC/8QAMhABAAIBAwMBBgYCAwEBAQAAAAECAwQFERMyYQcGEhUhMXEUIkFTVJJRkTNCgSOhsf/EABYBAQEBAAAAAAAAAAAAAAAAAAABAv/EABURAQEAAAAAAAAAAAAAAAAAAAAB/9oADAMBAAIRAxEAPwDwIA0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAO39mtmyb1uGPBSPyzPEy4e66O2g3DPprRMdO81ZS9Hdo9zFn1WavzmYmkun9Wdj/Ca6mqw1/LeJteY/wAiMdACgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAvodPbV6vFgp3XniEHsfTTaJ3HeqZvd5rgtFpEZl9k9BXb9k0uL3eLxTizh+3m0xumxZ6Urzl4/LL0laxWsRWOIh8vWL1msxzEiNVtTinBqMmK31paYlN6r1D2edq3q3FeIy83eVFABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZv9Jtp/CbZOrmv/PSPmw/seknW7ppsERzF7xEtktk0UbdtuHS1jjpxwJXOAEeA9VtljWbVbW0rzlxxFYYQtE1tMT9YnhtJuWlprNHkw5I5rMS1v9pduvtu65sV4mObTMfbkWOqAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH6x0nJetK/WZ4gGQPSXZ/xe45NRlr+XHEWrLNrx/pptcaHYMGW9eMt68WewGQABiX1f2Ti8bljrxWIis8MtOn9qtsruu0ZcFo54ibf6gGtAvr9PbTavLivHExaf/wCoDQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAO59k9vvuO8YKUjmK3ibfZ0zKno/s/wD9b6/JXml68R9xGVdJgpptPTFjjitYWAQAAfL1i1ZrP0mOH0Bgn1S2adDu9tRjrxhtEf7eGZ/9Sdm+KbLPuV/PSfemfEMA5KzS9qz+k8Cx8AFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFNNjnNqMeOP8AtaIbF+w+2/C9gwYJji0fP/bC/p/tPxXe6UmvMU4v/psNir7mOtY/SIgSv0AIAAAAlqsUZtPlxT/3rNf9w109ttpnad8zYK14p9Yn7tkGNPVzZOto663DXnJ735uP8BGGwBoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACI5niBydu099VrcWPHHMzeOf9gy76Q7R0dB+PvXi9pmv/jJLrfZ7QV2/a8OGkcR7sTP+nZDIAAAAAA4G+aOut2zPhtWLTNJ937ueTHMcSDV7etBfbdxy6XJz71J/VwWSPVzZZ0+tjX0r8s1uGNxQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHufSvaZ1u9VzZK84qxPz8vD1j3rRH+Z4Z39Ldo+H7L7+SvGS1uYnxIle2rEVrFY+kRw+gIAAAAAAAA877cbVXc9lzc15tjpM1+7XbPhvgy2x5I4tX6w2oy465cdqW+lo4lgD1H2i23b5myxXjHlv+UWPJACgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADsvZ3b7bluuHT1jmZnlspt2CNPocGOsce7jrE/6Yj9Idn62s+I2r8sczVmUSgAgAAAAAAAA8H6qbJ8Q2uNTWvz08TaeHvHG3HS11miy6e8cxkrwDVmYmJ4mOJHb+1W3227etThmsxSL8VdQNAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/VKze8VrHMzL8vQew+2zue/6fDNeccz85Bmn2B2qu17JSIjicsReXpktLijBpseKPpSsQqMgAAAAAAAAAAAMUer+y8xh1mCnEViZySxO2X9qtujc9l1Gn93m1q8Q1x3LTW0euzae0cTS3AscYAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGXPR/Z/dxZdZlrxatomsz/hirRae2r1NMFI5taeIbIeymgpoNl01K14tNI977iV3AAgAAAAAAAAAAAAwX6p7LOg3SM+On5cvNrSzo8j6kbRG47FmyUrzmpXioNfh+suOcWW2O3yms8S/I0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPX+mu1Tr99w5przTFf8zP2OkUpFa/SPo8B6T7P+C26+pvX554i0TLIIyAAAAAAAAAAAAAAJ58Vc2K1LxExMfSVAGuPtvtN9q3rLW0TEZbTeHnmZ/VvZfxGhncaV/NjiK/JhiY4niRQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHO2PSTrt102m4mYyX4cFkD0n2f8buV9Tev/BMWiZEZh2TSRodr0+miOOnThzgEAAAAAAAAAAAAAAAAcLedFTcNvy4Mkc1mOWtm96LJodxz4skcfntx9uW0ExzHDDXq9ss4tbGuxV4x+7ET8v1FjGoAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA+0pN7xWsczP0Z+9NdqjQbFhzTXi+Wn5mGvZLb7a/etNSI5rW8e99mx+k09NLp6YcccVrHECVYAQAAAAAAAAAAAAAAAAee9t9pjdtky4eOZj83+noX5yV9/Hak/9omAaranFbDnyY7Rx7tpj/wDU3r/UnZ52ze7zjpxitHPMfTmXkBQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAV0uKc+px4o+t7RAMm+j+z+9my6zLXmtqxNZll1572H2yNs2DT4bV4vEfOXoRkAAAAAAAAAAAAAAAAAAAB4b1S2X4htHVxV/wDpW3MzH+IYKtHu2mP8Tw2m12Cup0mXFasT71JiP9NcPazbJ2res2mmOOPn/sWOnAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6f0/2qdz3ukccximLvMMx+kG01xaT4jPHOSJqIyVSsUrFaxxEP0AgAAAAAAAAAAAAAAAAAAAAxV6v7JHSruGKnN724t8v0ZVdV7S7fTcNpz4715mKTNfuDWUcncdJk0Wrvgyxxas/OHGGgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZV9IN6/Pbb8luK0rzHPlip2/stuF9u3fBkpbiLXiLfYRswOPodVTWaWmfFPNbR8nIEAAAAAAAAAAAAAAAAAAAAHy0RaJifpL6Awd6rbNOj3W+tpXima3EcPAthfULZ67psuS81ibYazaGvd6Wpaa2iYmP0kWPgAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPtbTW0Wr9Y+j4Azr6WbzGt2bHpLW5yYq82e6a/8ApvvE7ZvNcfvcRnmKs/1tFo5rPMSMvoAAAAAAAAAAAAAAAAAAAAAJ6jFXPgvivHNbxxLXn292qdt33UcV4xWv+VsUx16tbJ+L0NNXir88UTa0gwqANAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC2jz202px56/WluYbFexW5xuexabLNucnuc2hreyf6Q710s2XR5rfK3FaRIlZgAEAAAAAAAAAAAAAAAAAAAAHC3nR11+259NaOepXhzQGsO/wChnb921OnmOIpfiHXMm+ruydDUYtXgp8r82vPDGQoAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADs/ZzXW2/d9NqItMVpfmXWEfL6A2k2nV11234NTWYmMleXLY99J97/G7fbS3t88MRWOWQhkAAAAAAAAAAAAAAAAAAAAAB0Htptdd02PUY4rzk93irXXWYJ0uqy4LfWluJbUWiLRxMcwwH6l7NO2bxOSKzxnmbix40AUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAeq9PN2nbt9wUm3GLJf8zYLDkrmxVyU7bRzDVbDlvhyRkxzxaPpLYn2F3Wm57LhiJ5tipFbfcSvRgCAAAAAAAAAAAAAAAAAAAADxHqjs0a7ZcmqrXnJirxV7dHWaemq098OWOa2j5wDVe9Zpaa2+sTxL47r2t22+27xmx2jiL2m1fs6UaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABkf0j3r8NrZ0FrcRmtyxw52y6++27ji1WPn3qT+gjaGJ5jmBwNj1ldbtmDNW0WmaR733c8QAAAAAAAAAAAAAAAAAAAAABiz1e2SL443Glf+OvuyxG2c9o9upum05tNeImJjn5+Gtu5aa+l12bFes1928xH25FjigCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADM/pHvXX0N9Jmtzk978sT/hkhrn7CbrO179hy2txj+cTH6NiNPkjLgx5I+lqxIzVAAAAAAAAAAAAAAAAAAAAAAfJjmJif1YS9V9k/B7nGqw14xTWOeP8AMs3PL+oGz/FtkyUpX89fzcx/iAa8D9ZqdPLek/8AW0w/I0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP1ivNMlbRPExPLYH073iN12Ss2tzak+7/pr49/6T7x+E3WNJktxitEz/6JWcB8rMWrEx9Jjl9EAAAAAAAAAAAAAAAAAAAAH4zY4y4b0mOferMP2A129v8AaPhO+ZMda8UtHvc/d5lmv1Z2X8Vt0avDXnLFo5+zCkxxMxP6CgAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOVtmqvo9biy454mLRz9uXFAbOez24U3LbMOfHPMe7ET9+HZsX+kG9e/g+G3t8682+bKAyAAAAAAAAAAAAAAAAAAAAAA4e66Wms0GbFeOeaTx9+Gt3tBt9ts3TLprxxMTy2dYg9Xtj6WaNyx1+eS3uzwLGLwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB3nsfuttp3jFlpMx79opP/ALLY7S5q59PTJSYmJiPnDVfHaaXrePrWeYZ79M94jcNixYr25zV55/zwJXsgBAAAAAAAAAAAAAAAAAAAAB0ftftlNz2fNS0RM0rNq/d3j85KRek0t9JjiQasavBfTai+LJHFqz9JRe19T9mnQb1l1Na8YstuKvFCgAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPb+l28/D93nHkv+TJEVrE/5eIcjb9ROl1mHNWePctEiNponmOX10vslucbrsuDU88zaPm7oQAAAAAAAAAAAAAAAAAAAAAB4v1N2WNy2icta/PBE3mWBJiYniW1Gu09dVpMuC30vXiWuftltk7XvmowxXjHFuKix0YAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyv6Qb188mhzW4rSse5HllhrP7K7hfb950+StuKzePe+zZDQaqmt0uPUY55reOYGa5AAAAAAAAAAAAAAAAAAAAAADF/q7sfVw4tZgr8682vLKDrfaHQxuO0anTTWJm9OIBrEOZu+jtoNxz6a0THTtw4Y0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPtZmsxMfWGdfS3eI12z00trc2wUjnlgl7D023m227zjwe9xXUWisiVn8fKWi9YtWeYn6S+iAAAAAAAAAAAAAAAAAAAAAAMJ+rGyfg9fTVY6/LNM2tMMeNiPUDaY3LYs81rzlpT8rXvPitgzXxX7qzxIsTAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABXSZ7abU481J4tSeYSAbI+xm513HY9Nb3uclaR733d8w/6Q710s+TRZb8zktEUiWYBkAAAAAAAAAAAAAAAAAAAAAB+cmOuWk0vHNZ+sNevUDaLbXvWS0xxXNabQ2HY+9WNl/GbbOurXm+GvEAwiExNZmJjiYBoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdn7N7jO2bvg1XvcRS3MtkNp1ddbt+DUVnn36RLVxm70o3r8bt19Plt+bHMVrAlZBAEAAAAAAAAAAAAAAAAAAAAHF3PR012jyafJETW0fq5QDWT2k0F9v3XPivX3Ym8zX7OrZS9X9kmM1NwxV4pSvFuP8AMsWigAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPV+nW7Tt2/4IvbjDaebPKP3iyWxZK3pMxMT9YEbU4ckZcVMlfpaOYft5z2G3au6bLjtExM4oikvRiAAAAAAAAAAAAAAAAAAAAAAOl9rtrjddlzaf3eZn5/6a463BbT6rLitHHu3mP8A9bT3j3qzWf1jhgf1Q2Wdv3jqY68Y7V5mfMix4kAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZH9I97nT6+NuvbiuSZt82aInmOYavbLrb7fuGLUY54mJ4bJ7LrKa7bsOXHPP5K8/fgSucAIAAAAAAAAAAAAAAAAAAAAPG+pmzfEtlvfFXnLWYnnxD2SWqxRm0+THaOferMf/AIDVbJWaXtWfrWZh8d77ZbTbaN5y4ZrxEz73+3RDQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAARPE8szeke89fQzostucnvTMfZhl6T2E3edp3vHlm3Fbfl/2I2LE9PkjLhpkieYtWJUEAAAAAAAAAAAAAAAAAAAAAAYy9Xtl6uljX46c5PeiJ4j9GHZ+U8Nn980WPX7bmxZKxb8lpj78NbN30V9Br8unyxMWiefmLHDAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB+sVvcy0vH/W0S/IDYT073eN02PHN7c5Kzxx4h6thD0n3n8Juk6bNbjFNZ4+8s3RPMRMfqMvoAAAAAAAAAAAAAAAAAAAAAPkxExxP0lhb1b2SdNuE7hWvFMkxWGanm/bvaa7rsuSs15nFE3gGug/ebHbFktS8cTE/R+BoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcrbNTbS67DlpaY928TPH+OWyfs9uFdz2rDqaTExMcNYmX/SHeurgnbb2/wCOvvfMSsoACAAAAAAAAAAAAAAAAAAAAD8ZscZcV8dvpaOJfsBr36i7Rbbd9z2rXjDafyy8ozj6q7L+P2yufHX82KZtaYYOFABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAd57HbnfbN5w3rbiL2itvs6N+sd5x3rev1rPMA2o0uemowVy455rMKvG+me7xr9jw4b25y4682eyGQAAAAAAAAAAAAAAAAAAAAAHF3PTRrNBm09oiffrMNcPajbp2zedRpvdmK0txDZliX1f2X3bYtZhp87TM3mBYxUAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPcel29fD91nDe3yzcViGd4nn6NWNu1M6TW4dRX647e82P9lNyjdNl0+om3N7V5tAldwAIAAAAAAAAAAAAAAAAAAAAOm9rNuruOyanF7vN5pxV3L5MRMcT9Aasa/TW0esy6e/djtxKD3Hqjss7fus6mtflntNnhxQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGV/R/ev+bR578RERFIlih3HsruFtu3rTZvemKRfm3kRsuOPt+prrNHi1FO3JXmHIEAAAAAAAAAAAAAAAAAAAAAAeQ9SdoruGx5c0V97Jip+VgHJS2O9qWji0TxMNqNRhpqMNsWSOa2+sS119ttrttm9Z4mvFcl5mv2Fjz4AoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAETMTzE8SAM7+l28xuG0xp5tzbT1ir3DAPppu87fveLBNuMea/wCZnzHeMlIvX5xMcwMv0AAAAAAAAAAAAAAAAAAAAAAxp6u7L19JGvpX/hr8+GS3A3zQV3LbcumvHMXgGr4529aS2i3LPhtXiIvMR9nBGgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABXS576bPXNjni1Z5hsZ7G7nTctlwWi3NqUiLfdrcyh6Q710s1tBkt88tvkJWXwBAAAAAAAAAAAAAAAAAAAAAAGF/VzZZ0+vpq8NeMfu/mnj9WOGxvtztNd12LNiivN/rEtdtTinDqMmOY4920wLEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABTB3pqYO8RMAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdn7Objbat2w6uszzSXWEfUG0m1aqus0GDNW3PvUiZctj/0n3r8Ztd8Ga//ANK2iKxP+GQBkAAAAAAAAAAAAAAAAAAAAAB+clffpas/rHDX/wBR9m+Fb3aMdfyXj35mP8y2CeD9VNmjW7RbU4685azEfL68AwYPtomtprP1ieHwaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHqvTvdp2zf8M3vxinnmP05bBYbxlxUyR9LViWq2DJbFlpes8TExPLYn2F3au7bLjyRPM04p/oSvRgCAAAAAAAAAAAAAAAAAAAADj7hp6arSZcV6xaJrPET9nIAaze1G222vdsuC8TEzM2j/ANl1LK/rBsvERuVKfWYr9GKBQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGR/SPeZwa/8BktxjtE2/wDWOHO2XW5NBuGLNjnifeiJ+3IjaCJ5iJj9X1wdl1tNft+LNjnmPdiJ+/DnCAAAAAAAAAAAAAAAAAAAAAAOp9p9tx7ntObFkjn3azaPvw1u1+nvpdVkxZY4tEz8m01qxas1n6THDBvqpss6Pdr6vHXjFfiIFjwYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAETxMTH6ADM/pHvPX0P4C9ub15syQ1z9hN3naN7pkiZ4ycU/22Jw5Iy4qXrPMTESM1+wAAAAAAAAAAAAAAAAAAAAAHk/UXZo3XZbcVj3sXN3rEtVhjPp8mK30vWYBqtes1tMTExMf5fHo/bvap2vftRjrXjHz+WXnBQAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUwd6amDvETAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfvDecWWmSPrW0S2C9O93jc9hwze3OWOeY/Xhr09/6Ub1Oj3S+DNf8A+dqxFYn/ACJWcAj5wCAAAAAAAAAAAAAAAAAAAAAAMb+reyfidHj1WGv56zNrz4YYbQb3o667bNRgmvM2pMQ1u33QW23c82ltHHTtwLHAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFMHempg7xEwBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABy9q1VtJuGDNW3Hu3iZcQBs57O7jXdNpw6qs8xeHZsWek/tBjjTW0OoycRir8uWR/iek/dgZcwcP4npP3YPiek/dgHMHD+J6T92D4npP3YBzBw/iek/dg+J6T92AcwcP4npP3YPiek/dgHMHD+J6T92D4npP3YBzBw/iek/dg+J6T92AcwcP4npP3YPiek/dgHMHD+J6T92D4npP3YBzBw/iek/dg+J6T92AcwcP4npP3YPiek/dgHMHD+J6T92D4npP3YBzGGvV3ZPw+qprcVOeraZtMMs/E9J+7DoPbSmi3PZdRX34nJWk+59wa9imoxThzXxz9azwmNAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDUZtPMzgyWpM/Wazw5HxTX/y839nDAcz4pr/AOXm/sfFNf8Ay839nDAcz4pr/wCXm/sfFNf/AC839nDAcz4pr/5eb+x8U1/8vN/ZwwHM+Ka/+Xm/sfFNf/Lzf2cMBzPimv8A5eb+x8U1/wDLzf2cMBzPimv/AJeb+x8U1/8ALzf2cMBzPimv/l5v7HxTX/y839nDAcz4pr/5eb+x8U1/8vN/ZwwHM+Ka/wDl5v7HxTX/AMvN/ZwwHM+Ka/8Al5v7HxTX/wAvN/ZwwHM+Ka/+Xm/sfFNf/Lzf2cMBzPimv/l5v7Pltz11omLarLMT+nvOIA+2tNpmbTzM/rL4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApg701MHeImAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKYO9NTB3iJgCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACmDvTUwd4iYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38AmKdG/g6N/AJinRv4OjfwCYp0b+Do38Ampg7zo38KYMNouD/2Q==" alt="Logotipo de Landa Consultoría y Proyectos">
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

st.markdown(
    '<p class="data-source">🔎 <b>Fuente de los datos:</b> '
    '<a href="https://contrataciondelestado.es/" target="_blank" rel="noopener noreferrer">'
    'Plataforma de Contratación del Sector Público</a>. '
    'Consulta siempre la documentación oficial antes de preparar una oferta.</p>',
    unsafe_allow_html=True,
)

opciones_vista = [
    "⚡ Últimas actualizaciones",
    "🗂️ Histórico",
    "📊 Gráficos",
    "🗺️ Mapa",
]
if ES_PREMIUM and LISTS_CONFIGURADO:
    opciones_vista.insert(0, "⭐ Favoritos")
vista_principal = st.segmented_control(
    "Vista del dashboard",
    opciones_vista,
    default="⚡ Últimas actualizaciones",
    selection_mode="single",
    key="vista_principal",
    label_visibility="collapsed",
)
if vista_principal is None:
    vista_principal = "⚡ Últimas actualizaciones"

if vista_principal == "⭐ Favoritos":
    favoritos_actuales = cargar_favoritos_compartidos()
    df_indicadores = df_combinado[
        df_combinado["id"].astype(str).isin(favoritos_actuales)
    ]
    etiqueta_cantidad = "Favoritos compartidos"
    etiqueta_actualizacion = "Seguimiento Premium"
elif vista_principal == "⚡ Últimas actualizaciones":
    df_indicadores = df_radar_filtrado
    etiqueta_cantidad = "Actualizaciones filtradas"
    etiqueta_actualizacion = "Última actualización feed"
elif vista_principal == "🗂️ Histórico":
    df_indicadores = df_f
    etiqueta_cantidad = "Licitaciones filtradas"
    etiqueta_actualizacion = "Última actualización BD"
elif vista_principal == "📊 Gráficos":
    fuente_indicadores = st.session_state.get(
        "fuente_graficos", "Últimas actualizaciones"
    )
    if fuente_indicadores == "Últimas actualizaciones":
        df_indicadores = df_radar_filtrado
    elif fuente_indicadores == "Combinado":
        df_indicadores = df_combinado
    else:
        df_indicadores = df_f
    etiqueta_cantidad = "Licitaciones filtradas"
    etiqueta_actualizacion = "Última actualización"
else:
    fuente_indicadores = st.session_state.get(
        "fuente_mapa", "Últimas actualizaciones"
    )
    if fuente_indicadores == "Últimas actualizaciones":
        df_indicadores = df_radar_filtrado
    elif fuente_indicadores == "Combinado":
        df_indicadores = df_combinado
    else:
        df_indicadores = df_f
    etiqueta_cantidad = "Licitaciones filtradas"
    etiqueta_actualizacion = "Última actualización"

indicadores_usan_feed = (
    vista_principal == "⚡ Últimas actualizaciones"
    or (
        vista_principal in {"📊 Gráficos", "🗺️ Mapa"}
        and fuente_indicadores == "Últimas actualizaciones"
    )
)
if indicadores_usan_feed and fecha_feed_catalogo:
    ultima_act = pd.to_datetime(fecha_feed_catalogo, errors="coerce", utc=True)
    if pd.notnull(ultima_act):
        ultima_act = ultima_act.tz_convert("Europe/Madrid")
else:
    ultima_act = (
        df_indicadores["fecha_act_dt"].max()
        if not df_indicadores.empty and "fecha_act_dt" in df_indicadores.columns
        else pd.NaT
    )
fecha_act_fmt = (
    ultima_act.strftime("%d/%m/%Y %H:%M")
    if pd.notnull(ultima_act) else "No disponible"
)
volumen_total = (
    pd.to_numeric(df_indicadores["pbl_sin_iva"], errors="coerce").fillna(0).sum()
    if not df_indicadores.empty and "pbl_sin_iva" in df_indicadores.columns
    else 0.0
)
presupuesto_medio = (
    pd.to_numeric(
        df_indicadores["pbl_sin_iva"], errors="coerce"
    ).dropna().mean()
    if not df_indicadores.empty and "pbl_sin_iva" in df_indicadores.columns
    else 0.0
)

kpi1, kpi2, kpi3, kpi4 = st.columns(4)
with kpi1:
    st.markdown(f'<div class="metric-box-grid top-kpi"><div class="metric-val-grid">{len(df_indicadores)}</div><div class="metric-lbl-grid">{etiqueta_cantidad}</div></div>', unsafe_allow_html=True)
with kpi2:
    st.markdown(f'<div class="metric-box-grid top-kpi"><div class="metric-val-grid">{formato_eur(volumen_total)}</div><div class="metric-lbl-grid">Volumen Total (sin IVA)</div></div>', unsafe_allow_html=True)
with kpi3:
    st.markdown(f'<div class="metric-box-grid top-kpi"><div class="metric-val-grid">{formato_eur(presupuesto_medio)}</div><div class="metric-lbl-grid">Presupuesto Medio (sin IVA)</div></div>', unsafe_allow_html=True)
with kpi4:
    st.markdown(f'<div class="metric-box-grid top-kpi"><div class="metric-val-grid" style="font-size:1.15rem; margin-top:2px;">{fecha_act_fmt}</div><div class="metric-lbl-grid">{etiqueta_actualizacion}</div><div style="margin-top:4px; font-size:0.72rem; font-weight:700; color:#198754;">{texto_antiguedad(ultima_act)}</div></div>', unsafe_allow_html=True)

filtros_activos = []
if busqueda_texto.strip(): filtros_activos.append(f'Texto: “{busqueda_texto.strip()}”')
if tipo_sel: filtros_activos.append('Tipo: ' + ', '.join(tipo_sel))
if cpv_2dig.strip(): filtros_activos.append('CPV: ' + cpv_2dig.strip())
if estados_sel: filtros_activos.append('Estado: ' + ', '.join(opciones_estado[e] for e in estados_sel))
if ccaa_sel: filtros_activos.append('CC. AA.: ' + ', '.join(ccaa_sel))
if prov_sel: filtros_activos.append('Provincia: ' + ', '.join(prov_sel))
if muni_sel: filtros_activos.append('Municipio: ' + ', '.join(muni_sel))
if pbl_min_val > 0 or pbl_max_val < max_pbl_db:
    filtros_activos.append(f'Presupuesto: {formato_eur(pbl_min_val)} – {formato_eur(pbl_max_val)}')
if fecha_rango and len(fecha_rango) == 2:
    filtros_activos.append(f'Fecha límite: {fecha_rango[0].strftime("%d/%m/%Y")} – {fecha_rango[1].strftime("%d/%m/%Y")}')
if organo_sel: filtros_activos.append('Órgano: ' + ', '.join(organo_sel))
if adjudicatario_sel: filtros_activos.append('Adjudicatario: ' + ', '.join(adjudicatario_sel))

resumen_filtros = ' · '.join(filtros_activos) if filtros_activos else 'Ninguno'
st.markdown(
    f'<div style="margin-top:0.65rem; padding:0.55rem 0.75rem; border-radius:8px; background:#eef4ff; color:#334155; font-size:0.82rem;"><b>🔎 Filtros aplicados:</b> {html.escape(resumen_filtros)}</div>',
    unsafe_allow_html=True
)

st.divider()

def render_grid_tarjetas(df_vista, key_prefix):
    favoritos_actuales = {}
    error_favoritos = None
    if ES_PREMIUM and LISTS_CONFIGURADO:
        try:
            favoritos_actuales = cargar_favoritos_compartidos()
        except requests.RequestException as error:
            error_favoritos = error
            st.warning(
                "El dashboard funciona, pero no se ha podido conectar con los "
                "favoritos de Microsoft Lists."
            )
            st.caption(f"Detalle Microsoft: {_detalle_error_graph(error)}")
    for i in range(0, len(df_vista), 3):
        cols = st.columns(3)
        lote = df_vista.iloc[i:i+3]
        
        for col, (_, r) in zip(cols, lote.iterrows()):
            st_txt, badge_cls = MAPA_ESTADOS.get(r['estado'], (r['estado'], 'badge-res'))
            st_txt = texto_seguro(st_txt, "Estado desconocido")
            url_oficial = url_externa_segura(r.get("url_licitacion"))
            link_html = f'<a href="{html.escape(url_oficial, quote=True)}" target="_blank" rel="noopener noreferrer" class="external-link-btn" title="Ver ficha en la Plataforma">🔗</a>' if url_oficial else ''
            movimiento = r.get("movimiento")
            if movimiento == "Nueva licitación":
                movimiento_html = '<span title="Nueva licitación" aria-label="Nueva licitación" style="display:inline-block;width:13px;height:13px;border-radius:50%;background:#22c55e;border:2px solid #dcfce7;margin:auto 4px;"></span>'
            elif movimiento == "Actualizada":
                movimiento_html = '<span title="Licitación actualizada" aria-label="Licitación actualizada" style="display:inline-block;width:13px;height:13px;border-radius:50%;background:#3b82f6;border:2px solid #dbeafe;margin:auto 4px;"></span>'
            else:
                movimiento_html = ""

            muni = r.get('municipio')
            prov = r.get('provincia')
            query_maps = f"{muni}, {prov}" if pd.notnull(muni) and pd.notnull(prov) else (muni or "España")
            maps_url = f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(str(query_maps))}"
            maps_html = f'<a href="{html.escape(maps_url, quote=True)}" target="_blank" rel="noopener noreferrer" class="maps-btn" title="Ver ubicación en el mapa">📍</a>'
            titulo_safe = texto_seguro(r.get('titulo'), 'Sin título')
            organo_safe = texto_seguro(r.get('organo_contratante'), 'Organismo N/A')
            muni_safe = texto_seguro(muni)
            prov_safe = texto_seguro(prov)
            tipo_safe = texto_seguro(r.get('tipo_contrato_desc'), 'Otros')
            expediente_safe = texto_seguro(r.get('expediente'))
            cpv_safe = texto_seguro(r.get('cpv'))
            
            with col:
                with st.container(border=True):
                    licitacion_id = str(r.get("id") or "").strip()
                    token_acciones = hashlib.sha1(
                        f"{key_prefix}-{licitacion_id}".encode("utf-8")
                    ).hexdigest()[:12]
                    with st.container(key=f"acciones_{token_acciones}"):
                        if ES_PREMIUM:
                            cabecera_col, fav_col, enlace_col, mapa_col = st.columns(
                                [6, 0.8, 0.8, 0.8], gap="small"
                            )
                        else:
                            cabecera_col, enlace_col, mapa_col = st.columns(
                                [6, 0.8, 0.8], gap="small"
                            )
                            fav_col = None
                        with cabecera_col:
                            st.markdown(
                                f'<span class="{badge_cls}">{st_txt}</span> {movimiento_html}',
                                unsafe_allow_html=True,
                            )
                        if fav_col is not None:
                            with fav_col:
                                if LISTS_CONFIGURADO and error_favoritos is None:
                                    es_favorito = licitacion_id in favoritos_actuales
                                    with st.container(
                                        key=("fav_activo_" if es_favorito else "fav_vacio_")
                                        + token_acciones
                                    ):
                                        if st.button(
                                            "★" if es_favorito else "☆",
                                            key=f"fav_{token_acciones}",
                                            help=(
                                                "Quitar de favoritos"
                                                if es_favorito
                                                else "Añadir a favoritos"
                                            ),
                                            use_container_width=True,
                                        ):
                                            try:
                                                alternar_favorito(r.to_dict())
                                                st.rerun()
                                            except requests.RequestException as error_favorito:
                                                st.toast(
                                                    "No se pudo actualizar Microsoft Lists: "
                                                    f"{_detalle_error_graph(error_favorito)}",
                                                    icon="⚠️",
                                                )
                                else:
                                    st.button(
                                        "☆", disabled=True, key=f"fav_off_{token_acciones}",
                                        help="Favoritos temporalmente no disponibles",
                                        use_container_width=True,
                                    )
                        with enlace_col:
                            if url_oficial:
                                st.link_button(
                                    "🔗", url_oficial, help="Ver ficha en la Plataforma",
                                    use_container_width=True,
                                )
                        with mapa_col:
                            st.link_button(
                                "📍", maps_url, help="Ver ubicación en el mapa",
                                use_container_width=True,
                            )
                    st.markdown(f"""
                    <h5 style="margin: 10px 0 6px 0; color: #1a252c; line-height: 1.35; font-size: 0.95rem; min-height: 2.7em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
                        {titulo_safe}
                    </h5>
                    <p style="margin: 0; font-size: 0.8rem; color: #495057; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                        🏛️ <b>{organo_safe}</b>
                    </p>
                    <p style="margin: 2px 0 4px 0; font-size: 0.78rem; color: #6c757d;">
                        📍 {muni_safe} ({prov_safe}) | 📦 <b>{tipo_safe}</b>
                    </p>
                    <p style="margin: 0 0 10px 0; font-size: 0.75rem; color: #8b949e;">
                        <b>Exp:</b> {expediente_safe} | <b>CPV:</b> {cpv_safe}
                    </p>
                    """, unsafe_allow_html=True)
                    
                    if r.get("estado") in {"ADJ", "RES", "PARCIAL"}:
                        importe_adjudicado = r.get("importe_adjudicacion_con_iva")
                        baja_importe, baja_porcentaje = calcular_baja(
                            r.get("pbl_con_iva"), importe_adjudicado
                        )
                        baja_valor = formato_eur(baja_importe) if baja_importe is not None else "No disponible"
                        baja_detalle = (
                            f"{baja_porcentaje:.2f} %".replace(".", ",")
                            if baja_porcentaje is not None else "Sin datos suficientes"
                        )
                        adjudicatario_safe = texto_seguro(
                            r.get("adjudicatario"), "No disponible"
                        )
                        ma1, ma2, ma3 = st.columns(3)
                        with ma1:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size:0.82rem;">{formato_eur(r.get("pbl_con_iva"))}</div><div class="metric-lbl-grid">PBL CON IVA</div></div>', unsafe_allow_html=True)
                        with ma2:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size:0.82rem;">{formato_eur(importe_adjudicado)}</div><div class="metric-lbl-grid">ADJUDICACIÓN CON IVA</div></div>', unsafe_allow_html=True)
                        with ma3:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size:0.82rem;">{baja_valor}</div><div class="metric-lbl-grid">BAJA</div><div style="margin-top:4px;font-size:0.72rem;font-weight:700;color:#198754;">{baja_detalle}</div></div>', unsafe_allow_html=True)

                        mi1, mi2 = st.columns([2, 1])
                        with mi1:
                            st.markdown(f'<div class="metric-box-grid card-metric" title="{adjudicatario_safe}"><div class="metric-val-grid" style="font-size:0.78rem;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;">{adjudicatario_safe}</div><div class="metric-lbl-grid">ADJUDICATARIO</div></div>', unsafe_allow_html=True)
                        with mi2:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size:0.82rem;color:#495057;">{formato_fecha_corta(r.get("fecha_adjudicacion"))}</div><div class="metric-lbl-grid">FECHA ADJUDICACIÓN</div></div>', unsafe_allow_html=True)
                    else:
                        mc1, mc2 = st.columns(2)
                        with mc1:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size: 0.85rem;">{formato_eur(r.get("pbl_con_iva"))}</div><div class="metric-lbl-grid">PBL CON IVA</div></div>', unsafe_allow_html=True)
                        with mc2:
                            st.markdown(f'<div class="metric-box-grid card-metric"><div class="metric-val-grid" style="font-size: 0.82rem; color: #495057;">{formato_fecha(r["fecha_limite"])}</div><div class="metric-lbl-grid">FECHA PRESENTACIÓN</div><div style="margin-top:4px; font-size:0.72rem; font-weight:700; color:#198754;">{texto_dias_restantes(r["fecha_limite"])}</div></div>', unsafe_allow_html=True)

                    with st.expander("📄 Ver documentación"):
                        docs = json.loads(r['documentos_adjuntos']) if r['documentos_adjuntos'] else []
                        if docs:
                            st.write("**Documentos descargables:**")
                            for d in docs:
                                doc_url = url_externa_segura(d.get('url'))
                                if doc_url:
                                    etiqueta = f"{d.get('tipo', 'Documento')} - {d.get('nombre', 'Archivo')}"
                                    st.link_button(etiqueta, doc_url, use_container_width=True)
                        else:
                            st.write("Sin documentos adjuntos directos.")

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
                            except Exception:
                                st.markdown(f"🏗️ **Alcance Técnico:**\n{raw_resumen}")
                        else:
                            st.info("Resumen pendiente de análisis privado. La aplicación pública no ejecuta modelos de IA.")



if df_f.empty and df_radar_catalogo.empty:
    st.warning("⚠️ No se ha encontrado ninguna licitación que coincida con los filtros aplicados. Prueba a relajar los criterios de búsqueda.")
else:
    st.write("")

    if vista_principal == "⭐ Favoritos":
        st.subheader("⭐ Favoritos compartidos")
        st.caption(
            "Licitaciones seleccionadas desde el acceso Premium y sincronizadas "
            "con Microsoft Lists."
        )
        favoritos_actuales = cargar_favoritos_compartidos()
        df_favoritos = df_combinado[
            df_combinado["id"].astype(str).isin(favoritos_actuales)
        ].copy()
        if df_favoritos.empty:
            st.info("Todavía no hay licitaciones favoritas.")
        else:
            df_favoritos = df_favoritos.sort_values(
                "fecha_act_dt", ascending=False, na_position="last"
            )
            render_grid_tarjetas(df_favoritos, "favoritos")

    elif vista_principal == "⚡ Últimas actualizaciones":
        st.subheader("⚡ Radar de actualizaciones")
        st.caption(
            "Cambios publicados recientemente en el feed oficial de la Plataforma de "
            "Contratación del Sector Público. Actualización automática diaria a las "
            "09:07, 13:07, 17:07 y 21:07 (hora de Madrid)."
        )
        try:
            if error_feed_catalogo:
                raise RuntimeError(error_feed_catalogo)
            df_radar = df_radar_filtrado.copy()
            fecha_feed = fecha_feed_catalogo

            if not df_radar_catalogo.empty:
                fecha_feed_fmt = formato_fecha(fecha_feed)
                nuevas = int((df_radar["movimiento"] == "Nueva licitación").sum())
                actualizadas = int((df_radar["movimiento"] == "Actualizada").sum())
                st.markdown(
                    f"**{len(df_radar)} resultados con los filtros actuales** · "
                    f"🟢 {nuevas} nuevas · 🔵 {actualizadas} actualizadas · "
                    f"{metadata_feed_catalogo['paginas']} páginas oficiales · "
                    f"{metadata_feed_catalogo['bajas']} cierres/anulaciones · "
                    f"Feed oficial: {fecha_feed_fmt}"
                )
                if metadata_feed_catalogo["parcial"]:
                    st.warning(
                        "La portada del feed está disponible, pero alguna página anterior "
                        "no respondió. Se muestran los cambios que sí pudieron recuperarse."
                    )

                if df_radar.empty:
                    st.info(
                        "El feed funciona correctamente, pero ninguna actualización "
                        "reciente coincide con los filtros actuales."
                    )
                else:
                    criterio_radar = st.selectbox(
                        "🔃 Ordenar tarjetas por:",
                        [
                            "Actualización (Más reciente)",
                            "Actualización (Más antigua)",
                            "Fecha límite (Más cercana)",
                            "Fecha límite (Más lejana)",
                            "Fecha de adjudicación (Más cercana)",
                            "Fecha de adjudicación (Más lejana)",
                            "Presupuesto (Mayor a menor)",
                            "Presupuesto (Menor a mayor)",
                        ],
                        key="select_orden_radar",
                    )
                    if criterio_radar == "Actualización (Más antigua)":
                        df_radar = df_radar.sort_values(
                            "fecha_act_dt", ascending=True, na_position="last"
                        )
                    elif criterio_radar == "Fecha límite (Más cercana)":
                        df_radar = df_radar.sort_values(
                            "fecha_limite_dt", ascending=True, na_position="last"
                        )
                    elif criterio_radar == "Fecha límite (Más lejana)":
                        df_radar = df_radar.sort_values(
                            "fecha_limite_dt", ascending=False, na_position="last"
                        )
                    elif criterio_radar == "Fecha de adjudicación (Más cercana)":
                        df_radar = df_radar.assign(
                            fecha_adjudicacion_dt=pd.to_datetime(
                                df_radar["fecha_adjudicacion"], errors="coerce"
                            )
                        ).sort_values(
                            "fecha_adjudicacion_dt", ascending=False,
                            na_position="last"
                        )
                    elif criterio_radar == "Fecha de adjudicación (Más lejana)":
                        df_radar = df_radar.assign(
                            fecha_adjudicacion_dt=pd.to_datetime(
                                df_radar["fecha_adjudicacion"], errors="coerce"
                            )
                        ).sort_values(
                            "fecha_adjudicacion_dt", ascending=True,
                            na_position="last"
                        )
                    elif criterio_radar == "Presupuesto (Mayor a menor)":
                        df_radar = df_radar.sort_values(
                            "pbl_sin_iva", ascending=False, na_position="last"
                        )
                    elif criterio_radar == "Presupuesto (Menor a mayor)":
                        df_radar = df_radar.sort_values(
                            "pbl_sin_iva", ascending=True, na_position="last"
                        )
                    else:
                        df_radar = df_radar.sort_values(
                            "fecha_act_dt", ascending=False, na_position="last"
                        )
                    items_radar = 12
                    total_radar = len(df_radar_filtrado)
                    paginas_radar = max(
                        1, (total_radar + items_radar - 1) // items_radar
                    )
                    if "pagina_radar" not in st.session_state:
                        st.session_state.pagina_radar = 1
                    if st.session_state.pagina_radar > paginas_radar:
                        st.session_state.pagina_radar = paginas_radar

                    rad_ant, rad_info, rad_sig = st.columns([1, 2, 1])
                    with rad_ant:
                        if st.button(
                            "⬅️ Anterior", key="radar_anterior",
                            use_container_width=True,
                            disabled=st.session_state.pagina_radar <= 1,
                        ):
                            st.session_state.pagina_radar -= 1
                            st.rerun()
                    with rad_info:
                        st.markdown(
                            f"<p style='text-align:center;font-weight:600;margin-top:6px;'>"
                            f"Página {st.session_state.pagina_radar} de {paginas_radar} "
                            f"(Total: {total_radar} actualizaciones)</p>",
                            unsafe_allow_html=True,
                        )
                    with rad_sig:
                        if st.button(
                            "Siguiente ➡️", key="radar_siguiente",
                            use_container_width=True,
                            disabled=st.session_state.pagina_radar >= paginas_radar,
                        ):
                            st.session_state.pagina_radar += 1
                            st.rerun()

                    inicio_radar = (
                        st.session_state.pagina_radar - 1
                    ) * items_radar
                    fin_radar = inicio_radar + items_radar
                    render_grid_tarjetas(
                        df_radar.iloc[inicio_radar:fin_radar],
                        "radar",
                    )
            else:
                st.info("El feed oficial no contiene actualizaciones en este momento.")
        except Exception as error_feed:
            st.warning(
                "Ahora mismo no ha sido posible consultar el feed oficial. "
                "El histórico del dashboard sigue disponible con normalidad."
            )
            st.caption(f"Detalle técnico: {error_feed}")

    elif vista_principal == "📊 Gráficos":
        st.subheader("📊 Análisis de las licitaciones")
        st.caption("Los gráficos se actualizan automáticamente con los filtros aplicados.")
        fuente_graficos = st.radio(
            "Datos que quieres analizar:",
            ["Últimas actualizaciones", "Histórico", "Combinado"],
            horizontal=True,
            key="fuente_graficos",
        )
        if fuente_graficos == "Últimas actualizaciones":
            df_graficos = df_radar_filtrado.copy()
        elif fuente_graficos == "Combinado":
            df_graficos = pd.concat(
                [df_f, df_radar_filtrado], ignore_index=True, sort=False
            )
            if "id" in df_graficos.columns:
                df_graficos = df_graficos.drop_duplicates(
                    subset=["id"], keep="last"
                )
        else:
            df_graficos = df_f.copy()

        if df_graficos.empty:
            st.info("No hay datos de esta fuente que coincidan con los filtros actuales.")
            df_graficos = df_f.iloc[0:0].copy()
        campos_graficos = {
            "comunidad": ("comunidad_autonoma", "No especificada"),
            "provincia": ("provincia", "No especificada"),
            "municipio": ("municipio", "No especificado"),
            "organo": ("organo_contratante", "No especificado"),
        }
        for clave, (columna, fallback) in campos_graficos.items():
            df_graficos[f"{clave}_grafico"] = df_graficos[columna].fillna(
                fallback
            )
        df_graficos["pbl_sin_iva"] = pd.to_numeric(
            df_graficos["pbl_sin_iva"], errors="coerce"
        ).fillna(0)
        df_graficos["importe_adjudicacion_con_iva"] = pd.to_numeric(
            df_graficos["importe_adjudicacion_con_iva"], errors="coerce"
        ).fillna(0)

        color_azul = "#2563eb"
        color_verde = "#0f9d6e"
        color_naranja = "#f59e0b"

        def agrupar_presupuesto(campo):
            return (
                df_graficos.groupby(campo, as_index=False)["pbl_sin_iva"]
                .sum()
                .sort_values("pbl_sin_iva", ascending=False)
                .head(15)
            )

        def agrupar_licitaciones(campo):
            tabla = (
                df_graficos.groupby(campo, as_index=False)
                .size()
                .rename(columns={"size": "licitaciones"})
                .sort_values("licitaciones", ascending=False)
                .head(15)
            )
            tabla["licitaciones"] = tabla["licitaciones"].astype(int)
            return tabla

        def incluir_etiqueta_envuelta(tabla, campo, ancho):
            resultado = tabla.copy()
            resultado["etiqueta_grafico"] = resultado[campo].map(
                lambda valor: "|".join(
                    textwrap.wrap(
                        str(valor),
                        width=ancho,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                )
            )
            return resultado

        def grafico_horizontal(
            tabla,
            campo_nombre,
            campo_valor,
            titulo_nombre,
            titulo_valor,
            color,
            es_cantidad=False,
            ancho_etiqueta=72,
        ):
            datos = incluir_etiqueta_envuelta(
                tabla, campo_nombre, ancho_etiqueta
            )
            max_lineas = (
                int(datos["etiqueta_grafico"].str.count(r"\|").max()) + 1
                if not datos.empty
                else 1
            )
            alto_por_fila = max(38, max_lineas * 16)
            altura = max(300, len(datos) * alto_por_fila)
            formato = "d" if es_cantidad else ",.2f"
            eje_x = (
                alt.Axis(format="d", tickMinStep=1)
                if es_cantidad
                else alt.Axis(format=",.2f")
            )
            return (
                alt.Chart(datos)
                .mark_bar(color=color, cornerRadiusEnd=4)
                .encode(
                    x=alt.X(
                        f"{campo_valor}:Q",
                        title=titulo_valor,
                        axis=eje_x,
                    ),
                    y=alt.Y(
                        "etiqueta_grafico:N",
                        title=None,
                        sort="-x",
                        axis=alt.Axis(
                            labelExpr="split(datum.label, '|')",
                            labelLimit=520,
                            labelLineHeight=14,
                            labelOverlap=False,
                            labelPadding=8,
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip(
                            f"{campo_nombre}:N", title=titulo_nombre
                        ),
                        alt.Tooltip(
                            f"{campo_valor}:Q",
                            title=titulo_valor,
                            format=formato,
                        ),
                    ],
                )
                .properties(height=altura)
            )

        presupuesto_comunidad = agrupar_presupuesto("comunidad_grafico")
        presupuesto_provincia = agrupar_presupuesto("provincia_grafico")
        presupuesto_municipio = agrupar_presupuesto("municipio_grafico")
        presupuesto_organo = agrupar_presupuesto("organo_grafico")
        presupuesto_adjudicatario = (
            df_graficos[
                df_graficos["adjudicatario"].notna()
                & df_graficos["adjudicatario"].astype(str).str.strip().ne("")
                & (df_graficos["importe_adjudicacion_con_iva"] > 0)
            ]
            .groupby("adjudicatario", as_index=False)["importe_adjudicacion_con_iva"]
            .sum()
            .sort_values("importe_adjudicacion_con_iva", ascending=False)
            .head(10)
        )
        por_comunidad = agrupar_licitaciones("comunidad_grafico")
        por_provincia = agrupar_licitaciones("provincia_grafico")
        por_municipio = agrupar_licitaciones("municipio_grafico")
        por_organo = agrupar_licitaciones("organo_grafico")
        tramos_presupuesto = [
            "Menos de 25.000 €",
            "25.000–50.000 €",
            "50.000–100.000 €",
            "100.000–200.000 €",
            "Más de 200.000 €"
        ]
        df_graficos["tramo_presupuesto"] = pd.cut(
            df_graficos["pbl_sin_iva"],
            bins=[-np.inf, 25000, 50000, 100000, 200000, np.inf],
            labels=tramos_presupuesto,
            right=False
        )
        por_tramo = (
            df_graficos.groupby("tramo_presupuesto", observed=False)
            .size()
            .reindex(tramos_presupuesto, fill_value=0)
            .rename("licitaciones")
            .reset_index()
        )

        grafico_presupuesto_comunidad = grafico_horizontal(
            presupuesto_comunidad,
            "comunidad_grafico",
            "pbl_sin_iva",
            "Comunidad autónoma",
            "Presupuesto total sin IVA (€)",
            "#0f9d6e",
        )
        grafico_presupuesto_provincia = grafico_horizontal(
            presupuesto_provincia,
            "provincia_grafico",
            "pbl_sin_iva",
            "Provincia",
            "Presupuesto total sin IVA (€)",
            "#14b8a6",
        )
        grafico_presupuesto_municipio = grafico_horizontal(
            presupuesto_municipio,
            "municipio_grafico",
            "pbl_sin_iva",
            "Municipio",
            "Presupuesto total sin IVA (€)",
            "#22c55e",
        )
        grafico_presupuesto_organo = grafico_horizontal(
            presupuesto_organo,
            "organo_grafico",
            "pbl_sin_iva",
            "Órgano de contratación",
            "Presupuesto total sin IVA (€)",
            "#0891b2",
        )
        grafico_presupuesto_adjudicatario = grafico_horizontal(
            presupuesto_adjudicatario,
            "adjudicatario",
            "importe_adjudicacion_con_iva",
            "Adjudicatario",
            "Importe adjudicado acumulado con IVA (€)",
            "#0d9488",
        )
        grafico_comunidades = grafico_horizontal(
            por_comunidad,
            "comunidad_grafico",
            "licitaciones",
            "Comunidad autónoma",
            "Número de licitaciones",
            "#7c3aed",
            es_cantidad=True,
        )
        grafico_provincias = grafico_horizontal(
            por_provincia,
            "provincia_grafico",
            "licitaciones",
            "Provincia",
            "Número de licitaciones",
            color_azul,
            es_cantidad=True,
        )
        grafico_municipios = grafico_horizontal(
            por_municipio,
            "municipio_grafico",
            "licitaciones",
            "Municipio",
            "Número de licitaciones",
            "#4f46e5",
            es_cantidad=True,
        )
        grafico_organos = grafico_horizontal(
            por_organo,
            "organo_grafico",
            "licitaciones",
            "Órgano de contratación",
            "Número de licitaciones",
            "#0284c7",
            es_cantidad=True,
        )
        grafico_tramos = (
            alt.Chart(por_tramo)
            .mark_bar(color=color_naranja, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("tramo_presupuesto:N", title=None, sort=tramos_presupuesto, axis=alt.Axis(labelAngle=-25)),
                y=alt.Y(
                    "licitaciones:Q",
                    title="Número de licitaciones",
                    axis=alt.Axis(format="d", tickMinStep=1),
                ),
                tooltip=[
                    alt.Tooltip("tramo_presupuesto:N", title="Tramo"),
                    alt.Tooltip(
                        "licitaciones:Q", title="Licitaciones", format="d"
                    )
                ]
            )
            .properties(height=300)
        )
        graficos_ordenados = [
            (
                "Presupuesto por comunidad autónoma",
                grafico_presupuesto_comunidad,
            ),
            ("Presupuesto por provincia", grafico_presupuesto_provincia),
            ("Presupuesto por municipio", grafico_presupuesto_municipio),
            (
                "Presupuesto por órgano de contratación",
                grafico_presupuesto_organo,
            ),
            ("Licitaciones por comunidad autónoma", grafico_comunidades),
            ("Licitaciones por provincia", grafico_provincias),
            ("Licitaciones por municipio", grafico_municipios),
            ("Licitaciones por órgano de contratación", grafico_organos),
            (
                "Presupuesto por adjudicatario",
                grafico_presupuesto_adjudicatario,
            ),
            ("Distribución de presupuesto", grafico_tramos),
        ]
        for titulo_grafico, grafico in graficos_ordenados:
            with st.container(border=True):
                st.markdown(f"#### {titulo_grafico}")
                st.altair_chart(grafico, use_container_width=True)

    elif vista_principal == "🗺️ Mapa":
        st.subheader("📍 Ubicación de las licitaciones")
        st.caption(
            "Las ubicaciones del feed son aproximadas: se calculan mediante el "
            "código postal o, si no está disponible, mediante un municipio inequívoco."
        )
        fuente_mapa = st.radio(
            "Datos que quieres mostrar:",
            ["Últimas actualizaciones", "Histórico", "Combinado"],
            horizontal=True,
            key="fuente_mapa",
        )
        if fuente_mapa == "Últimas actualizaciones":
            df_base_mapa = df_radar_filtrado.copy()
        elif fuente_mapa == "Combinado":
            df_base_mapa = pd.concat(
                [df_f, df_radar_filtrado], ignore_index=True, sort=False
            )
            if "id" in df_base_mapa.columns:
                df_base_mapa = df_base_mapa.drop_duplicates(
                    subset=["id"], keep="last"
                )
        else:
            df_base_mapa = df_f.copy()
        if df_base_mapa.empty and not set(["latitud", "longitud"]).issubset(df_base_mapa.columns):
            df_base_mapa = df_f.iloc[0:0].copy()
        df_mapa = df_base_mapa.dropna(subset=['latitud', 'longitud']).copy()
        
        if not df_mapa.empty:
            df_mapa['pbl_fmt'] = df_mapa['pbl_sin_iva'].apply(lambda x: formato_eur(x))
            df_mapa['fecha_limite_fmt'] = df_mapa['fecha_limite'].fillna('No especificada')
            df_mapa['municipio_clean'] = df_mapa['municipio'].fillna('No especificado')
            df_mapa['provincia_clean'] = df_mapa['provincia'].fillna('No especificada')
            df_mapa['ccaa_clean'] = df_mapa['comunidad_autonoma'].fillna('No especificada')
            df_mapa['expediente_clean'] = df_mapa['expediente'].fillna('N/A')
            if "origen_coordenadas" not in df_mapa.columns:
                df_mapa["origen_coordenadas"] = "Histórico"
            df_mapa["origen_coordenadas"] = df_mapa[
                "origen_coordenadas"
            ].fillna("Histórico")
            
            np.random.seed(42)
            df_mapa['lat_j'] = df_mapa['latitud'] + np.random.normal(0, 0.00008, size=len(df_mapa))
            df_mapa['lon_j'] = df_mapa['longitud'] + np.random.normal(0, 0.00008, size=len(df_mapa))

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
        else:
            st.info("No hay licitaciones con coordenadas geográficas disponibles para mostrar en el mapa.")

    elif vista_principal == "🗂️ Histórico":
        st.subheader("🗂️ Histórico de licitaciones")
        st.caption(
            "Base histórica consolidada mediante los conjuntos de datos abiertos de la "
            "Plataforma de Contratación del Sector Público. Se actualiza diariamente."
        )
        col_ord1, col_ord2 = st.columns([2, 3])
        with col_ord1:
            criterio_orden = st.selectbox(
                "🔃 Ordenar tarjetas por:",
                [
                    "Fecha límite (Más cercana)",
                    "Fecha límite (Más lejana)",
                    "Fecha de adjudicación (Más cercana)",
                    "Fecha de adjudicación (Más lejana)",
                    "Presupuesto (Mayor a menor)",
                    "Presupuesto (Menor a mayor)",
                ],
                key="select_orden"
            )
        
        if "Fecha límite (Más cercana)" in criterio_orden:
            df_f = df_f.sort_values(by='fecha_limite_dt', ascending=True, na_position='last')
        elif "Fecha límite (Más lejana)" in criterio_orden:
            df_f = df_f.sort_values(by='fecha_limite_dt', ascending=False, na_position='last')
        elif "Fecha de adjudicación (Más cercana)" in criterio_orden:
            df_f = df_f.assign(
                fecha_adjudicacion_dt=pd.to_datetime(
                    df_f['fecha_adjudicacion'], errors='coerce'
                )
            ).sort_values(by='fecha_adjudicacion_dt', ascending=False, na_position='last')
        elif "Fecha de adjudicación (Más lejana)" in criterio_orden:
            df_f = df_f.assign(
                fecha_adjudicacion_dt=pd.to_datetime(
                    df_f['fecha_adjudicacion'], errors='coerce'
                )
            ).sort_values(by='fecha_adjudicacion_dt', ascending=True, na_position='last')
        elif "Presupuesto (Mayor a menor)" in criterio_orden:
            df_f = df_f.sort_values(by='pbl_sin_iva', ascending=False, na_position='last')
        elif "Presupuesto (Menor a mayor)" in criterio_orden:
            df_f = df_f.sort_values(by='pbl_sin_iva', ascending=True, na_position='last')

        st.write("")

        ITEMS_POR_PAGINA = 12
        total_items = len(df_f)
        total_paginas = max(1, (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA)
        
        if 'pagina_actual' not in st.session_state:
            st.session_state.pagina_actual = 1
            
        if st.session_state.pagina_actual > total_paginas:
            st.session_state.pagina_actual = total_paginas

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
        st.markdown('<div id="tarjetas-inicio" style="height: 1px; margin: 0; padding: 0;" aria-hidden="true">&nbsp;</div>', unsafe_allow_html=True)

        render_grid_tarjetas(df_pagina, "principal")

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

st.markdown(
    '<div class="legal-note"><b>Aviso:</b> LandAI Licitaciones es una herramienta '
    'independiente de consulta y análisis. No pertenece ni representa a la Plataforma de '
    'Contratación del Sector Público. La información oficial y vinculante es la publicada '
    'en dicha plataforma.</div>',
    unsafe_allow_html=True,
)

# Mantiene una navegación vertical predecible tras los reruns de Streamlit.
components.html("""
<script>
(() => {
    const parentWindow = window.parent;
    const parentDocument = parentWindow.document;
    const storageKey = "dashboard_licitaciones_scroll";
    const scrollContainer = hostDocument.querySelector('[data-testid="stMain"]');

    const savedScroll = parentWindow.sessionStorage.getItem(storageKey);

    const restoreScroll = () => {
        if (!scrollContainer || !savedScroll) return;
        const saved = savedScroll;

        if (saved === "tarjetas") {
            const pageIndicator = Array.from(hostDocument.querySelectorAll("p"))
                .find((element) => /^Página \\d+ de \\d+/.test((element.textContent || "").trim()));
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
        hostDocument.querySelectorAll("button").forEach((button) => {
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
