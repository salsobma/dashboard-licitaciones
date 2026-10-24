from __future__ import annotations

import argparse
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from feed_parser import NAMESPACES, fila_desde_entrada
from sincronizar_licitaciones import (
    cargar_maestro, clave_texto, codigo_postal, inicializar_esquema,
)


BASE_DIR = Path(__file__).resolve().parent


def archivos_atom(carpetas: list[Path]) -> list[Path]:
    archivos = []
    for carpeta in carpetas:
        if carpeta.is_file():
            archivos.append(carpeta)
            continue
        for patron in ("*.atom", "*.xml"):
            archivos.extend(carpeta.glob(patron))
    return sorted(archivos, key=lambda p: p.name, reverse=True)


def catalogo_municipios(maestro: dict[str, dict[str, object]]):
    catalogo: dict[str, dict[str, object]] = {}
    ambiguos: set[str] = set()
    for referencia in maestro.values():
        nombre = str(referencia.get("municipio") or "").strip()
        clave = clave_texto(nombre)
        if len(clave) < 4:
            continue
        anterior = catalogo.get(clave)
        if anterior and clave_texto(anterior.get("provincia")) != clave_texto(referencia.get("provincia")):
            ambiguos.add(clave)
        else:
            catalogo.setdefault(clave, referencia)
    return [
        (clave, referencia)
        for clave, referencia in sorted(catalogo.items(), key=lambda x: len(x[0]), reverse=True)
        if clave not in ambiguos
    ]


def backfill_catalogo(db_path: Path, maestro: dict[str, dict[str, object]]) -> Counter[str]:
    catalogo = catalogo_municipios(maestro)
    nuts_provincia = {"ES521": ("alicante", "alacant"), "ES522": ("castellon", "castello"), "ES523": ("valencia",)}
    cambios: Counter[str] = Counter()
    with sqlite3.connect(db_path) as conexion:
        filas = conexion.execute(
            "SELECT id, titulo, organo_contratante, codigo_nuts, provincia "
            "FROM licitaciones WHERE origen='perfil_plataforma' "
            "AND (municipio IS NULL OR trim(municipio)='')"
        ).fetchall()
        for lic_id, titulo, organo, nuts, provincia in filas:
            organo_clave = f" {clave_texto(organo)} "
            if not any(x in organo_clave for x in (" ayuntamiento ", " ajuntament ", " alcaldia ", " alcalde ", " pleno ", " junta de gobierno local ", " concejal")):
                continue
            titulo_clave = f" {clave_texto(titulo)} "
            provincia_clave = clave_texto(provincia)
            prefijo_nuts = str(nuts or "").upper()[:5]
            for municipio_clave, referencia in catalogo:
                patron = f" {municipio_clave} "
                if patron not in organo_clave:
                    continue
                ref_provincia = clave_texto(referencia.get("provincia"))
                nombres_esperados = nuts_provincia.get(prefijo_nuts, ())
                if nombres_esperados and not any(x in ref_provincia for x in nombres_esperados):
                    continue
                if provincia_clave and not any(
                    x in provincia_clave and x in ref_provincia
                    for x in ("alicante", "alacant", "castellon", "castello", "valencia")
                ):
                    continue
                citado = patron in titulo_clave
                fuente = "titulo_y_organo_municipal" if citado else "organo_municipal_catalogo"
                confianza = "alta" if citado else "media"
                conexion.execute(
                    "UPDATE licitaciones SET municipio=?, latitud=?, longitud=?, "
                    "municipio_fuente=?, municipio_confianza=? WHERE id=?",
                    (
                        referencia.get("municipio"), referencia.get("latitud"),
                        referencia.get("longitud"), fuente, confianza, lic_id,
                    ),
                )
                cambios[fuente] += 1
                break
        conexion.commit()
    return cambios


def normalizar_codigos_postales(
    db_path: Path, maestro: dict[str, dict[str, object]]
) -> int:
    cambios = 0
    with sqlite3.connect(db_path) as conexion:
        filas = conexion.execute(
            "SELECT id, codigo_postal, municipio FROM licitaciones"
        ).fetchall()
        for lic_id, cp_original, municipio_anterior in filas:
            referencia = maestro.get(codigo_postal(cp_original))
            if not referencia:
                continue
            municipio = str(referencia.get("municipio") or "").strip()
            if not municipio:
                continue
            conexion.execute(
                "UPDATE licitaciones SET municipio=?, provincia=?, "
                "comunidad_autonoma=?, latitud=?, longitud=? WHERE id=?",
                (
                    municipio, referencia.get("provincia"),
                    referencia.get("comunidad_autonoma"),
                    referencia.get("latitud"), referencia.get("longitud"), lic_id,
                ),
            )
            cambios += municipio != str(municipio_anterior or "").strip()
        conexion.commit()
    return cambios


