from __future__ import annotations

import json
import xml.etree.ElementTree as ET


NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "at": "http://purl.org/atompub/tombstones/1.0",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}


def texto_xml(elemento: ET.Element | None, ruta: str) -> str | None:
    if elemento is None:
        return None
    nodo = elemento.find(ruta, NAMESPACES)
    return nodo.text.strip() if nodo is not None and nodo.text else None


def extraer_adjudicacion(status: ET.Element) -> tuple[
    str | None, str | None, float | None, float | None,
    str | None, str | None, str | None,
]:
    nombres = []
    fechas = []
    importes_sin_iva = []
    importes_con_iva = []
    contratos = []
    fechas_formalizacion = []
    fechas_inicio = []
    for resultado in status.findall("cac:TenderResult", NAMESPACES):
        nombre = texto_xml(resultado, "cac:WinningParty/cac:PartyName/cbc:Name")
        if nombre and nombre not in nombres:
            nombres.append(nombre)
        fecha = texto_xml(resultado, "cbc:AwardDate")
        if fecha:
            fechas.append(fecha)
        fecha_inicio = texto_xml(resultado, "cbc:StartDate")
        if fecha_inicio:
            fechas_inicio.append(fecha_inicio)
        for contrato in resultado.findall("cac:Contract", NAMESPACES):
            contrato_id = texto_xml(contrato, "cbc:ID")
            if contrato_id and contrato_id not in contratos:
                contratos.append(contrato_id)
            formalizacion = texto_xml(contrato, "cbc:IssueDate")
            if formalizacion:
                fechas_formalizacion.append(formalizacion)
        for proyecto in resultado.findall("cac:AwardedTenderedProject", NAMESPACES):
            importe_sin_iva = texto_xml(
                proyecto, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"
            )
            if importe_sin_iva:
                try:
                    importes_sin_iva.append(float(importe_sin_iva))
                except ValueError:
                    pass
            importe = texto_xml(proyecto, "cac:LegalMonetaryTotal/cbc:PayableAmount")
            if importe:
                try:
                    importes_con_iva.append(float(importe))
                except ValueError:
                    pass
    return (
        " · ".join(nombres) if nombres else None,
        max(fechas) if fechas else None,
        sum(importes_sin_iva) if importes_sin_iva else None,
        sum(importes_con_iva) if importes_con_iva else None,
        " · ".join(contratos) if contratos else None,
        max(fechas_formalizacion) if fechas_formalizacion else None,
        max(fechas_inicio) if fechas_inicio else None,
    )


def extraer_resultados(status: ET.Element) -> str | None:
    """Conserva todos los resultados oficiales, incluidos los de cada lote."""
    codigos = []
    for resultado in status.findall("cac:TenderResult", NAMESPACES):
        codigo = texto_xml(resultado, "cbc:ResultCode")
        if codigo and codigo not in codigos:
            codigos.append(codigo)
    return ",".join(codigos) if codigos else None


def extraer_resultados_lotes(status: ET.Element) -> str | None:
    """Guarda la correspondencia lote-resultado sin duplicar el expediente."""
    resultados = []
    vistos = set()
    for resultado in status.findall("cac:TenderResult", NAMESPACES):
        codigo = texto_xml(resultado, "cbc:ResultCode")
        if not codigo:
            continue
        lotes = [
            nodo.text.strip()
            for nodo in resultado.findall(".//cbc:ProcurementProjectLotID", NAMESPACES)
            if nodo.text and nodo.text.strip()
        ] or [None]
        for lote in lotes:
            clave = (lote, codigo)
            if clave not in vistos:
                resultados.append({"lote": lote, "codigo": codigo})
                vistos.add(clave)
    return json.dumps(resultados, ensure_ascii=False) if resultados else None


def extraer_cpvs(status: ET.Element, proyecto: ET.Element) -> list[str]:
    """Incluye el CPV general y los CPV declarados dentro de cada lote."""
    nodos = proyecto.findall(
        "cac:RequiredCommodityClassification/cbc:ItemClassificationCode", NAMESPACES
    )
    for lote in status.findall("cac:ProcurementProjectLot", NAMESPACES):
        nodos.extend(lote.findall(
            ".//cac:RequiredCommodityClassification/cbc:ItemClassificationCode",
            NAMESPACES,
        ))
    return sorted({n.text.strip() for n in nodos if n.text and n.text.strip()})


