from __future__ import annotations

import argparse
import json
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from feed_parser import bajas_desde_pagina, fila_desde_entrada, NAMESPACES


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "licitaciones.db"
METADATA_PATH = BASE_DIR / "sync_metadata.json"
MAESTRO_PATH = BASE_DIR / "provincias.xlsx"
FEED_URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)
FEED_URL_ALTERNATIVA = FEED_URL.replace(
    "contrataciondelsectorpublico.gob.es", "contrataciondelestado.es"
)
FEED_CONTRATOS_MENORES_URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_1143/contratosMenoresPerfilesContratantes.atom"
)
SOLAPE_HORAS = 48
MAX_PAGINAS_SEGURIDAD = 5000
INICIO_HISTORICO = {
    "perfil_plataforma": datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC")),
    "contrato_menor": datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC")),
}
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
    "resultado_codigos",
    "resultados_lotes",
    "pbl_sin_iva",
    "pbl_con_iva",
    "valor_estimado",
    "cpv",
    "codigo_postal",
    "municipio",
    "provincia",
    "codigo_nuts",
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
    "contrato_id",
    "fecha_formalizacion",
    "fecha_inicio_contrato",
    "url_licitacion",
    "documentos_adjuntos",
    "origen",
)


def fecha_utc(valor: object) -> datetime | None:
    if not valor:
        return None
    fecha = pd.to_datetime(valor, errors="coerce", utc=True)
    return None if pd.isna(fecha) else fecha.to_pydatetime()


def descargar_pagina(url: str) -> tuple[bytes, ET.Element, str]:
    if urlparse(url).hostname not in DOMINIOS_PERMITIDOS:
        raise RuntimeError("El feed enlaza a un dominio no permitido.")
    variantes = [url]
    for origen, destino in (
        ("contrataciondelsectorpublico.gob.es", "contrataciondelestado.es"),
        ("contrataciondelestado.es", "contrataciondelsectorpublico.gob.es"),
    ):
        alternativa = url.replace(origen, destino)
        if alternativa not in variantes:
            variantes.append(alternativa)
    errores = []
    cabeceras = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": "https://contrataciondelsectorpublico.gob.es/datosabiertos",
        "Cache-Control": "no-cache",
    }
    for ronda in range(2):
        for candidata in variantes:
            try:
                respuesta = requests.get(candidata, headers=cabeceras, timeout=45)
                respuesta.raise_for_status()
                contenido = respuesta.content
                if b"Web Application Firewall" in contenido[:4096]:
                    raise RuntimeError("el cortafuegos rechazo temporalmente la consulta")
                if b"<feed" not in contenido[:4096] and b"<?xml" not in contenido[:4096]:
                    tipo = respuesta.headers.get("content-type", "desconocido")
                    raise RuntimeError(
                        f"la plataforma no devolvio un feed XML ({tipo})"
                    )
                raiz = ET.fromstring(contenido)
                if raiz.tag != "{http://www.w3.org/2005/Atom}feed":
                    raise RuntimeError("la respuesta no contiene un feed ATOM valido")
                return contenido, raiz, respuesta.url
            except Exception as error:
                errores.append(f"{candidata}: {error}")
        if ronda == 0:
            time.sleep(2)
    raise RuntimeError(
        "El feed oficial no esta disponible. "
        f"Ultimo intento: {errores[-1] if errores else 'error desconocido'}"
    )


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


def codigo_nuts(valor: object) -> str:
    return "".join(c for c in str(valor or "").upper() if c.isalnum())