def reprocesar(db_path: Path, carpetas: list[Path], sin_xml: bool = False) -> dict[str, object]:
    maestro = cargar_maestro()
    with sqlite3.connect(db_path) as conexion:
        inicializar_esquema(conexion)
        conexion.commit()
    normalizados_cp = normalizar_codigos_postales(db_path, maestro)
    cambios_catalogo = backfill_catalogo(db_path, maestro)
    if sin_xml:
        with sqlite3.connect(db_path) as conexion:
            restantes = conexion.execute(
                "SELECT count(*) FROM licitaciones WHERE origen='perfil_plataforma' "
                "AND (municipio IS NULL OR trim(municipio)='')"
            ).fetchone()[0]
        return {
            "municipios_normalizados_por_cp": normalizados_cp,
            "recuperados_catalogo": dict(cambios_catalogo),
            "restantes": restantes,
        }
    with sqlite3.connect(db_path) as conexion:
        objetivos = {
            fila[0]: str(fila[1] or "")
            for fila in conexion.execute(
                "SELECT id, fecha_actualizacion FROM licitaciones "
                "WHERE origen='perfil_plataforma' "
                "AND (municipio IS NULL OR trim(municipio)='')"
            )
        }
        conexion.commit()

    mejores: dict[str, dict[str, object]] = {}
    exactos: set[str] = set()
    errores = 0
    archivos = archivos_atom(carpetas)
    for numero, archivo in enumerate(archivos, 1):
        try:
            for _, elemento in ET.iterparse(archivo, events=("end",)):
                if elemento.tag != f'{{{NAMESPACES["atom"]}}}entry':
                    continue
                lic_id = elemento.findtext("atom:id", namespaces=NAMESPACES)
                if lic_id in objetivos and lic_id not in exactos:
                    fila = fila_desde_entrada(elemento)
                    if fila:
                        fecha = str(fila.get("fecha_actualizacion") or "")
                        anterior = mejores.get(lic_id)
                        if anterior is None or fecha > str(anterior.get("fecha_actualizacion") or ""):
                            mejores[lic_id] = fila
                        if fecha == objetivos[lic_id]:
                            exactos.add(lic_id)
                elemento.clear()
        except (ET.ParseError, OSError):
            errores += 1
        if numero % 100 == 0:
            print(
                f"Procesados {numero}/{len(archivos)} archivos; "
                f"localizados {len(mejores)}/{len(objetivos)} expedientes."
            )
        if len(exactos) == len(objetivos):
            break

    fuentes: Counter[str] = Counter()
    recuperados = 0
    with sqlite3.connect(db_path) as conexion:
        for lic_id, fila in mejores.items():
            municipio = str(fila.get("municipio") or "").strip()
            if not municipio:
                continue
            cp = codigo_postal(fila.get("codigo_postal"))
            referencia = maestro.get(cp)
            if referencia:
                fila.update(referencia)
            conexion.execute(
                """
                UPDATE licitaciones SET
                    codigo_postal=?, municipio=?, provincia=?, codigo_nuts=?,
                    comunidad_autonoma=?, latitud=?, longitud=?,
                    municipio_organo=?, codigo_postal_organo=?,
                    municipio_fuente=?, municipio_confianza=?
                WHERE id=? AND (municipio IS NULL OR trim(municipio)='')
                """,
                (
                    fila.get("codigo_postal"), fila.get("municipio"),
                    fila.get("provincia"), fila.get("codigo_nuts"),
                    fila.get("comunidad_autonoma") or "Comunitat Valenciana",
                    fila.get("latitud"), fila.get("longitud"),
                    fila.get("municipio_organo"), fila.get("codigo_postal_organo"),
                    fila.get("municipio_fuente"), fila.get("municipio_confianza"),
                    lic_id,
                ),
            )
            if conexion.total_changes > recuperados:
                recuperados += 1
                fuentes[str(fila.get("municipio_fuente") or "sin_fuente")] += 1
        conexion.commit()
        restantes = conexion.execute(
            "SELECT count(*) FROM licitaciones WHERE origen='perfil_plataforma' "
            "AND (municipio IS NULL OR trim(municipio)='')"
        ).fetchone()[0]
    return {
        "objetivos": len(objetivos),
        "localizados": len(mejores),
        "version_exacta": len(exactos),
        "recuperados": recuperados,
        "restantes": restantes,
        "fuentes": dict(fuentes),
        "recuperados_catalogo": dict(cambios_catalogo),
        "municipios_normalizados_por_cp": normalizados_cp,
        "archivos_con_error": errores,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=BASE_DIR / "licitaciones.db")
    parser.add_argument("--sin-xml", action="store_true")
    parser.add_argument("carpetas", nargs="*", type=Path)
    args = parser.parse_args()
    print(reprocesar(args.db, args.carpetas, sin_xml=args.sin_xml))
