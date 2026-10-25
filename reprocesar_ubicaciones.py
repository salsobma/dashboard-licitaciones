from __future__ import annotations

import argparse
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pandas as pd

from feed_parser import NAMESPACES, fila_desde_entrada
from sincronizar_licitaciones import (
    cargar_maestro, clave_texto, codigo_postal, inicializar_esquema,
)


BASE_DIR = Path(__file__).resolve().parent


def texto_busqueda(valor: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", clave_texto(valor)))


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
    referencias = {
        clave_texto(referencia.get("municipio")): referencia
        for referencia in maestro.values()
    }
    aliases: dict[str, list[dict[str, object]]] = {}
    provincias = {
        "03": "Alicante/Alacant",
        "12": "Castellón/Castelló",
        "46": "Valencia/València",
    }
    for provincia, nombre_provincia in provincias.items():
        tabla = pd.read_excel(
            BASE_DIR / "municipios_ine_2026.xlsx",
            sheet_name=provincia,
            header=2,
        )
        for _, fila in tabla.iterrows():
            oficial = str(fila.get("NOMBRE") or "").strip()
            referencia = dict(referencias.get(clave_texto(oficial), {}))
            referencia.update({"municipio": oficial, "provincia": nombre_provincia})
            variantes = {oficial, *oficial.split("/")}
            for variante in list(variantes):
                if "," in variante:
                    nombre, articulo = [parte.strip() for parte in variante.split(",", 1)]
                    variantes.add(f"{articulo} {nombre}")
            for variante in variantes:
                alias = texto_busqueda(variante)
                if len(alias) >= 4:
                    aliases.setdefault(alias, []).append(referencia)
    return [
        (alias, referencias_alias[0])
        for alias, referencias_alias in sorted(
            aliases.items(), key=lambda elemento: len(elemento[0]), reverse=True
        )
        if len({r["municipio"] for r in referencias_alias}) == 1
    ]


def provincia_compatible(
    referencia: dict[str, object], nuts: object, provincia: object
) -> bool:
    esperadas = {
        "ES521": ("alicante", "alacant"),
        "ES522": ("castellon", "castello"),
        "ES523": ("valencia",),
    }.get(str(nuts or "").upper()[:5], ())
    ref = clave_texto(referencia.get("provincia"))
    declarada = clave_texto(provincia)
    if esperadas and not any(nombre in ref for nombre in esperadas):
        return False
    return not declarada or any(
        nombre in declarada and nombre in ref
        for nombre in ("alicante", "alacant", "castellon", "castello", "valencia")
    )


def backfill_catalogo(db_path: Path, maestro: dict[str, dict[str, object]]) -> Counter[str]:
    catalogo = catalogo_municipios(maestro)
    cambios: Counter[str] = Counter()
    with sqlite3.connect(db_path) as conexion:
        filas = conexion.execute(
            "SELECT id, titulo, organo_contratante, codigo_nuts, provincia "
            "FROM licitaciones WHERE origen='perfil_plataforma' "
            "AND (municipio IS NULL OR trim(municipio)='')"
        ).fetchall()
        for lic_id, titulo, organo, nuts, provincia in filas:
            organo_clave = texto_busqueda(organo)
            titulo_clave = texto_busqueda(titulo)
            es_municipal = any(
                etiqueta in f" {organo_clave} "
                for etiqueta in (
                    " ayuntamiento ", " ajuntament ", " alcaldia ",
                    " alcalde ", " pleno ", " junta de gobierno",
                    " concejal", " teniente de alcalde ",
                )
            )
            coincidencias_titulo = {
                referencia["municipio"]
                for alias, referencia in catalogo
                if provincia_compatible(referencia, nuts, provincia)
                and re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                    titulo_clave,
                )
            }
            contexto_multiple = len(coincidencias_titulo) >= 2 and (
                " municipios " in f" {titulo_clave} "
                or " t m " in f" {titulo_clave} "
            )
            encontrada = None
            fuente = confianza = None
            for municipio_clave, referencia in catalogo:
                if not provincia_compatible(referencia, nuts, provincia):
                    continue
                patron = rf"(?<![a-z0-9]){re.escape(municipio_clave)}(?![a-z0-9])"
                citado_organo = bool(re.search(patron, organo_clave))
                contexto_titulo = bool(re.search(
                    rf"\b(?:municipio|termino municipal|localidad)\s+(?:de\s+|del\s+)?{re.escape(municipio_clave)}\b",
                    titulo_clave,
                ))
                if es_municipal and citado_organo:
                    encontrada = referencia
                    fuente = (
                        "titulo_y_organo_municipal"
                        if re.search(patron, titulo_clave)
                        else "organo_municipal_catalogo"
                    )
                    confianza = "alta" if fuente.startswith("titulo_") else "media"
                    break
                if contexto_titulo and not contexto_multiple:
                    encontrada = referencia
                    fuente = "titulo_contexto_explicito"
                    confianza = "alta"
                    break
            if encontrada:
                conexion.execute(
                    "UPDATE licitaciones SET municipio=?, latitud=?, longitud=?, "
                    "municipio_fuente=?, municipio_confianza=?, "
                    "ambito_ubicacion='municipio_concreto' WHERE id=?",
                    (
                        encontrada.get("municipio"), encontrada.get("latitud"),
                        encontrada.get("longitud"), fuente, confianza, lic_id,
                    ),
                )
                cambios[fuente] += 1
        conexion.commit()
    return cambios


