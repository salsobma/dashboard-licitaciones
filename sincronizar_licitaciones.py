from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from feed_parser import bajas_desde_pagina, fila_desde_entrada, NAMESPACES


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "licitaciones.db"
METADATA_PATH = BASE_DIR / "sync_metadata.json"
MAESTRO_PATH = BASE_DIR / "provincias.xlsx"
FEED_URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)
SOLAPE_HORAS = 48
MAX_PAGINAS_SEGURIDAD = 200
DOMINIOS_PERMITIDOS = {
    "contrataciondelsectorpublico.gob.es",
    "contrataciondelestado.es",
}

COLUMNAS_FUENTE = (
    "id",
    "id_licitacion_corta",
    "expediente",
    "titulo",
    "organo_contratante",
    "tipo_contrato",
    "estado",
    "pbl_sin_iva",
    "pbl_con_iva",
    "valor_estimado",
    "cpv",
    "codigo_postal",
    "municipio",
    "provincia",
    "comunidad_autonoma",
    "latitud",
    "longitud",
    "fecha_limite",
    "fecha_publicacion",
    "fecha_actualizacion",
    "adjudicatario",
    "fecha_adjudicacion",
    "importe_adjudicacion_sin_iva",
    "importe_adjudicacion_con_iva",
    "url_licitacion",
    "documentos_adjuntos",
)


def fecha_utc(valor: object) -> datetime | None:
    if not valor:
        return None
    fecha = pd.to_datetime(valor, errors="coerce", utc=True)
    return None if pd.isna(fecha) else fecha.to_pydatetime()


def descargar_pagina(url: str) -> tuple[bytes, ET.Element, str]:
    if urlparse(url).hostname not in DOMINIOS_PERMITIDOS:
        raise RuntimeError("El feed enlaza a un dominio no permitido.")
    peticion = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(peticion, timeout=60) as respuesta:
        contenido = respuesta.read()
        url_real = respuesta.geturl()
    if b"Web Application Firewall" in contenido[:4096]:
        raise RuntimeError("El cortafuegos rechazo temporalmente la consulta.")
    raiz = ET.fromstring(contenido)
    if raiz.tag != "{http://www.w3.org/2005/Atom}feed":
        raise RuntimeError("La respuesta no contiene un feed ATOM valido.")
    return contenido, raiz, url_real


def enlace_siguiente(raiz: ET.Element, url_real: str) -> str | None:
    enlace = raiz.find("atom:link[@rel='next']", NAMESPACES)
    if enlace is None or not enlace.attrib.get("href"):
        return None
    siguiente = urljoin(url_real, enlace.attrib["href"])
    if urlparse(siguiente).hostname not in DOMINIOS_PERMITIDOS:
        raise RuntimeError("La pagina siguiente usa un dominio no permitido.")
    return siguiente