def evaluar_y_enriquecer(
    fila: dict[str, object], maestro: dict[str, dict[str, object]]
) -> tuple[dict[str, object] | None, str | None]:
    cpvs = [codigo.strip() for codigo in str(fila.get("cpv") or "").split(",")]
    if not any(cpvs):
        return None, "sin_cpv"
    if not any(codigo.startswith("71") for codigo in cpvs):
        return None, "cpv_fuera_71"

    origen = str(fila.get("origen") or "perfil_plataforma")
    inicio = INICIO_HISTORICO.get(origen)
    actualizada = fecha_utc(fila.get("fecha_actualizacion"))
    if inicio and actualizada and actualizada < inicio:
        return None, "actualizacion_anterior_inicio_base"

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
    nuts = codigo_nuts(fila.get("codigo_nuts"))
    if not es_cv and nuts.startswith("ES52"):
        fila["comunidad_autonoma"] = "Comunitat Valenciana"
        if not str(fila.get("provincia") or "").strip():
            fila["provincia"] = {
                "ES521": "Alicante",
                "ES522": "Castellón",
                "ES523": "Valencia",
            }.get(nuts[:5])
        es_cv = True
    if not es_cv:
        tiene_evidencia = bool(provincia or comunidad or nuts or cp)
        return None, (
            "fuera_comunitat_valenciana"
            if tiene_evidencia else "ubicacion_no_verificable"
        )

    if origen == "contrato_menor":
        fecha_adjudicacion = str(fila.get("fecha_adjudicacion") or "")
        try:
            anyo_adjudicacion = int(fecha_adjudicacion[:4])
        except ValueError:
            return None, "fecha_adjudicacion_invalida"
        if anyo_adjudicacion < 2025:
            return None, "adjudicacion_anterior_2025"
        if not str(fila.get("adjudicatario") or "").strip():
            return None, "sin_adjudicatario"
        if fila.get("importe_adjudicacion_sin_iva") is None:
            return None, "sin_importe_adjudicacion"

    return fila, None


def enriquecer_y_filtrar(
    fila: dict[str, object], maestro: dict[str, dict[str, object]]
) -> dict[str, object] | None:
    return evaluar_y_enriquecer(fila, maestro)[0]


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
    clave_metadata: str = "feed_incremental_actualizado_hasta",
    origen: str = "perfil_plataforma",
    dias_iniciales: int | None = None,
    inicio_historico: datetime | None = None,
) -> datetime:
    if desde_inicio:
        if inicio_historico is None:
            raise ValueError("La auditoría completa requiere una fecha de inicio.")
        return inicio_historico
    metadata = leer_metadata(metadata_path)
    checkpoint = fecha_utc(metadata.get(clave_metadata))
    if checkpoint is None:
        maximo = conexion.execute(
            "SELECT MAX(fecha_actualizacion) FROM licitaciones WHERE origen = ?",
            (origen,),
        ).fetchone()[0]
        checkpoint = fecha_utc(maximo)
    if checkpoint is None:
        if dias_iniciales is None:
            checkpoint = datetime(datetime.now().year, 1, 1, tzinfo=ZoneInfo("UTC"))
        else:
            checkpoint = datetime.now(ZoneInfo("UTC")) - timedelta(
                days=dias_iniciales
            )
    return checkpoint


def descargar_incremento(
    checkpoint: datetime,
    feed_url: str = FEED_URL,
    origen: str = "perfil_plataforma",
):
    limite = checkpoint - timedelta(hours=SOLAPE_HORAS)
    siguiente = feed_url
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
                fila["origen"] = origen
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
        "codigo_nuts": "TEXT",
        "contrato_id": "TEXT",
        "fecha_formalizacion": "TEXT",
        "fecha_inicio_contrato": "TEXT",
        "origen": "TEXT NOT NULL DEFAULT 'perfil_plataforma'",
        "resultado_codigos": "TEXT",
        "resultados_lotes": "TEXT",
    }
    for columna, tipo in migraciones.items():
        if columna not in columnas:
            conexion.execute(f"ALTER TABLE licitaciones ADD COLUMN {columna} {tipo}")
    conexion.execute(
        """
        CREATE TABLE IF NOT EXISTS cuarentena_licitaciones (
            id TEXT NOT NULL,
            origen TEXT NOT NULL,
            motivo TEXT NOT NULL,
            fecha_actualizacion TEXT,
            expediente TEXT,
            titulo TEXT,
            cpv TEXT,
            provincia TEXT,
            codigo_nuts TEXT,
            datos_json TEXT NOT NULL,
            revisado_en TEXT NOT NULL,
            PRIMARY KEY (id, origen)
        )
        """
    )