def clasificar_ambitos(
    db_path: Path, maestro: dict[str, dict[str, object]]
) -> Counter[str]:
    catalogo = catalogo_municipios(maestro)
    cambios: Counter[str] = Counter()
    with sqlite3.connect(db_path) as conexion:
        conexion.execute(
            "UPDATE licitaciones SET ambito_ubicacion='municipio_concreto' "
            "WHERE municipio IS NOT NULL AND trim(municipio)<>''"
        )
        filas = conexion.execute(
            "SELECT id, titulo, codigo_nuts, provincia FROM licitaciones "
            "WHERE origen='perfil_plataforma' "
            "AND (municipio IS NULL OR trim(municipio)='')"
        ).fetchall()
        for lic_id, titulo, nuts, provincia in filas:
            texto = texto_busqueda(titulo)
            coincidencias = {
                referencia["municipio"]
                for alias, referencia in catalogo
                if provincia_compatible(referencia, nuts, provincia)
                and re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", texto
                )
            }
            if any(frase in texto for frase in (
                "varios municipios", "diversos municipios",
                "distintos municipios", "multiples municipios",
            )) or (
                len(coincidencias) >= 2
                and (
                    " municipios " in f" {texto} "
                    or " t m " in f" {texto} "
                )
            ):
                ambito = "varios_municipios"
            elif any(frase in texto for frase in (
                "ambito provincial", "red provincial", "carreteras provinciales",
                "provincia de alicante", "provincia de valencia",
                "provincia de castellon",
            )):
                ambito = "ambito_provincial"
            else:
                ambito = (
                    "pendiente_identificar"
                    if len(coincidencias) == 1
                    else "municipio_no_publicado"
                )
            conexion.execute(
                "UPDATE licitaciones SET ambito_ubicacion=? WHERE id=?",
                (ambito, lic_id),
            )
            cambios[ambito] += 1
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
                "comunidad_autonoma=?, latitud=?, longitud=?, "
                "ambito_ubicacion='municipio_concreto' WHERE id=?",
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
    ambitos = clasificar_ambitos(db_path, maestro)
    if sin_xml:
        with sqlite3.connect(db_path) as conexion:
            restantes = conexion.execute(
                "SELECT count(*) FROM licitaciones WHERE origen='perfil_plataforma' "
                "AND (municipio IS NULL OR trim(municipio)='')"
            ).fetchone()[0]
        return {
            "municipios_normalizados_por_cp": normalizados_cp,
            "recuperados_catalogo": dict(cambios_catalogo),
            "ambitos_sin_municipio": dict(ambitos),
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
                    , ambito_ubicacion=?
                WHERE id=? AND (municipio IS NULL OR trim(municipio)='')
                """,
                (
                    fila.get("codigo_postal"), fila.get("municipio"),
                    fila.get("provincia"), fila.get("codigo_nuts"),
                    fila.get("comunidad_autonoma") or "Comunitat Valenciana",
                    fila.get("latitud"), fila.get("longitud"),
                    fila.get("municipio_organo"), fila.get("codigo_postal_organo"),
                    fila.get("municipio_fuente"), fila.get("municipio_confianza"),
                    fila.get("ambito_ubicacion"),
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
        "ambitos_sin_municipio": dict(ambitos),
        "archivos_con_error": errores,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=BASE_DIR / "licitaciones.db")
    parser.add_argument("--sin-xml", action="store_true")
    parser.add_argument("carpetas", nargs="*", type=Path)
    args = parser.parse_args()
    print(reprocesar(args.db, args.carpetas, sin_xml=args.sin_xml))
