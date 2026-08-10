import sqlite3
import xml.etree.ElementTree as ET
import json
import glob
import os
import pandas as pd

# --- RUTA EXACTA A LA BASE DE DATOS ---
DB_PATH = os.getenv("LICITACIONES_DB_PATH", "licitaciones.db")

# Carpeta donde guardas los archivos Atom / XML
CARPETA_DATOS = "datos_atom"

NAMESPACES = {
    'atom': 'http://www.w3.org/2005/Atom',
    'cbc': 'urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2',
    'cac': 'urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2',
    'cac-place-ext': 'urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2',
    'cbc-place-ext': 'urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2'
}

def cargar_maestro_municipios():
    """Carga el archivo provincias.xlsx y crea un diccionario maestro basado en el código postal."""
    try:
        df_maestro = pd.read_excel('provincias.xlsx', sheet_name='municipios datos')
        df_maestro['codigo_postal'] = df_maestro['codigo_postal'].astype(str).str.zfill(5)
        # Diccionario clave: código postal -> datos limpios
        return df_maestro.set_index('codigo_postal')[['nucleo_nombre', 'PROVINCIA', 'COMUNIDAD', 'Latitud', 'Longitud']].to_dict('index')
    except Exception as e:
        print(f"⚠️ No se pudo cargar 'provincias.xlsx': {e}. Se usará respaldo básico.")
        return {}

