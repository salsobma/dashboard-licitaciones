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

    def numero(ruta: str) -> float | None:
        valor = texto_xml(proyecto, ruta)
        try:
            return float(valor) if valor else None
        except (TypeError, ValueError):
            return None

    codigo_postal = texto_xml(proyecto, "cac:RealizedLocation/cac:Address/cbc:PostalZone")
    municipio = texto_xml(proyecto, "cac:RealizedLocation/cac:Address/cbc:CityName")
    if not codigo_postal:
        codigo_postal = texto_xml(party, "cac:PostalAddress/cbc:PostalZone")
    if not municipio:
        municipio = texto_xml(party, "cac:PostalAddress/cbc:CityName")
    cpvs = [
        nodo.text.strip()
        for nodo in proyecto.findall(
            "cac:RequiredCommodityClassification/cbc:ItemClassificationCode", NAMESPACES
        )
        if nodo.text
    ]
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
        "expediente": texto_xml(status, "cbc:ContractFolderID"),
        "titulo": texto_xml(proyecto, "cbc:Name"),
        "organo_contratante": texto_xml(party, "cac:PartyName/cbc:Name"),
        "tipo_contrato": texto_xml(proyecto, "cbc:TypeCode"),
        "estado": texto_xml(status, "cbc-place-ext:ContractFolderStatusCode"),
        "pbl_sin_iva": numero("cac:BudgetAmount/cbc:TaxExclusiveAmount"),
        "pbl_con_iva": numero("cac:BudgetAmount/cbc:TotalAmount"),
        "valor_estimado": numero("cac:BudgetAmount/cbc:EstimatedOverallContractAmount"),
        "cpv": ",".join(cpvs),
        "codigo_postal": codigo_postal,
        "municipio": municipio,
        "provincia": texto_xml(proyecto, "cac:RealizedLocation/cbc:CountrySubentity"),
        "comunidad_autonoma": None,
        "latitud": None,
        "longitud": None,
        "fecha_limite": fecha_limite,
        "fecha_actualizacion": texto_xml(entrada, "atom:updated"),
        "url_licitacion": enlace.attrib.get("href") if enlace is not None else None,
        "documentos_adjuntos": json.dumps(documentos, ensure_ascii=False),
        "resumen_ia": None,
    }


def bajas_desde_pagina(raiz: ET.Element) -> list[dict[str, str | None]]:
    bajas = []
    for eliminada in raiz.findall("at:deleted-entry", NAMESPACES):
        comentario = " ".join(
            texto.strip() for texto in eliminada.itertext() if texto and texto.strip()
        )
        detalle = f"{eliminada.attrib.get('type', '')} {comentario}".upper()
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