def clave_texto(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().lower()


def codigo_postal(valor: object) -> str:
    digitos = "".join(c for c in str(valor or "").split(".")[0] if c.isdigit())
    return digitos.zfill(5) if digitos else ""


def cargar_maestro() -> dict[str, dict[str, object]]:
    maestro = pd.read_excel(
        MAESTRO_PATH, sheet_name="municipios datos", dtype={"codigo_postal": str}
    )
    resultado: dict[str, dict[str, object]] = {}
    for _, fila in maestro.iterrows():
        cp = codigo_postal(fila.get("codigo_postal"))
        if cp and cp not in resultado:
            resultado[cp] = {
                "municipio": fila.get("nucleo_nombre"),
                "provincia": fila.get("PROVINCIA"),
                "comunidad_autonoma": fila.get("COMUNIDAD"),
                "latitud": fila.get("Latitud"),
                "longitud": fila.get("Longitud"),
            }
    return resultado


def enriquecer_y_filtrar(
    fila: dict[str, object], maestro: dict[str, dict[str, object]]
) -> dict[str, object] | None:
    cpvs = [codigo.strip() for codigo in str(fila.get("cpv") or "").split(",")]
    if not any(codigo.startswith("71") for codigo in cpvs):
        return None

    cp = codigo_postal(fila.get("codigo_postal"))
    ubicacion = maestro.get(cp)
    if ubicacion:
        fila.update(ubicacion)

    provincia = clave_texto(fila.get("provincia"))
    comunidad = clave_texto(fila.get("comunidad_autonoma"))
    es_cv = "valenc" in comunidad or provincia in {
        "alicante",
        "castellon",
        "castello",
        "valencia",
    }
    return fila if es_cv else None


def valor_sqlite(valor: object) -> object:
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    return valor.item() if hasattr(valor, "item") else valor


def leer_metadata(metadata_path: Path = METADATA_PATH) -> dict[str, object]:
    try:
        contenido = json.loads(metadata_path.read_text(encoding="utf-8"))
        return contenido if isinstance(contenido, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def obtener_checkpoint(
    conexion: sqlite3.Connection,
    metadata_path: Path = METADATA_PATH,
    desde_inicio: bool = False,
) -> datetime:
    if desde_inicio:
        return datetime(datetime.now().year, 1, 1, tzinfo=ZoneInfo("UTC"))
    metadata = leer_metadata(metadata_path)
    checkpoint = fecha_utc(metadata.get("feed_incremental_actualizado_hasta"))
    if checkpoint is None:
        maximo = conexion.execute(
            "SELECT MAX(fecha_actualizacion) FROM licitaciones"
        ).fetchone()[0]
        checkpoint = fecha_utc(maximo)
    if checkpoint is None:
        checkpoint = datetime(datetime.now().year, 1, 1, tzinfo=ZoneInfo("UTC"))
    return checkpoint


def descargar_incremento(checkpoint: datetime):
    limite = checkpoint - timedelta(hours=SOLAPE_HORAS)
    siguiente = FEED_URL
    visitadas: set[str] = set()
    filas: list[dict[str, object]] = []
    bajas: list[dict[str, str | None]] = []
    paginas = []
    fecha_feed = None

    while siguiente and len(paginas) < MAX_PAGINAS_SEGURIDAD:
        if siguiente in visitadas:
            raise RuntimeError("La paginacion oficial contiene un ciclo.")
        visitadas.add(siguiente)
        _, raiz, url_real = descargar_pagina(siguiente)
        if fecha_feed is None:
            fecha_feed = raiz.findtext("atom:updated", namespaces=NAMESPACES)

        fechas_pagina = []
        for entrada in raiz.findall("atom:entry", NAMESPACES):
            fila = fila_desde_entrada(entrada)
            if fila:
                filas.append(fila)
                fecha = fecha_utc(fila.get("fecha_actualizacion"))
                if fecha:
                    fechas_pagina.append(fecha)
        bajas_pagina = bajas_desde_pagina(raiz)
        bajas.extend(bajas_pagina)
        fechas_pagina.extend(
            fecha
            for fecha in (fecha_utc(baja.get("fecha_actualizacion")) for baja in bajas_pagina)
            if fecha
        )
        paginas.append({"url": url_real, "registros": len(fechas_pagina)})

        # El feed esta ordenado globalmente de mas reciente a mas antiguo.
        if fechas_pagina and max(fechas_pagina) < limite:
            break
        siguiente = enlace_siguiente(raiz, url_real)
    else:
        if siguiente:
            raise RuntimeError("Se alcanzo el limite de seguridad de paginacion.")

    return filas, bajas, paginas, fecha_feed


def inicializar_esquema(conexion: sqlite3.Connection) -> None:
    columnas = {fila[1] for fila in conexion.execute("PRAGMA table_info(licitaciones)")}
    migraciones = {
        "fecha_publicacion": "TEXT",
        "importe_adjudicacion_sin_iva": "REAL",
    }
    for columna, tipo in migraciones.items():
        if columna not in columnas:
            conexion.execute(f"ALTER TABLE licitaciones ADD COLUMN {columna} {tipo}")


def sincronizar(
    db_path: Path,
    guardar_metadata: bool = True,
    metadata_path: Path = METADATA_PATH,
    desde_inicio: bool = False,
) -> dict[str, object]:
    maestro = cargar_maestro()
    with sqlite3.connect(db_path) as conexion:
        inicializar_esquema(conexion)
        checkpoint = obtener_checkpoint(conexion, metadata_path, desde_inicio=desde_inicio)
        filas, bajas, paginas, fecha_feed = descargar_incremento(checkpoint)

        # Una misma sindicacion puede contener varias versiones del mismo ID.
        ultimas: dict[str, dict[str, object]] = {}
        for fila in filas:
            lic_id = str(fila.get("id") or "").strip()
            if not lic_id:
                continue
            anterior = ultimas.get(lic_id)
            if anterior is None or str(fila.get("fecha_actualizacion") or "") >= str(
                anterior.get("fecha_actualizacion") or ""
            ):
                ultimas[lic_id] = fila

        insertadas = 0
        actualizadas = 0
        descartadas = 0
        for fila in ultimas.values():
            fila = enriquecer_y_filtrar(fila, maestro)
            if fila is None:
                descartadas += 1
                continue
            valores = tuple(valor_sqlite(fila.get(columna)) for columna in COLUMNAS_FUENTE)
            existente = conexion.execute(
                f"SELECT {','.join(COLUMNAS_FUENTE)} FROM licitaciones WHERE id = ?",
                (fila["id"],),
            ).fetchone()
            nueva_fecha = fecha_utc(fila.get("fecha_actualizacion"))
            fecha_anterior = (
                fecha_utc(existente[COLUMNAS_FUENTE.index("fecha_actualizacion")])
                if existente
                else None
            )
            if existente and nueva_fecha and fecha_anterior and nueva_fecha < fecha_anterior:
                continue
            if existente and tuple(existente) == valores:
                continue

            marcadores = ",".join("?" for _ in COLUMNAS_FUENTE)
            actualizaciones = ",".join(
                f"{columna}=excluded.{columna}" for columna in COLUMNAS_FUENTE if columna != "id"
            )
            conexion.execute(
                f"""
                INSERT INTO licitaciones ({','.join(COLUMNAS_FUENTE)})
                VALUES ({marcadores})
                ON CONFLICT(id) DO UPDATE SET {actualizaciones}
                """,
                valores,
            )
            if existente:
                actualizadas += 1
            else:
                insertadas += 1

        bajas_aplicadas = 0
        for baja in bajas:
            lic_id = str(baja.get("id") or "").strip()
            if not lic_id:
                continue
            existente = conexion.execute(
                "SELECT estado, fecha_actualizacion FROM licitaciones WHERE id = ?", (lic_id,)
            ).fetchone()
            if not existente:
                continue
            nueva_fecha = fecha_utc(baja.get("fecha_actualizacion"))
            fecha_anterior = fecha_utc(existente[1])
            if fecha_anterior and nueva_fecha and nueva_fecha < fecha_anterior:
                continue
            if existente[0] == baja.get("estado") and existente[1] == baja.get(
                "fecha_actualizacion"
            ):
                continue
            conexion.execute(
                "UPDATE licitaciones SET estado=?, fecha_actualizacion=? WHERE id=?",
                (baja.get("estado"), baja.get("fecha_actualizacion"), lic_id),
            )
            bajas_aplicadas += 1

        conexion.commit()
        total = conexion.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]
        duplicados = conexion.execute(
            "SELECT COUNT(*) FROM (SELECT id FROM licitaciones GROUP BY id HAVING COUNT(*) > 1)"
        ).fetchone()[0]

    ahora = datetime.now(ZoneInfo("Europe/Madrid")).isoformat()
    resultado = {
        "modo": "auditoria_anual" if desde_inicio else "incremental",
        "sincronizado_en": ahora,
        "feed_actualizado": fecha_feed,
        "checkpoint_anterior": checkpoint.isoformat(),
        "paginas": len(paginas),
        "versiones_leidas": len(filas),
        "ids_revisados": len(ultimas),
        "insertadas": insertadas,
        "actualizadas": actualizadas,
        "bajas": bajas_aplicadas,
        "descartadas_filtro": descartadas,
        "total": total,
        "duplicados": duplicados,
    }
    if guardar_metadata:
        metadata = leer_metadata(metadata_path)
        metadata.update(
            {
                "feed_incremental_sincronizado_en": ahora,
                "feed_incremental_actualizado_hasta": fecha_feed,
                "feed_incremental_ultima_ejecucion": resultado,
            }
        )
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return resultado


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--metadata", type=Path, default=METADATA_PATH)
    parser.add_argument("--sin-metadata", action="store_true")
    parser.add_argument(
        "--desde-inicio",
        action="store_true",
        help="Revisa el feed completo desde el 1 de enero del año en curso.",
    )
    args = parser.parse_args()
    resultado = sincronizar(
        args.db.resolve(),
        guardar_metadata=not args.sin_metadata,
        metadata_path=args.metadata.resolve(),
        desde_inicio=args.desde_inicio,
    )
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
