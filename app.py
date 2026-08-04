import html
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
import unicodedata
from datetime import date
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from google import genai
from google.genai import types

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
    'ADJ': ('Adjudicada', 'badge-adj'),
    'RES': ('Resuelta / Formalizada', 'badge-res'),
    'ANUL': ('Anulada', 'badge-res')
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

FEED_RECIENTE_URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)

NAMESPACES_ATOM = {
    "atom": "http://www.w3.org/2005/Atom",
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

@st.cache_data(ttl=900, show_spinner="Consultando las últimas actualizaciones oficiales…")
def cargar_feed_reciente():
    respuesta = requests.get(
        FEED_RECIENTE_URL,
        headers={"User-Agent": "LandAI-Licitaciones/1.0"},
        timeout=45,
    )
    respuesta.raise_for_status()
    raiz = ET.fromstring(respuesta.content)
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
        for doc in status.findall("cac:AdditionalDocu…14528 tokens truncated…_publicacion")
        )
        licitaciones_diarias = (
            datos_publicacion_diaria.groupby("fecha_publicacion", as_index=False)
            .size()
            .rename(columns={"size": "licitaciones"})
            .sort_values("fecha_publicacion")
        )

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

        fechas_limite_grafico = pd.to_datetime(df_graficos["fecha_limite_dt"], errors="coerce", utc=True).dt.tz_convert(None)
        dias_restantes = (fechas_limite_grafico.dt.normalize() - pd.Timestamp.today().normalize()).dt.days
        orden_vencimientos = ["Finalizan hoy", "Próximos 3 días", "Entre 4 y 7 días", "Más de 7 días"]
        df_vencimientos = pd.DataFrame({"dias": dias_restantes}).dropna()
        df_vencimientos = df_vencimientos[df_vencimientos["dias"] >= 0]
        df_vencimientos["plazo"] = np.select(
            [
                df_vencimientos["dias"] == 0,
                df_vencimientos["dias"].between(1, 3),
                df_vencimientos["dias"].between(4, 7)
            ],
            orden_vencimientos[:3],
            default=orden_vencimientos[3]
        )
        por_vencimiento = (
            df_vencimientos.groupby("plazo")
            .size()
            .reindex(orden_vencimientos, fill_value=0)
            .rename("licitaciones")
            .reset_index()
        )

        grafico_provincias = (
            alt.Chart(por_provincia)
            .mark_bar(color=color_azul, cornerRadiusEnd=4)
            .encode(
                x=alt.X("licitaciones:Q", title="Número de licitaciones"),
                y=alt.Y("provincia_grafico:N", title=None, sort="-x"),
                tooltip=[
                    alt.Tooltip("provincia_grafico:N", title="Provincia"),
                    alt.Tooltip("licitaciones:Q", title="Licitaciones")
                ]
            )
            .properties(height=300)
        )
        grafico_presupuesto_provincia = (
            alt.Chart(presupuesto_provincia)
            .mark_bar(color=color_verde, cornerRadiusEnd=4)
            .encode(
                x=alt.X("pbl_sin_iva:Q", title="Presupuesto total sin IVA (€)"),
                y=alt.Y("provincia_grafico:N", title=None, sort="-x"),
                tooltip=[
                    alt.Tooltip("provincia_grafico:N", title="Provincia"),
                    alt.Tooltip("pbl_sin_iva:Q", title="Presupuesto", format=",.2f")
                ]
            )
            .properties(height=300)
        )
        grafico_tramos = (
            alt.Chart(por_tramo)
            .mark_bar(color=color_naranja, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("tramo_presupuesto:N", title=None, sort=tramos_presupuesto, axis=alt.Axis(labelAngle=-25)),
                y=alt.Y("licitaciones:Q", title="Número de licitaciones"),
                tooltip=[
                    alt.Tooltip("tramo_presupuesto:N", title="Tramo"),
                    alt.Tooltip("licitaciones:Q", title="Licitaciones")
                ]
            )
            .properties(height=300)
        )
        grafico_vencimientos = (
            alt.Chart(por_vencimiento)
            .mark_bar(color=color_rojo, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("plazo:N", title=None, sort=orden_vencimientos, axis=alt.Axis(labelAngle=-20)),
                y=alt.Y("licitaciones:Q", title="Número de licitaciones"),
                tooltip=[
                    alt.Tooltip("plazo:N", title="Vencimiento"),
                    alt.Tooltip("licitaciones:Q", title="Licitaciones")
                ]
            )
            .properties(height=300)
        )

        grafico_comunidades = (
            alt.Chart(por_comunidad)
            .mark_bar(color="#7c3aed", cornerRadiusEnd=4)
            .encode(
                x=alt.X("licitaciones:Q", title="Número de licitaciones"),
                y=alt.Y("comunidad_grafico:N", title=None, sort="-x"),
                tooltip=[
                    alt.Tooltip("comunidad_grafico:N", title="Comunidad autónoma"),
                    alt.Tooltip("licitaciones:Q", title="Licitaciones")
                ]
            )
            .properties(height=420)
        )
        grafico_presupuesto_organo = (
            alt.Chart(presupuesto_organo)
            .mark_bar(color="#0891b2", cornerRadiusEnd=4)
            .encode(
                x=alt.X("pbl_sin_iva:Q", title="Presupuesto total sin IVA (€)"),
                y=alt.Y("organo_grafico:N", title=None, sort="-x", axis=alt.Axis(labelLimit=260)),
                tooltip=[
                    alt.Tooltip("organo_grafico:N", title="Órgano de contratación"),
                    alt.Tooltip("pbl_sin_iva:Q", title="Presupuesto", format=",.2f")
                ]
            )
            .properties(height=420)
        )
        grafico_licitaciones_diarias = (
            alt.Chart(licitaciones_diarias)
            .mark_line(color=color_naranja, point=alt.OverlayMarkDef(color=color_naranja, size=45), strokeWidth=3)
            .encode(
                x=alt.X("fecha_publicacion:T", title="Fecha de publicación", axis=alt.Axis(format="%d/%m/%Y")),
                y=alt.Y("licitaciones:Q", title="Número de licitaciones"),
                tooltip=[
                    alt.Tooltip("fecha_publicacion:T", title="Fecha", format="%d/%m/%Y"),
                    alt.Tooltip("licitaciones:Q", title="Licitaciones anunciadas")
                ]
            )
            .properties(height=340)
        )
        grafico_presupuesto_diario = (
            alt.Chart(presupuesto_diario)
            .mark_line(color=color_azul, point=alt.OverlayMarkDef(color=color_azul, size=45), strokeWidth=3)
            .encode(
                x=alt.X("fecha_publicacion:T", title="Fecha de publicación / actualización", axis=alt.Axis(format="%d/%m/%Y")),
                y=alt.Y("pbl_sin_iva:Q", title="PBL total sin IVA (€)"),
                tooltip=[
                    alt.Tooltip("fecha_publicacion:T", title="Fecha", format="%d/%m/%Y"),
                    alt.Tooltip("pbl_sin_iva:Q", title="Presupuesto total", format=",.2f")
                ]
            )
            .properties(height=360)
        )

        grafico_fila_1a, grafico_fila_1b = st.columns(2)
        with grafico_fila_1a:
            with st.container(border=True):
                st.markdown("#### Licitaciones por provincia")
                st.altair_chart(grafico_provincias, use_container_width=True)
        with grafico_fila_1b:
            with st.container(border=True):
                st.markdown("#### Presupuesto por provincia")
                st.altair_chart(grafico_presupuesto_provincia, use_container_width=True)

        grafico_fila_2a, grafico_fila_2b = st.columns(2)
        with grafico_fila_2a:
            with st.container(border=True):
                st.markdown("#### Distribución por presupuesto")
                st.altair_chart(grafico_tramos, use_container_width=True)
        with grafico_fila_2b:
            with st.container(border=True):
                st.markdown("#### Próximos vencimientos")
                st.altair_chart(grafico_vencimientos, use_container_width=True)

        grafico_fila_3a, grafico_fila_3b = st.columns(2)
        with grafico_fila_3a:
            with st.container(border=True):
                st.markdown("#### Licitaciones por comunidad autónoma")
                st.altair_chart(grafico_comunidades, use_container_width=True)
        with grafico_fila_3b:
            with st.container(border=True):
                st.markdown("#### Presupuesto por órgano de contratación")
                st.altair_chart(grafico_presupuesto_organo, use_container_width=True)

        with st.container(border=True):
            st.markdown("#### Licitaciones anunciadas por día")
            st.caption(f"Número de licitaciones agrupadas según {descripcion_fecha_publicacion}.")
            st.altair_chart(grafico_licitaciones_diarias, use_container_width=True)

        with st.container(border=True):
            st.markdown("#### Presupuesto publicado por día")
            st.caption(f"Suma diaria del presupuesto base de licitación sin IVA según {descripcion_fecha_publicacion}.")
            st.altair_chart(grafico_presupuesto_diario, use_container_width=True)

    elif vista_principal == "🗺️ Mapa":
        st.subheader("📍 Ubicación de las licitaciones")
        st.caption(
            "Las ubicaciones del feed son aproximadas: se calculan mediante el "
            "código postal o, si no está disponible, mediante un municipio inequívoco."
        )
        fuente_mapa = st.radio(
            "Datos que quieres mostrar:",
            ["Histórico", "Últimas actualizaciones", "Combinado"],
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
                        "🧭 <b>Origen de ubicación:</b> {origen_coordenadas}<br/>"
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