CAMPOS_UBICACION = (
    "codigo_postal",
    "municipio",
    "provincia",
    "codigo_nuts",
    "comunidad_autonoma",
    "latitud",
    "longitud",
)
MOTIVOS_CUARENTENA = {
    "ubicacion_no_verificable",
    "fecha_adjudicacion_invalida",
    "sin_adjudicatario",
    "sin_importe_adjudicacion",
}


def heredar_ubicacion_anterior(
    fila: dict[str, object], existente: tuple[object, ...] | None
) -> bool:
    if existente is None:
        return False
    # NUTS, provincia o código postal nuevos constituyen evidencia territorial
    # propia. Solo heredamos cuando la actualización no trae ninguna de ellas.
    if any(
        str(fila.get(campo) or "").strip()
        for campo in ("codigo_postal", "provincia", "codigo_nuts")
    ):
        return False
    heredada = False
    for campo in CAMPOS_UBICACION:
        indice = COLUMNAS_FUENTE.index(campo)
        if not str(fila.get(campo) or "").strip() and existente[indice] is not None:
            fila[campo] = existente[indice]
            heredada = True
    return heredada


def guardar_cuarentena(
    conexion: sqlite3.Connection,
    fila: dict[str, object],
    motivo: str,
    revisado_en: str,
) -> None:
    lic_id = str(fila.get("id") or "").strip()
    if not lic_id:
        return
    datos = {
        columna: valor_sqlite(fila.get(columna))
        for columna in COLUMNAS_FUENTE
    }
    conexion.execute(
        """
        INSERT INTO cuarentena_licitaciones (
            id, origen, motivo, fecha_actualizacion, expediente, titulo,
            cpv, provincia, codigo_nuts, datos_json, revisado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id, origen) DO UPDATE SET
            motivo=excluded.motivo,
            fecha_actualizacion=excluded.fecha_actualizacion,
            expediente=excluded.expediente,
            titulo=excluded.titulo,
            cpv=excluded.cpv,
            provincia=excluded.provincia,
            codigo_nuts=excluded.codigo_nuts,
            datos_json=excluded.datos_json,
            revisado_en=excluded.revisado_en
        """,
        (
            lic_id,
            str(fila.get("origen") or ""),
            motivo,
            fila.get("fecha_actualizacion"),
            fila.get("expediente"),
            fila.get("titulo"),
            fila.get("cpv"),
            fila.get("provincia"),
            fila.get("codigo_nuts"),
            json.dumps(datos, ensure_ascii=False),
            revisado_en,
        ),
    )


