import sqlite3
import json
import os
import time
import re
from google import genai
from google.genai import types

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "TU_API_KEY_AQUI")

PROMPT_ANALISIS = """
Eres un ingeniero perito especializado en contratación pública en España.
Tu trabajo es analizar la información técnica y administrativa de una licitación pública para sintetizar los requisitos esenciales que necesita saber el licitador.

Extrae y resume la información en los siguientes puntos clave. Devuelve ÚNICAMENTE un objeto JSON válido con la siguiente estructura:
{
  "alcance_tecnico": "<Resumen conciso del objeto del contrato y los trabajos a realizar>",
  "criterios_puntuacion": "<Síntesis de criterios: % Precio vs % Memoria/Juicio de valor y otras mejoras>",
  "solvencia_requerida": "<Requisitos de solvencia técnica (obras/servicios similares) y económica (facturación anual o clasificación)>",
  "seguro_rc": "<Detalle si se exige Seguro de Responsabilidad Civil y su importe mínimo, o 'No especificado / Según pliegos'>",
  "garantia": "<Detalle sobre garantía definitiva (ej. 5%) o provisional si la hubiera>",
  "condicionantes_destacados": "<Plazo de ejecución, penalizaciones o aspectos de especial atención para la oferta>"
}
"""

def analizar_licitacion(licitacion):
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    contenido_input = f"""
    Título: {licitacion['titulo']}
    Órgano de Contratación: {licitacion['organo_contratante']}
    Tipo de Contrato: {licitacion['tipo_contrato']}
    CPV: {licitacion['cpv']}
    PBL sin IVA: {licitacion['pbl_sin_iva']} €
    PBL con IVA: {licitacion['pbl_con_iva']} €
    Ubicación: {licitacion['municipio']}, {licitacion['provincia']}
    Documentos/Pliegos disponibles: {licitacion['documentos_adjuntos']}
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=f"{PROMPT_ANALISIS}\n\nDatos de la Licitación:\n{contenido_input}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if not response or not response.text:
            return None

        # Limpieza robusta de marcas de código markdown si las hubiera
        texto_limpio = response.text.strip()
        if texto_limpio.startswith("```json"):
            texto_limpio = texto_limpio[7:]
        if texto_limpio.endswith("```"):
            texto_limpio = texto_limpio[:-3]
        texto_limpio = texto_limpio.strip()

        # Corrección de errores comunes de sintaxis en JSON (comas finales colgadas)
        texto_limpio = re.sub(r',\s*([\]}])', r'\1', texto_limpio)

        try:
            return json.loads(texto_limpio)
        except json.JSONDecodeError as json_err:
            # Respaldo de emergencia estructurado si el JSON crudo del pliego falla
            print(f"⚠️ Aviso de sintaxis JSON en ID {licitacion['id']}: {json_err}. Aplicando respaldo de texto.")
            return {
                "alcance_tecnico": response.text,
                "criterios_puntuacion": "No se pudo estructurar automáticamente debido a caracteres en el pliego.",
                "solvencia_requerida": "Ver pliego original.",
                "seguro_rc": "No especificado",
                "garantia": "No especificado",
                "condicionantes_destacados": "No especificado"
            }

    except Exception as e:
        print(f"Error analizando ID {licitacion['id']}: {e}")
        return None

def ejecutar_analisis_pendientes(limite=15):
    conn = sqlite3.connect("licitaciones.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(licitaciones)")
    columnas = [column[1] for column in cursor.fetchall()]
    
    if 'analizado_ia' not in columnas:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN analizado_ia INTEGER DEFAULT 0")
    if 'resumen_ia' not in columnas:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN resumen_ia TEXT")
    conn.commit()

    cursor.execute("SELECT * FROM licitaciones WHERE analizado_ia IS NULL OR analizado_ia = 0 LIMIT ?", (limite,))
    pendientes = cursor.fetchall()
    
    print(f"🧠 Analizando {len(pendientes)} licitaciones con Gemini...")
    
    for lic in pendientes:
        res = analizar_licitacion(lic)
        if res:
            resumen_json = json.dumps(res, ensure_ascii=False)
            cursor.execute("""
                UPDATE licitaciones 
                SET analizado_ia = 1, resumen_ia = ?
                WHERE id = ?
            """, (resumen_json, lic['id']))
            conn.commit()
            print(f"✅ Analizada correctamente: {lic['titulo'][:50]}...")
        
        # Pausa de 4 segundos para respetar los límites de peticiones de la API[cite: 2]
        time.sleep(4)
            
    conn.close()

if __name__ == "__main__":
    ejecutar_analisis_pendientes()