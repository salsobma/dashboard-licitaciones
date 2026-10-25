import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import sincronizar_licitaciones as sync
from feed_parser import (
    NAMESPACES,
    bajas_desde_pagina,
    extraer_adjudicatarios,
    extraer_resultados,
    extraer_resultados_lotes,
    extraer_fecha_resolucion,
    extraer_ubicacion,
    extraer_ubicacion_detallada,
)
import xml.etree.ElementTree as ET


def fila_base(lic_id: str) -> dict[str, object]:
    fila = {columna: None for columna in sync.COLUMNAS_FUENTE}
    fila.update(
        {
            "id": lic_id,
            "id_licitacion_corta": lic_id,
            "expediente": lic_id,
            "titulo": "Servicio de ingeniería",
            "organo_contratante": "Órgano de prueba",
            "tipo_contrato": "2",
            "estado": "PUB",
            "pbl_sin_iva": 1000.0,
            "cpv": "71000000",
            "fecha_actualizacion": "2026-08-20T10:00:00+02:00",
            "url_licitacion": "https://contrataciondelestado.es/prueba",
            "documentos_adjuntos": "[]",
            "origen": "perfil_plataforma",
        }
    )
    return fila


class SincronizacionAuditableTest(unittest.TestCase):
    def test_adjudicatarios_conservan_identidad_nombre_y_lote(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:TenderResult>
                    <cbc:ResultCode>8</cbc:ResultCode>
                    <cbc:ProcurementProjectLotID>2</cbc:ProcurementProjectLotID>
                    <cac:WinningParty>
                        <cac:PartyIdentification><cbc:ID schemeName="NIF">B-12345678</cbc:ID></cac:PartyIdentification>
                        <cac:PartyName><cbc:Name>Empresa Ejemplo, S.L.</cbc:Name></cac:PartyName>
                    </cac:WinningParty>
                </cac:TenderResult>
            </ContractFolderStatus>'''
        )
        self.assertEqual(
            extraer_adjudicatarios(status),
            '[{"identificador": "B-12345678", "tipo_identificador": "NIF", '
            '"nombre": "Empresa Ejemplo, S.L.", "lote": "2", "resultado": "8"}]',
        )

    def test_admite_cpv_71_y_45(self):
        for cpv in ("71000000", "45000000"):
            fila = fila_base(f"cpv-{cpv}")
            fila["cpv"] = cpv
            fila["codigo_nuts"] = "ES523"
            evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
            self.assertIsNotNone(evaluada)
            self.assertIsNone(motivo)

        fila = fila_base("cpv-fuera")
        fila["cpv"] = "33000000"
        fila["codigo_nuts"] = "ES523"
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNone(evaluada)
        self.assertEqual(motivo, "cpv_fuera_71_45")

    def test_maestro_postal_devuelve_municipio_ine_y_no_nucleo(self):
        maestro = sync.cargar_maestro()
        self.assertEqual(maestro["12540"]["municipio"], "Vila-real")
        self.assertNotEqual(maestro["12540"]["municipio"], "Carinyena")

    def test_feed_infiere_municipio_en_contexto_explicito(self):
        fila = fila_base("titulo-municipio")
        fila.update({
            "titulo": "Obras de saneamiento en el municipio de Silla",
            "organo_contratante": "Presidencia de la Diputación de Valencia",
            "codigo_nuts": "ES523",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNone(motivo)
        self.assertEqual(evaluada["municipio"], "Silla")
        self.assertEqual(evaluada["municipio_fuente"], "titulo_contexto_explicito")
        self.assertEqual(evaluada["ambito_ubicacion"], "municipio_concreto")

    def test_feed_no_elige_uno_si_el_titulo_declara_varios(self):
        fila = fila_base("titulo-varios")
        fila.update({
            "titulo": "Actuaciones en los municipios de Silla y Beniparrell",
            "organo_contratante": "Presidencia de la Diputación de Valencia",
            "codigo_nuts": "ES523",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNone(motivo)
        self.assertIsNone(evaluada["municipio"])
        self.assertEqual(evaluada["ambito_ubicacion"], "varios_municipios")

    def test_contrato_menor_sin_adjudicatario_se_incorpora_con_aviso(self):
        fila = fila_base("menor-incompleto")
        fila.update({
            "origen": "contrato_menor",
            "estado": "RES",
            "fecha_adjudicacion": "2026-04-12",
            "adjudicatario": None,
            "importe_adjudicacion_sin_iva": 4500.0,
            "codigo_nuts": "ES523",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNone(motivo)
        self.assertIsNotNone(evaluada)
        self.assertEqual(
            evaluada["incidencias_datos"],
            '["adjudicatario_no_publicado"]',
        )

    def test_fecha_relevante_excluye_expediente_antiguo_actualizado(self):
        fila = fila_base("antigua")
        fila.update({
            "estado": "RES",
            "fecha_publicacion": "2021-05-10",
            "fecha_actualizacion": "2026-10-20T10:00:00+02:00",
            "codigo_nuts": "ES523",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNone(evaluada)
        self.assertEqual(motivo, "fecha_relevante_anterior_inicio_base")

    def test_fecha_relevante_conserva_adjudicacion_actual_de_licitacion_antigua(self):
        fila = fila_base("adjudicada-actual")
        fila.update({
            "estado": "RES",
            "fecha_publicacion": "2021-05-10",
            "fecha_adjudicacion": "2026-04-15",
            "adjudicatario": "Empresa de prueba",
            "codigo_nuts": "ES523",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNotNone(evaluada)
        self.assertIsNone(motivo)

    def test_normaliza_comunidad_cuando_la_provincia_es_valenciana(self):
        fila = fila_base("provincia-valenciana")
        fila.update({
            "provincia": "Valencia/València",
            "comunidad_autonoma": "Murcia",
        })
        evaluada, motivo = sync.evaluar_y_enriquecer(fila, {})
        self.assertIsNotNone(evaluada)
        self.assertIsNone(motivo)
        self.assertEqual(
            evaluada["comunidad_autonoma"], "Comunitat Valenciana"
        )

    def test_ubicacion_es52_no_se_completa_con_sede_del_organo(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:ProcurementProject>
                    <cac:RealizedLocation><cbc:CountrySubentityCode>ES52</cbc:CountrySubentityCode></cac:RealizedLocation>
                </cac:ProcurementProject>
                <cac:LocatedContractingParty><cac:Party><cac:PostalAddress>
                    <cbc:PostalZone>28001</cbc:PostalZone><cbc:CityName>Madrid</cbc:CityName>
                </cac:PostalAddress></cac:Party></cac:LocatedContractingParty>
            </ContractFolderStatus>'''
        )
        proyecto = status.find("cac:ProcurementProject", NAMESPACES)
        party = status.find("cac:LocatedContractingParty/cac:Party", NAMESPACES)
        self.assertEqual(
            extraer_ubicacion(status, proyecto, party),
            (None, None, None, "ES52"),
        )

    def test_prioriza_municipio_y_cp_sobre_nuts_provincial_de_lotes(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:ProcurementProject>
                    <cac:RealizedLocation><cac:Address><cbc:PostalZone>12540</cbc:PostalZone><cbc:CityName>Vila-real</cbc:CityName></cac:Address></cac:RealizedLocation>
                </cac:ProcurementProject>
                <cac:ProcurementProjectLot><cac:ProcurementProject>
                    <cac:RealizedLocation><cbc:CountrySubentity>Castellón/Castelló</cbc:CountrySubentity><cbc:CountrySubentityCode>ES522</cbc:CountrySubentityCode></cac:RealizedLocation>
                </cac:ProcurementProject></cac:ProcurementProjectLot>
            </ContractFolderStatus>'''
        )
        proyecto = status.find("cac:ProcurementProject", NAMESPACES)
        ubicacion = extraer_ubicacion_detallada(status, proyecto, None)
        self.assertEqual(ubicacion["municipio"], "Vila-real")
        self.assertEqual(ubicacion["codigo_postal"], "12540")
        self.assertEqual(ubicacion["municipio_fuente"], "xml_proyecto")
        self.assertEqual(ubicacion["municipio_confianza"], "oficial")

    def test_infiere_sede_solo_para_organo_municipal_y_provincia_coherente(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:LocatedContractingParty><cac:Party>
                    <cac:PartyName><cbc:Name>Pleno del Ayuntamiento de Marines</cbc:Name></cac:PartyName>
                    <cac:PostalAddress><cbc:PostalZone>46163</cbc:PostalZone><cbc:CityName>Marines (Valencia)</cbc:CityName></cac:PostalAddress>
                </cac:Party></cac:LocatedContractingParty>
                <cac:ProcurementProject><cbc:Name>Digitalización de la red de Marines</cbc:Name>
                    <cac:RealizedLocation><cbc:CountrySubentityCode>ES523</cbc:CountrySubentityCode></cac:RealizedLocation>
                </cac:ProcurementProject>
            </ContractFolderStatus>'''
        )
        proyecto = status.find("cac:ProcurementProject", NAMESPACES)
        party = status.find("cac:LocatedContractingParty/cac:Party", NAMESPACES)
        ubicacion = extraer_ubicacion_detallada(status, proyecto, party)
        self.assertEqual(ubicacion["municipio"], "Marines")
        self.assertEqual(ubicacion["municipio_fuente"], "titulo_y_sede_organo")
        self.assertEqual(ubicacion["municipio_confianza"], "alta")

    def test_no_infiere_sede_si_el_nuts_es_de_otra_provincia(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:LocatedContractingParty><cac:Party>
                    <cac:PartyName><cbc:Name>Entidad autonómica</cbc:Name></cac:PartyName>
                    <cac:PostalAddress><cbc:PostalZone>46018</cbc:PostalZone><cbc:CityName>Valencia</cbc:CityName></cac:PostalAddress>
                </cac:Party></cac:LocatedContractingParty>
                <cac:ProcurementProject><cbc:Name>Obra en Valencia</cbc:Name>
                    <cac:RealizedLocation><cbc:CountrySubentityCode>ES522</cbc:CountrySubentityCode></cac:RealizedLocation>
                </cac:ProcurementProject>
            </ContractFolderStatus>'''
        )
        proyecto = status.find("cac:ProcurementProject", NAMESPACES)
        party = status.find("cac:LocatedContractingParty/cac:Party", NAMESPACES)
        ubicacion = extraer_ubicacion_detallada(status, proyecto, party)
        self.assertIsNone(ubicacion["municipio"])
        self.assertEqual(ubicacion["ambito_ubicacion"], "municipio_no_publicado")

    def test_detecta_ubicacion_en_varios_municipios(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:ProcurementProject>
                    <cbc:Name>Actuaciones en varios municipios</cbc:Name>
                    <cac:RealizedLocation><cbc:CountrySubentityCode>ES523</cbc:CountrySubentityCode></cac:RealizedLocation>
                </cac:ProcurementProject>
            </ContractFolderStatus>'''
        )
        proyecto = status.find("cac:ProcurementProject", NAMESPACES)
        ubicacion = extraer_ubicacion_detallada(status, proyecto, None)
        self.assertIsNone(ubicacion["municipio"])
        self.assertEqual(ubicacion["ambito_ubicacion"], "varios_municipios")

    def test_fecha_resolucion_usa_el_anuncio_oficial(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus
                xmlns:pe="{NAMESPACES["cac-place-ext"]}"
                xmlns:pbe="{NAMESPACES["cbc-place-ext"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <pe:ValidNoticeInfo><pbe:NoticeTypeCode>DOC_CN</pbe:NoticeTypeCode><cbc:IssueDate>2025-01-02</cbc:IssueDate></pe:ValidNoticeInfo>
                <pe:ValidNoticeInfo><pbe:NoticeTypeCode>DESISTIMIENTO</pbe:NoticeTypeCode><cbc:IssueDate>2026-03-04</cbc:IssueDate></pe:ValidNoticeInfo>
            </ContractFolderStatus>'''
        )
        self.assertEqual(extraer_fecha_resolucion(status), "2026-03-04")

    def test_actualizacion_nuts_provincial_no_borra_municipio_anterior(self):
        anterior = fila_base("ubicacion-conocida")
        anterior.update({
            "municipio": "Marines",
            "codigo_postal": "46163",
            "codigo_nuts": "ES523",
            "municipio_fuente": "titulo_y_sede_organo",
            "municipio_confianza": "alta",
        })
        existente = tuple(anterior.get(c) for c in sync.COLUMNAS_FUENTE)
        nueva = fila_base("ubicacion-conocida")
        nueva["codigo_nuts"] = "ES523"
        self.assertTrue(sync.heredar_ubicacion_anterior(nueva, existente))
        self.assertEqual(nueva["municipio"], "Marines")
        self.assertEqual(nueva["codigo_postal"], "46163")
        self.assertEqual(nueva["municipio_confianza"], "alta")

    def test_resultados_por_lote_se_conservan(self):
        status = ET.fromstring(
            f'''<ContractFolderStatus xmlns:cac="{NAMESPACES["cac"]}"
                xmlns:cbc="{NAMESPACES["cbc"]}">
                <cac:TenderResult><cbc:ResultCode>8</cbc:ResultCode><cbc:ProcurementProjectLotID>1</cbc:ProcurementProjectLotID></cac:TenderResult>
                <cac:TenderResult><cbc:ResultCode>3</cbc:ResultCode><cbc:ProcurementProjectLotID>2</cbc:ProcurementProjectLotID></cac:TenderResult>
                <cac:TenderResult><cbc:ResultCode>8</cbc:ResultCode><cbc:ProcurementProjectLotID>1</cbc:ProcurementProjectLotID></cac:TenderResult>
            </ContractFolderStatus>'''
        )
        self.assertEqual(extraer_resultados(status), "8,3")
        self.assertEqual(
            extraer_resultados_lotes(status),
            '[{"lote": "1", "codigo": "8"}, {"lote": "2", "codigo": "3"}]',
        )

    def test_baja_lee_tipo_del_comentario_atom(self):
        raiz = ET.fromstring(
            f'''<feed xmlns:at="{NAMESPACES["at"]}">
                <at:deleted-entry ref="anulada" when="2026-08-20T10:00:00Z">
                    <at:comment type="ANULADA" />
                </at:deleted-entry>
                <at:deleted-entry ref="cerrada" when="2026-08-20T10:00:00Z">
                    <at:comment type="CERRADA" />
                </at:deleted-entry>
            </feed>'''
        )
        self.assertEqual(
            [(baja["id"], baja["estado"]) for baja in bajas_desde_pagina(raiz)],
            [("anulada", "ANUL"), ("cerrada", "CERR")],
        )

    def test_auditoria_completa_empieza_en_inicio_de_cada_fuente(self):
        for origen in ("perfil_plataforma", "contrato_menor"):
            inicio = sync.INICIO_HISTORICO[origen]
            with sqlite3.connect(":memory:") as conexion:
                obtenido = sync.obtener_checkpoint(
                    conexion,
                    desde_inicio=True,
                    inicio_historico=inicio,
                    origen=origen,
                )
            self.assertEqual(obtenido, inicio)

    def test_nuts_herencia_cuarentena_y_conciliacion(self):
        with tempfile.TemporaryDirectory() as temporal:
            db_path = Path(temporal) / "prueba.db"
            metadata_path = Path(temporal) / "metadata.json"
            definiciones = []
            for columna in sync.COLUMNAS_FUENTE:
                tipo = "REAL" if columna in {
                    "pbl_sin_iva", "pbl_con_iva", "valor_estimado",
                    "latitud", "longitud", "importe_adjudicacion_sin_iva",
                    "importe_adjudicacion_con_iva",
                } else "TEXT"
                definiciones.append(
                    f"{columna} {tipo}" + (" PRIMARY KEY" if columna == "id" else "")
                )
            with closing(sqlite3.connect(db_path)) as conexion:
                conexion.execute(
                    f"CREATE TABLE licitaciones ({','.join(definiciones)})"
                )
                anterior = fila_base("existente")
                anterior.update(
                    {
                        "codigo_postal": "46001",
                        "municipio": "Valencia",
                        "provincia": "Valencia",
                        "comunidad_autonoma": "Comunitat Valenciana",
                        "latitud": 39.47,
                        "longitud": -0.37,
                    }
                )
                conexion.execute(
                    f"INSERT INTO licitaciones ({','.join(sync.COLUMNAS_FUENTE)}) "
                    f"VALUES ({','.join('?' for _ in sync.COLUMNAS_FUENTE)})",
                    tuple(anterior[columna] for columna in sync.COLUMNAS_FUENTE),
                )
                conexion.commit()

            heredada = fila_base("existente")
            heredada["titulo"] = "Servicio actualizado"
            heredada["fecha_actualizacion"] = "2026-08-21T10:00:00+02:00"
            por_nuts = fila_base("nuevo-nuts")
            por_nuts["codigo_nuts"] = "ES523"
            sin_ubicacion = fila_base("nuevo-sin-ubicacion")

            def descarga(_checkpoint, feed_url=sync.FEED_URL, origen="perfil_plataforma"):
                if origen == "contrato_menor":
                    return [], [], [], "2026-08-21T10:00:00+02:00"
                return [heredada, por_nuts, sin_ubicacion], [], [], "2026-08-21T10:00:00+02:00"

            with (
                patch.object(sync, "descargar_incremento", side_effect=descarga),
                patch.object(sync, "cargar_maestro", return_value={}),
            ):
                resultado = sync.sincronizar(
                    db_path,
                    guardar_metadata=False,
                    metadata_path=metadata_path,
                )

            self.assertTrue(resultado["conciliacion"]["cuadra"])
            self.assertEqual(resultado["ubicaciones_heredadas"], 1)
            self.assertEqual(resultado["descartadas_por_motivo"], {
                "ubicacion_no_verificable": 1
            })
            with closing(sqlite3.connect(db_path)) as conexion:
                actualizado = conexion.execute(
                    "SELECT titulo, provincia FROM licitaciones WHERE id='existente'"
                ).fetchone()
                nuts = conexion.execute(
                    "SELECT provincia, comunidad_autonoma FROM licitaciones "
                    "WHERE id='nuevo-nuts'"
                ).fetchone()
                cuarentena = conexion.execute(
                    "SELECT motivo FROM cuarentena_licitaciones "
                    "WHERE id='nuevo-sin-ubicacion'"
                ).fetchone()
            self.assertEqual(actualizado, ("Servicio actualizado", "Valencia"))
            self.assertEqual(nuts, ("Valencia", "Comunitat Valenciana"))
            self.assertEqual(cuarentena, ("ubicacion_no_verificable",))


if __name__ == "__main__":
    unittest.main()