def sincronizar(
    db_path: Path,
    guardar_metadata: bool = True,
    metadata_path: Path = METADATA_PATH,
    desde_inicio: bool = False,
) -> dict[str, object]:
    maestro = cargar_maestro()
    with sqlite3.connect(db_path) as conexion:
        inicializar_esquema(conexion)
        checkpoint = obtener_checkpoint(
            conexion,
            metadata_path,
            desde_inicio=desde_inicio,
            inicio_historico=INICIO_HISTORICO["perfil_plataforma"],
        )
        checkpoint_menores = obtener_checkpoint(
            conexion,
            metadata_path,
            # La primera carga usa la ventana reciente. El histórico de menores
            # se incorporará desde los archivos mensuales, que evitan recorrer
            # cientos de enlaces diarios y los bloqueos del WAF oficial.
            desde_inicio=desde_inicio,
            clave_metadata="feed_menores_actualizado_hasta",
            origen="contrato_menor",
            dias_iniciales=2,
            inicio_historico=INICIO_HISTORICO["contrato_menor"],
        )
        filas, bajas, paginas, fecha_feed = descargar_incremento(checkpoint)
        filas_menores, bajas_menores, paginas_menores, fecha_feed_menores = (
            descargar_incremento(
                checkpoint_menores,
                feed_url=FEED_CONTRATOS_MENORES_URL,
                origen="contrato_menor",
            )
        )
        filas.extend(filas_menores)
        bajas.extend(bajas_menores)
        paginas.extend(paginas_menores)

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
        sin_cambios = 0
        versiones_anteriores = 0
        ubicaciones_heredadas = 0
        cuarentena_revisada = 0
        motivos_descarte: Counter[str] = Counter()
        revisado_en = datetime.now(ZoneInfo("Europe/Madrid")).isoformat()
        for fila in ultimas.values():
            lic_id = str(fila.get("id") or "").strip()
            existente = conexion.execute(
                f"SELECT {','.join(COLUMNAS_FUENTE)} FROM licitaciones WHERE id = ?",
                (lic_id,),
            ).fetchone()
            if heredar_ubicacion_anterior(fila, existente):
                ubicaciones_heredadas += 1

            fila_evaluada, motivo = evaluar_y_enriquecer(fila, maestro)
            if fila_evaluada is None:
                descartadas += 1
                motivo = motivo or "motivo_desconocido"
                motivos_descarte[motivo] += 1
                if motivo in MOTIVOS_CUARENTENA:
                    guardar_cuarentena(
                        conexion, fila, motivo, revisado_en
                    )
                    cuarentena_revisada += 1
                else:
                    conexion.execute(
                        "DELETE FROM cuarentena_licitaciones "
                        "WHERE id = ? AND origen = ?",
                        (lic_id, fila.get("origen")),
                    )
                continue
            fila = fila_evaluada
            conexion.execute(
                "DELETE FROM cuarentena_licitaciones WHERE id = ? AND origen = ?",
                (fila["id"], fila.get("origen")),
            )
            valores = tuple(valor_sqlite(fila.get(columna)) for columna in COLUMNAS_FUENTE)
            nueva_fecha = fecha_utc(fila.get("fecha_actualizacion"))
            fecha_anterior = (
                fecha_utc(existente[COLUMNAS_FUENTE.index("fecha_actualizacion")])
                if existente
                else None
            )
            if existente and nueva_fecha and fecha_anterior and nueva_fecha < fecha_anterior:
                versiones_anteriores += 1
                continue
            if existente and tuple(existente) == valores:
                sin_cambios += 1
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
        cuarentena_total = conexion.execute(
            "SELECT COUNT(*) FROM cuarentena_licitaciones"
        ).fetchone()[0]
    conexion.close()

    ahora = datetime.now(ZoneInfo("Europe/Madrid")).isoformat()
    conciliados = (
        insertadas + actualizadas + sin_cambios
        + versiones_anteriores + descartadas
    )
    resultado = {
        "modo": "auditoria_historica" if desde_inicio else "incremental",
        "sincronizado_en": ahora,
        "feed_actualizado": fecha_feed,
        "checkpoint_anterior": checkpoint.isoformat(),
        "checkpoint_menores_anterior": checkpoint_menores.isoformat(),
        "paginas": len(paginas),
        "versiones_leidas": len(filas),
        "ids_revisados": len(ultimas),
        "insertadas": insertadas,
        "actualizadas": actualizadas,
        "sin_cambios": sin_cambios,
        "versiones_anteriores_ignoradas": versiones_anteriores,
        "ubicaciones_heredadas": ubicaciones_heredadas,
        "bajas": bajas_aplicadas,
        "descartadas_filtro": descartadas,
        "descartadas_por_motivo": dict(sorted(motivos_descarte.items())),
        "cuarentena_revisada": cuarentena_revisada,
        "cuarentena_total": cuarentena_total,
        "conciliacion": {
            "ids_revisados": len(ultimas),
            "ids_conciliados": conciliados,
            "cuadra": conciliados == len(ultimas),
        },
        "total": total,
        "duplicados": duplicados,
        "feed_menores_actualizado": fecha_feed_menores,
        "versiones_menores_leidas": len(filas_menores),
    }
    if guardar_metadata:
        metadata = leer_metadata(metadata_path)
        metadata.update(
            {
                "feed_incremental_sincronizado_en": ahora,
                "feed_incremental_actualizado_hasta": fecha_feed,
                "feed_incremental_ultima_ejecucion": resultado,
                "feed_menores_sincronizado_en": ahora,
                "feed_menores_actualizado_hasta": fecha_feed_menores,
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
        help="Revisa ambas fuentes desde el 1 de enero de 2025.",
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