def extraer_ubicacion(status: ET.Element, proyecto: ET.Element, party: ET.Element | None):
    """Prioriza una ubicación valenciana, incluida la declarada a nivel de lote."""
    ubicaciones = list(proyecto.findall("cac:RealizedLocation", NAMESPACES))
    for lote in status.findall("cac:ProcurementProjectLot", NAMESPACES):
        ubicaciones.extend(lote.findall(".//cac:RealizedLocation", NAMESPACES))

    def datos(ubicacion: ET.Element):
        return (
            texto_xml(ubicacion, "cac:Address/cbc:PostalZone"),
            texto_xml(ubicacion, "cac:Address/cbc:CityName"),
            texto_xml(ubicacion, "cbc:CountrySubentity"),
            texto_xml(ubicacion, "cbc:CountrySubentityCode"),
        )

    candidatas = [datos(u) for u in ubicaciones]
    elegida = next((u for u in candidatas if (u[3] or "").upper().startswith("ES52")), None)
    if elegida is None:
        elegida = next((u for u in candidatas if (u[0] or "")[:2] in {"03", "12", "46"}), None)
    if elegida is None:
        elegida = candidatas[0] if candidatas else (None, None, None, None)
    cp, municipio, provincia, nuts = elegida
    if not cp:
        cp = texto_xml(party, "cac:PostalAddress/cbc:PostalZone")
    if not municipio:
        municipio = texto_xml(party, "cac:PostalAddress/cbc:CityName")
    return cp, municipio, provincia, nuts


def extraer_fecha_publicacion(status: ET.Element) -> str | None:
    """Devuelve la fecha del anuncio inicial de licitacion, si esta publicada."""
    fechas_convocatoria = []
    fechas_documento = []
    for aviso in status.iter():
        if aviso.tag.rsplit("}", 1)[-1] != "ValidNoticeInfo":
            continue
        codigos = {
            (nodo.text or "").strip()
            for nodo in aviso.iter()
            if nodo.tag.rsplit("}", 1)[-1] == "NoticeTypeCode"
        }
        fechas = [
            (nodo.text or "").strip()
            for nodo in aviso.iter()
            if nodo.tag.rsplit("}", 1)[-1] == "IssueDate"
            and (nodo.text or "").strip()
        ]
        if "DOC_CN" in codigos:
            fechas_convocatoria.extend(fechas)
        elif "DOC_CD" in codigos:
            fechas_documento.extend(fechas)
    candidatas = fechas_convocatoria or fechas_documento
    return min(candidatas) if candidatas else None


