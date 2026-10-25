from __future__ import annotations

import json
import re
import unicodedata
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


def extraer_adjudicatarios(status: ET.Element) -> str | None:
    """Conserva identidad fiscal, nombre y lote de cada adjudicatario publicado."""
    adjudicatarios = []
    vistos = set()
    for resultado in status.findall("cac:TenderResult", NAMESPACES):
        codigo_resultado = texto_xml(resultado, "cbc:ResultCode")
        lotes = [
            nodo.text.strip()
            for nodo in resultado.findall(
                ".//cbc:ProcurementProjectLotID", NAMESPACES
            )
            if nodo.text and nodo.text.strip()
        ] or [None]
        for parte in resultado.findall("cac:WinningParty", NAMESPACES):
            nombre = texto_xml(parte, "cac:PartyName/cbc:Name")
            identificador_nodo = parte.find(
                "cac:PartyIdentification/cbc:ID", NAMESPACES
            )
            identificador = (
                identificador_nodo.text.strip()
                if identificador_nodo is not None and identificador_nodo.text
                else None
            )
            tipo = (
                identificador_nodo.attrib.get("schemeName")
                if identificador_nodo is not None
                else None
            )
            if not nombre and not identificador:
                continue
            for lote in lotes:
                clave = (identificador, tipo, nombre, lote, codigo_resultado)
                if clave in vistos:
                    continue
                vistos.add(clave)
                adjudicatarios.append({
                    "identificador": identificador,
                    "tipo_identificador": tipo,
                    "nombre": nombre,
                    "lote": lote,
                    "resultado": codigo_resultado,
                })
    return (
        json.dumps(adjudicatarios, ensure_ascii=False)
        if adjudicatarios else None
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


def _clave_texto(valor: str | None) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", texto))


def _municipio_sede(valor: str | None) -> str | None:
    municipio = re.sub(r"\s*\([^)]*\)\s*$", "", str(valor or "")).strip()
    return municipio or None


def _provincia_por_cp(cp: str | None) -> str | None:
    return {"03": "ES521", "12": "ES522", "46": "ES523"}.get(str(cp or "")[:2])


def extraer_ubicacion_detallada(
    status: ET.Element,
    proyecto: ET.Element,
    party: ET.Element | None,
) -> dict[str, str | None]:
    """Elige la ubicación más precisa y documenta cualquier inferencia."""
    ubicaciones = [("xml_proyecto", u) for u in proyecto.findall("cac:RealizedLocation", NAMESPACES)]
    for lote in status.findall("cac:ProcurementProjectLot", NAMESPACES):
        ubicaciones.extend(
            ("xml_lote", u)
            for u in lote.findall(".//cac:RealizedLocation", NAMESPACES)
        )

    def datos(ubicacion: ET.Element):
        return (
            texto_xml(ubicacion, "cac:Address/cbc:PostalZone"),
            texto_xml(ubicacion, "cac:Address/cbc:CityName"),
            texto_xml(ubicacion, "cac:Address/cbc:CountrySubentity")
            or texto_xml(ubicacion, "cbc:CountrySubentity"),
            texto_xml(ubicacion, "cbc:CountrySubentityCode")
            or texto_xml(ubicacion, "cac:Address/cbc:CountrySubentityCode"),
        )

    candidatas = [(fuente, *datos(u)) for fuente, u in ubicaciones]

    def es_valenciana(candidata):
        _, cp, _, provincia, nuts = candidata
        clave_provincia = _clave_texto(provincia)
        return (
            str(nuts or "").upper().startswith("ES52")
            or str(cp or "")[:2] in {"03", "12", "46"}
            or any(x in clave_provincia for x in ("alicante", "alacant", "castellon", "castello", "valencia"))
        )

    def puntuacion(candidata):
        fuente, cp, municipio, provincia, nuts = candidata
        # La precisión manda sobre un NUTS meramente provincial. En igualdad,
        # la ubicación general prevalece sobre la repetida en un lote.
        return (
            12 * bool(municipio)
            + 8 * bool(cp)
            + 2 * bool(provincia)
            + bool(nuts)
            + (2 if fuente == "xml_proyecto" else 1)
        )

    validas = [c for c in candidatas if es_valenciana(c)]
    elegida = max(validas or candidatas, key=puntuacion) if candidatas else None
    if elegida:
        fuente, cp, municipio, provincia, nuts = elegida
    else:
        fuente, cp, municipio, provincia, nuts = (None, None, None, None, None)

    municipio_organo = _municipio_sede(
        texto_xml(party, "cac:PostalAddress/cbc:CityName")
    )
    cp_organo = texto_xml(party, "cac:PostalAddress/cbc:PostalZone")
    fuente_municipio = fuente if municipio else None
    confianza = "oficial" if municipio else None

    # La dirección de la entidad contratante no es el lugar de ejecución. Solo se
    # usa como último recurso cuando el XML no declara ninguna ubicación de obra.
    if not candidatas:
        cp = cp_organo
        municipio = municipio_organo
        provincia = texto_xml(party, "cac:PostalAddress/cbc:CountrySubentity")
        fuente_municipio = "sede_organo_sin_ubicacion_ejecucion" if municipio else None
        confianza = "media" if municipio else None
    elif not municipio and municipio_organo and cp_organo:
        nuts_provincia = str(nuts or "").upper()[:5]
        cp_provincia = _provincia_por_cp(cp_organo)
        organo = _clave_texto(texto_xml(party, "cac:PartyName/cbc:Name"))
        es_organo_municipal = any(
            etiqueta in organo
            for etiqueta in (
                "ayuntamiento", "ajuntament", "alcaldia", "alcalde",
                "pleno", "junta de gobierno local", "concejal",
            )
        )
        titulo = _clave_texto(texto_xml(proyecto, "cbc:Name"))
        municipio_clave = _clave_texto(municipio_organo)
        citado_en_titulo = bool(
            municipio_clave
            and re.search(rf"(?<![a-z0-9]){re.escape(municipio_clave)}(?![a-z0-9])", titulo)
        )
        provincia_coherente = bool(
            cp_provincia and (not nuts_provincia.startswith("ES52") or cp_provincia == nuts_provincia)
        )
        if provincia_coherente and (citado_en_titulo or es_organo_municipal):
            cp = cp_organo
            municipio = municipio_organo
            fuente_municipio = (
                "titulo_y_sede_organo" if citado_en_titulo else "sede_organo_municipal"
            )
            confianza = "alta" if citado_en_titulo else "media"

    titulo_clave = _clave_texto(texto_xml(proyecto, "cbc:Name"))
    municipios_xml = {
        _clave_texto(candidata[2])
        for candidata in candidatas
        if candidata[2]
    }
    if municipio:
        ambito_ubicacion = "municipio_concreto"
    elif len(municipios_xml) > 1 or any(
        frase in titulo_clave
        for frase in (
            "varios municipios", "diversos municipios",
            "distintos municipios", "multiples municipios",
        )
    ):
        ambito_ubicacion = "varios_municipios"
    elif any(
        frase in titulo_clave
        for frase in (
            "ambito provincial", "red provincial",
            "carreteras provinciales", "provincia de alicante",
            "provincia de valencia", "provincia de castellon",
        )
    ):
        ambito_ubicacion = "ambito_provincial"
    else:
        ambito_ubicacion = "municipio_no_publicado"

    return {
        "codigo_postal": cp,
        "municipio": municipio,
        "provincia": provincia,
        "codigo_nuts": nuts,
        "municipio_organo": municipio_organo,
        "codigo_postal_organo": cp_organo,
        "municipio_fuente": fuente_municipio,
        "municipio_confianza": confianza,
        "ambito_ubicacion": ambito_ubicacion,
    }


def extraer_ubicacion(status: ET.Element, proyecto: ET.Element, party: ET.Element | None):
    ubicacion = extraer_ubicacion_detallada(status, proyecto, party)
    return tuple(
        ubicacion[campo]
        for campo in ("codigo_postal", "municipio", "provincia", "codigo_nuts")
    )


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


def extraer_fecha_resolucion(status: ET.Element) -> str | None:
    """Fecha oficial del anuncio que resuelve o termina el procedimiento."""
    tipos_resolucion = {
        "DOC_CAN_ADJ", "DOC_FORM", "DOC_DESC", "DESISTIMIENTO", "RENUNCIA"
    }
    fechas = []
    for aviso in status.iter():
        if aviso.tag.rsplit("}", 1)[-1] != "ValidNoticeInfo":
            continue
        codigos = {
            (nodo.text or "").strip()
            for nodo in aviso.iter()
            if nodo.tag.rsplit("}", 1)[-1] == "NoticeTypeCode"
        }
        if not codigos.intersection(tipos_resolucion):
            continue
        fechas.extend(
            (nodo.text or "").strip()
            for nodo in aviso.iter()
            if nodo.tag.rsplit("}", 1)[-1] in {"IssueDate", "NoticeIssueDate"}
            and (nodo.text or "").strip()
        )
    return max(fechas) if fechas else None


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

    ubicacion = extraer_ubicacion_detallada(status, proyecto, party)
    codigo_postal = ubicacion["codigo_postal"]
    municipio = ubicacion["municipio"]
    provincia = ubicacion["provincia"]
    codigo_nuts = ubicacion["codigo_nuts"]
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
        "municipio_organo": ubicacion["municipio_organo"],
        "codigo_postal_organo": ubicacion["codigo_postal_organo"],
        "municipio_fuente": ubicacion["municipio_fuente"],
        "municipio_confianza": ubicacion["municipio_confianza"],
        "ambito_ubicacion": ubicacion["ambito_ubicacion"],
        "comunidad_autonoma": None,
        "latitud": None,
        "longitud": None,
        "fecha_limite": fecha_limite,
        "fecha_publicacion": extraer_fecha_publicacion(status),
        "fecha_resolucion": extraer_fecha_resolucion(status),
        "fecha_actualizacion": texto_xml(entrada, "atom:updated"),
        "adjudicatario": adjudicatario,
        "adjudicatarios_detalle": extraer_adjudicatarios(status),
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
