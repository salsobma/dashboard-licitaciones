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
    extraer_resultados,
    extraer_resultados_lotes,
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