def fila_desde_entrada(entrada: ET.Element) -> dict[str, object] | None:
    lic_id = texto_xml(entrada, "atom:id")
    enlace = entrada.find("atom:link", NAMESPACES)
    status = entrada.find("cac-place-ext:ContractFolderStatus", NAMESPACES)
    if status is None:
        return None
    party = status.find("cac-place-ext:LocatedContractingParty/cac:Party", NAMESPACES)
    proyecto = status.find("cac:ProcurementProject", NAMESPACES)
    if proyecto is None:
        return None
    (
        adjudicatario,
        fecha_adjudicacion,
        importe_adjudicacion_sin_iva,
        importe_adjudicacion_con_iva,
        contrato_id,
        fecha_formalizacion,
        fecha_inicio_contrato,
    ) = extraer_adjudicacion(status)
    expediente = texto_xml(status, "cbc:ContractFolderID")
    estado = texto_xml(status, "cbc-place-ext:ContractFolderStatusCode")
    resultado_codigos = extraer_resultados(status)
    resultados_lotes = extraer_resultados_lotes(status)

    def numero(ruta: str) -> float | None:
        valor = texto_xml(proyecto, ruta)
        try:
            return float(valor) if valor else None
        except (TypeError, ValueError):
            return None

    codigo_postal, municipio, provincia, codigo_nuts = extraer_ubicacion(
        status, proyecto, party
    )
    cpvs = extraer_cpvs(status, proyecto)
    fecha_limite = texto_xml(
        status, "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate"
    )
    hora_limite = texto_xml(
        status, "cac:TenderingProcess/cac:TenderSubmissionDeadlinePeriod/cbc:EndTime"
    )
    if fecha_limite and hora_limite:
        fecha_limite = f"{fecha_limite} {hora_limite}"

    documentos = []
    for tipo_doc, etiqueta in (
        ("PPT", "cac:TechnicalDocumentReference"),
        ("PCAP", "cac:LegalDocumentReference"),
    ):
        doc = status.find(etiqueta, NAMESPACES)
        if doc is not None:
            uri = texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
            if uri:
                documentos.append({
                    "tipo": tipo_doc,
                    "nombre": texto_xml(doc, "cbc:ID") or tipo_doc,
                    "url": uri,
                })
    for doc in status.findall("cac:AdditionalDocumentReference", NAMESPACES):
        uri = texto_xml(doc, "cac:Attachment/cac:ExternalReference/cbc:URI")
        if uri:
            documentos.append({
                "tipo": "ANEXO",
                "nombre": texto_xml(doc, "cbc:ID") or "Anexo adicional",
                "url": uri,
            })

    return {
        "id": lic_id,
        "id_licitacion_corta": lic_id.split("/")[-1] if lic_id else None,
        "expediente": expediente,
        "titulo": texto_xml(proyecto, "cbc:Name"),
        "organo_contratante": texto_xml(party, "cac:PartyName/cbc:Name"),
        "tipo_contrato": texto_xml(proyecto, "cbc:TypeCode"),
        "estado": estado,
        "resultado_codigos": resultado_codigos,
        "resultados_lotes": resultados_lotes,
        "pbl_sin_iva": numero("cac:BudgetAmount/cbc:TaxExclusiveAmount"),
        "pbl_con_iva": numero("cac:BudgetAmount/cbc:TotalAmount"),
        "valor_estimado": numero("cac:BudgetAmount/cbc:EstimatedOverallContractAmount"),
        "cpv": ",".join(cpvs),
        "codigo_postal": codigo_postal,
        "municipio": municipio,
        "provincia": provincia,
        "codigo_nuts": codigo_nuts,
        "comunidad_autonoma": None,
        "latitud": None,
        "longitud": None,
        "fecha_limite": fecha_limite,
        "fecha_publicacion": extraer_fecha_publicacion(status),
        "fecha_actualizacion": texto_xml(entrada, "atom:updated"),
        "adjudicatario": adjudicatario,
        "fecha_adjudicacion": fecha_adjudicacion,
        "importe_adjudicacion_sin_iva": importe_adjudicacion_sin_iva,
        "importe_adjudicacion_con_iva": importe_adjudicacion_con_iva,
        "contrato_id": contrato_id,
        "fecha_formalizacion": fecha_formalizacion,
        "fecha_inicio_contrato": fecha_inicio_contrato,
        "url_licitacion": enlace.attrib.get("href") if enlace is not None else None,
        "documentos_adjuntos": json.dumps(documentos, ensure_ascii=False),
        "resumen_ia": None,
    }


def bajas_desde_pagina(raiz: ET.Element) -> list[dict[str, str | None]]:
    bajas = []
    for eliminada in raiz.findall("at:deleted-entry", NAMESPACES):
        comentario_nodo = eliminada.find("at:comment", NAMESPACES)
        comentario = " ".join(
            texto.strip() for texto in eliminada.itertext() if texto and texto.strip()
        )
        tipo = (
            comentario_nodo.attrib.get("type", "")
            if comentario_nodo is not None
            else eliminada.attrib.get("type", "")
        )
        detalle = f"{tipo} {comentario}".upper()
        bajas.append({
            "id": eliminada.attrib.get("ref"),
            "fecha_actualizacion": eliminada.attrib.get("when"),
            "estado": "ANUL" if "ANUL" in detalle else "CERR",
        })
    return bajas


def procesar_paginas(raices: list[ET.Element]) -> tuple[list[dict[str, object]], list[dict[str, str | None]]]:
    filas = []
    bajas = []
    for raiz in raices:
        for entrada in raiz.findall("atom:entry", NAMESPACES):
            fila = fila_desde_entrada(entrada)
            if fila:
                filas.append(fila)
        bajas.extend(bajas_desde_pagina(raiz))
    return filas, bajas
