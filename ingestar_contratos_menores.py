from __future__ import annotations

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from feed_parser import NAMESPACES, fila_desde_entrada, texto_xml
from sincronizar_licitaciones import (
    COLUMNAS_FUENTE,
    DB_PATH,
    cargar_maestro,
    clave_texto,
    codigo_postal,
    enriquecer_y_filtrar,
    inicializar_esquema,
    valor_sqlite,
)


ORIGEN = "contrato_menor"
ATOM_ENTRY = f"{{{NAMESPACES['atom']}}}entry"


def archivos_atom(directorios: list[Path]) -> list[Path]:
    archivos: list[Path] = []
    for directorio in directorios:
        if not directorio.is_dir():
            raise FileNotFoundError(f"No existe el directorio: {directorio}")
        archivos.extend(directorio.glob("*.atom"))
    return sorted(archivos, key=lambda ruta: (ruta.name, str(ruta.parent)))


def entradas_atom(ruta: Path):
    contexto = ET.iterparse(ruta, events=("end",))
    for _, elemento in contexto:
        if elemento.tag == ATOM_ENTRY:
            yield elemento
            elemento.clear()


def entrada_candidata(
    entrada: ET.Element, maestro: dict[str, dict[str, object]]
) -> bool:
    """Descarta pronto el grueso del feed antes de extraer todos sus campos."""
    status = entrada.find("cac-place-ext:ContractFolderStatus", NAMESPACES)
    proyecto = (
        status.find("cac:ProcurementProject", NAMESPACES)
        if status is not None
        else None
    )
    if proyecto is None:
        return False
    cpvs = (
        (nodo.text or "").strip()
        for nodo in proyecto.findall(
            "cac:RequiredCommodityClassification/cbc:ItemClassificationCode",
            NAMESPACES,
        )
    )
    if not any(cpv.startswith("71") for cpv in cpvs):
        return False

    provincia = clave_texto(
        texto_xml(proyecto, "cac:RealizedLocation/cbc:CountrySubentity")
    )
    if provincia in {"alicante", "castellon", "castello", "valencia"}:
        return True

    cp = codigo_postal(
        texto_xml(proyecto, "cac:RealizedLocation/cac:Address/cbc:PostalZone")
    )
    if not cp:
        party = status.find(
            "cac-place-ext:LocatedContractingParty/cac:Party", NAMESPACES
        )
        cp = codigo_postal(texto_xml(party, "cac:PostalAddress/cbc:PostalZone"))
    ubicacion = maestro.get(cp)
    return bool(
        ubicacion
        and "valenc" in clave_texto(ubicacion.get("comunidad_autonoma"))
    )


def ingestar(
    db_path: Path,
    directorios: list[Path],
    reemplazar: bool = False,
) -> dict[str, object]:
    archivos = archivos_atom(directorios)
    if not archivos:
        raise RuntimeError("No se encontraron archivos ATOM.")

    maestro = cargar_maestro()
    marcadores = ",".join("?" for _ in COLUMNAS_FUENTE)
    actualizaciones = ",".join(
        f"{columna}=excluded.{columna}"
        for columna in COLUMNAS_FUENTE
        if columna != "id"
    )
    sql = f"""
        INSERT INTO licitaciones ({','.join(COLUMNAS_FUENTE)})
        VALUES ({marcadores})
        ON CONFLICT(id) DO UPDATE SET {actualizaciones}
        WHERE julianday(excluded.fecha_actualizacion) >=
              julianday(licitaciones.fecha_actualizacion)
           OR licitaciones.fecha_actualizacion IS NULL
    """

    leidas = 0
    validas = 0
    descartadas = 0
    errores = 0
    with sqlite3.connect(db_path) as conexion:
        inicializar_esquema(conexion)
        if reemplazar:
            conexion.execute("DELETE FROM licitaciones WHERE origen = ?", (ORIGEN,))
            conexion.commit()

        for indice, archivo in enumerate(archivos, start=1):
            try:
                for entrada in entradas_atom(archivo):
                    leidas += 1
                    if not entrada_candidata(entrada, maestro):
                        descartadas += 1
                        continue
                    fila = fila_desde_entrada(entrada)
                    if fila is None:
                        errores += 1
                        continue
                    fila["origen"] = ORIGEN
                    fila = enriquecer_y_filtrar(fila, maestro)
                    if fila is None:
                        descartadas += 1
                        continue
                    valores = tuple(
                        valor_sqlite(fila.get(columna))
                        for columna in COLUMNAS_FUENTE
                    )
                    conexion.execute(sql, valores)
                    validas += 1
            except ET.ParseError as error:
                raise RuntimeError(f"ATOM no válido: {archivo}: {error}") from error

            if indice % 25 == 0:
                conexion.commit()
                print(
                    json.dumps(
                        {
                            "archivos": f"{indice}/{len(archivos)}",
                            "entradas_leidas": leidas,
                            "entradas_validas": validas,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        conexion.commit()
        total = conexion.execute(
            "SELECT COUNT(*) FROM licitaciones WHERE origen = ?", (ORIGEN,)
        ).fetchone()[0]
        duplicados = conexion.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT id FROM licitaciones WHERE origen = ? "
            "GROUP BY id HAVING COUNT(*) > 1)",
            (ORIGEN,),
        ).fetchone()[0]
        cpvs_guardados = conexion.execute(
            "SELECT cpv FROM licitaciones WHERE origen = ?", (ORIGEN,)
        ).fetchall()
        fuera_cpv = sum(
            not any(
                codigo.strip().startswith("71")
                for codigo in str(cpv or "").split(",")
            )
            for (cpv,) in cpvs_guardados
        )
        fuera_cv = conexion.execute(
            "SELECT COUNT(*) FROM licitaciones WHERE origen = ? "
            "AND comunidad_autonoma NOT LIKE '%Valenc%'",
            (ORIGEN,),
        ).fetchone()[0]

    return {
        "finalizado_en": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "archivos": len(archivos),
        "entradas_leidas": leidas,
        "entradas_validas": validas,
        "entradas_descartadas": descartadas,
        "entradas_sin_datos": errores,
        "contratos_menores": total,
        "duplicados": duplicados,
        "fuera_cpv_71": fuera_cpv,
        "fuera_comunitat_valenciana": fuera_cv,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Carga el histórico ATOM de contratos menores."
    )
    parser.add_argument("directorios", nargs="+", type=Path)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--reemplazar",
        action="store_true",
        help="Elimina antes los contratos menores existentes.",
    )
    args = parser.parse_args()
    resultado = ingestar(
        args.db.resolve(),
        [directorio.resolve() for directorio in args.directorios],
        reemplazar=args.reemplazar,
    )
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