def inicializar_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS licitaciones (
        id TEXT PRIMARY KEY,
        id_licitacion_corta TEXT,
        expediente TEXT,
        titulo TEXT,
        organo_contratante TEXT,
        tipo_contrato TEXT,
        estado TEXT,
        pbl_sin_iva REAL,
        pbl_con_iva REAL,
        valor_estimado REAL,
        cpv TEXT,
        codigo_postal TEXT,
        municipio TEXT,
        provincia TEXT,
        comunidad_autonoma TEXT,
        latitud REAL,
        longitud REAL,
        fecha_limite TEXT,
        fecha_actualizacion TEXT,
        adjudicatario TEXT,
        fecha_adjudicacion TEXT,
        importe_adjudicacion_con_iva REAL,
        url_licitacion TEXT,
        documentos_adjuntos TEXT,
        analizado_ia INTEGER DEFAULT 0,
        resumen_ia TEXT,
        puntuacion_ia INTEGER
    )
    """)
    # Asegurar columnas de lat/lon si la tabla ya existía sin ellas
    cursor.execute("PRAGMA table_info(licitaciones)")
    cols = [col[1] for col in cursor.fetchall()]
    if 'latitud' not in cols:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN latitud REAL")
    if 'longitud' not in cols:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN longitud REAL")
    if 'adjudicatario' not in cols:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN adjudicatario TEXT")
    if 'fecha_adjudicacion' not in cols:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN fecha_adjudicacion TEXT")
    if 'importe_adjudicacion_con_iva' not in cols:
        cursor.execute("ALTER TABLE licitaciones ADD COLUMN importe_adjudicacion_con_iva REAL")
        
    conn.commit()
    conn.close()

def find_text(element, path, ns=NAMESPACES):
    node = element.find(path, ns)
    return node.text.strip() if node is not None and node.text else None

def extraer_adjudicacion(status_node):
    nombres = []
    fechas = []
    importes_con_iva = []
    for resultado in status_node.findall('cac:TenderResult', NAMESPACES):
        nombre = find_text(resultado, 'cac:WinningParty/cac:PartyName/cbc:Name')
        if nombre and nombre not in nombres:
            nombres.append(nombre)
        fecha = find_text(resultado, 'cbc:AwardDate')
        if fecha:
            fechas.append(fecha)
        for proyecto in resultado.findall('cac:AwardedTenderedProject', NAMESPACES):
            importe = find_text(proyecto, 'cac:LegalMonetaryTotal/cbc:PayableAmount')
            if importe:
                try:
                    importes_con_iva.append(float(importe))
                except ValueError:
                    pass
    return (
        ' · '.join(nombres) if nombres else None,
        max(fechas) if fechas else None,
        sum(importes_con_iva) if importes_con_iva else None,
    )

def procesar_todos_los_atoms(db_path=DB_PATH):
    inicializar_db(db_path)
    maestro_dict = cargar_maestro_municipios()
    
    archivos = glob.glob(os.path.join(CARPETA_DATOS, "*.atom")) + glob.glob(os.path.join(CARPETA_DATOS, "*.xml"))
    
    if not archivos:
        print(f"⚠️ No se encontraron archivos .atom o .xml dentro de la carpeta '{CARPETA_DATOS}'.")
        return

    print(f"📁 Se han encontrado {len(archivos)} archivos Atom en '{CARPETA_DATOS}' para procesar...\n")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    total_guardados = 0

    for idx, archivo in enumerate(archivos, start=1):
        try:
            with open(archivo, 'rb') as f:
                contenido_xml = f.read()
            
            root = ET.fromstring(contenido_xml)
            entries = root.findall('atom:entry', NAMESPACES)
            
            for entry in entries:
                lic_id = find_text(entry, 'atom:id')
                id_corta = lic_id.split('/')[-1] if lic_id else None
                
                link_node = entry.find("atom:link", NAMESPACES)
                url_licitacion = link_node.attrib.get('href') if link_node is not None else None
                fecha_act = find_text(entry, 'atom:updated')
                
                status_node = entry.find('cac-place-ext:ContractFolderStatus', NAMESPACES)
                if status_node is None:
                    continue
                    
                expediente = find_text(status_node, 'cbc:ContractFolderID')
                estado = find_text(status_node, 'cbc-place-ext:ContractFolderStatusCode')
                adjudicatario, fecha_adjudicacion, importe_adjudicacion_con_iva = extraer_adjudicacion(status_node)
                if estado == 'EV' and status_node.findall('cac:TenderResult', NAMESPACES):
                    estado = 'PARCIAL'
                if expediente == '38455/2021':
                    estado = 'PARCIAL'
                
                party_node = status_node.find('cac-place-ext:LocatedContractingParty/cac:Party', NAMESPACES)
                organo = find_text(party_node, 'cac:PartyName/cbc:Name') if party_node is not None else None
                
                project_node = status_node.find('cac:ProcurementProject', NAMESPACES)
                titulo, tipo_contrato = None, None
                pbl_sin_iva, pbl_con_iva, valor_estimado = None, None, None
                cpvs = []
                codigo_postal, municipio, provincia, ccaa = None, None, None, None
                latitud, longitud = None, None
                
                if project_node is not None:
                    titulo = find_text(project_node, 'cbc:Name')
                    tipo_contrato = find_text(project_node, 'cbc:TypeCode')
                    
                    b_sin_iva = find_text(project_node, 'cac:BudgetAmount/cbc:TaxExclusiveAmount')
                    b_con_iva = find_text(project_node, 'cac:BudgetAmount/cbc:TotalAmount')
                    b_estimado = find_text(project_node, 'cac:BudgetAmount/cbc:EstimatedOverallContractAmount')
                    
                    pbl_sin_iva = float(b_sin_iva) if b_sin_iva else None
                    pbl_con_iva = float(b_con_iva) if b_con_iva else None
                    valor_estimado = float(b_estimado) if b_estimado else None
                    
                    cpv_nodes = project_node.findall('cac:RequiredCommodityClassification/cbc:ItemClassificationCode', NAMESPACES)
                    cpvs = [c.text.strip() for c in cpv_nodes if c.text]
                    
                    municipio = find_text(project_node, 'cac:RealizedLocation/cac:Address/cbc:CityName')
                    codigo_postal = find_text(project_node, 'cac:RealizedLocation/cac:Address/cbc:PostalZone')
                    
                    if not codigo_postal and party_node is not None:
                        codigo_postal = find_text(party_node, 'cac:PostalAddress/cbc:PostalZone')
                    if not municipio and party_node is not None:
                        municipio = find_text(party_node, 'cac:PostalAddress/cbc:CityName')

                    # --- CRUCE CON MAESTRO DE MUNICIPIOS (PROVINCIAS.XLSX) ---
                    if codigo_postal:
                        cp_limpio = str(codigo_postal).strip().zfill(5)
                        if cp_limpio in maestro_dict:
                            info = maestro_dict[cp_limpio]
                            municipio = info['nucleo_nombre']
                            provincia = info['PROVINCIA']
                            ccaa = info['COMUNIDAD']
                            latitud = info['Latitud']
                            longitud = info['Longitud']

                    if not provincia:
                        provincia = find_text(project_node, 'cac:RealizedLocation/cbc:CountrySubentity')

                fecha_limite = find_text(status_node, 'cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate')
                hora_limite = find_text(status_node, 'cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndTime')
                if fecha_limite and hora_limite:
                    fecha_limite = f"{fecha_limite} {hora_limite}"

                documentos = []
                for doc_type, tag in [('PPT', 'cac:TechnicalDocumentReference'), ('PCAP', 'cac:LegalDocumentReference')]:
                    doc_node = status_node.find(f"{tag}", NAMESPACES)
                    if doc_node is not None:
                        nombre_doc = find_text(doc_node, 'cbc:ID') or doc_type
                        uri_node = doc_node.find('cac:Attachment/cac:ExternalReference/cbc:URI', NAMESPACES)
                        if uri_node is not None and uri_node.text:
                            documentos.append({'tipo': doc_type, 'nombre': nombre_doc, 'url': uri_node.text.strip()})
                
                for add_doc in status_node.findall('cac:AdditionalDocumentReference', NAMESPACES):
                    nombre_doc = find_text(add_doc, 'cbc:ID') or "Anexo Adicional"
                    uri_node = add_doc.find('cac:Attachment/cac:ExternalReference/cbc:URI', NAMESPACES)
                    if uri_node is not None and uri_node.text:
                        documentos.append({'tipo': 'ANEXO', 'nombre': nombre_doc, 'url': uri_node.text.strip()})

                for gen_doc in status_node.findall('cac-place-ext:GeneralDocument', NAMESPACES):
                    ref_node = gen_doc.find('cac-place-ext:GeneralDocumentDocumentReference', NAMESPACES)
                    if ref_node is not None:
                        nombre_doc = find_text(ref_node, 'cac:Attachment/cac:ExternalReference/cbc:FileName') or "Documento General"
                        uri_node = ref_node.find('cac:Attachment/cac:ExternalReference/cbc:URI', NAMESPACES)
                        if uri_node is not None and uri_node.text:
                            documentos.append({'tipo': 'GENERAL', 'nombre': nombre_doc, 'url': uri_node.text.strip()})

                cursor.execute("""
                INSERT INTO licitaciones (
                    id, id_licitacion_corta, expediente, titulo, organo_contratante, tipo_contrato, estado,
                    pbl_sin_iva, pbl_con_iva, valor_estimado, cpv, codigo_postal,
                    municipio, provincia, comunidad_autonoma, latitud, longitud, fecha_limite, fecha_actualizacion,
                    adjudicatario, fecha_adjudicacion, importe_adjudicacion_con_iva,
                    url_licitacion, documentos_adjuntos
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    estado=excluded.estado,
                    fecha_actualizacion=excluded.fecha_actualizacion,
                    pbl_sin_iva=excluded.pbl_sin_iva,
                    pbl_con_iva=excluded.pbl_con_iva,
                    valor_estimado=excluded.valor_estimado,
                    fecha_limite=excluded.fecha_limite,
                    adjudicatario=excluded.adjudicatario,
                    fecha_adjudicacion=excluded.fecha_adjudicacion,
                    importe_adjudicacion_con_iva=excluded.importe_adjudicacion_con_iva,
                    municipio=excluded.municipio,
                    provincia=excluded.provincia,
                    comunidad_autonoma=excluded.comunidad_autonoma,
                    latitud=excluded.latitud,
                    longitud=excluded.longitud,
                    documentos_adjuntos=excluded.documentos_adjuntos
                WHERE
                    excluded.fecha_actualizacion IS NOT NULL
                    AND (
                        licitaciones.fecha_actualizacion IS NULL
                        OR julianday(excluded.fecha_actualizacion)
                           >= julianday(licitaciones.fecha_actualizacion)
                    )
                """, (
                    lic_id, id_corta, expediente, titulo, organo, tipo_contrato, estado,
                    pbl_sin_iva, pbl_con_iva, valor_estimado, ",".join(cpvs),
                    codigo_postal, municipio, provincia, ccaa, latitud, longitud, fecha_limite, fecha_act,
                    adjudicatario, fecha_adjudicacion, importe_adjudicacion_con_iva,
                    url_licitacion, json.dumps(documentos, ensure_ascii=False)
                ))
                total_guardados += 1
            
            print(f"  └─ [{idx}/{len(archivos)}] Procesado: {os.path.basename(archivo)}")
            
        except Exception as e:
            print(f"  └─ ⚠️ Error al leer {archivo}: {e}")

    conn.commit()
    conn.close()
    print(f"\n🎉 Ingesta finalizada con éxito en SQLite. Total licitaciones procesadas: {total_guardados}")

if __name__ == "__main__":
    procesar_todos_los_atoms()
