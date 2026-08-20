from __future__ import annotations

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from feed_parser import NAMESPACES, fila_desde_entrada
from sincronizar_licitaciones import (
    COLUMNAS_FUENTE,
    DB_PATH,
    MOTIVOS_CUARENTENA,
    cargar_maestro,
    evaluar_y_enriquecer,
    guardar_cuarentena,
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


def motivo_descarte_preliminar(entrada: ET.Element) -> str | None:
    """Evita extraer todos los campos de entradas claramente fuera de alcance."""
    status = entrada.find("cac-place-ext:ContractFolderStatus", NAMESPACES)
    proyecto = (
        status.find("cac:ProcurementProject", NAMESPACES)
        if status is not None
        else None
    )
    if proyecto is None:
        return "estructura_incompleta"
    cpvs = [
        (nodo.text or "").strip()
        for nodo in proyecto.findall(
            "cac:RequiredCommodityClassification/cbc:ItemClassificationCode",
            NAMESPACES,
        )
    ]
    if not any(cpvs):
        return "sin_cpv"
    if not any(cpv.startswith("71") for cpv in cpvs):
        return "cpv_fuera_71"
    return None


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
    motivos_descarte: Counter[str] = Counter()
    revisado_en = datetime.now(ZoneInfo("Europe/Madrid")).isoformat()
    with sqlite3.connect(db_path) as conexion:
        inicializar_esquema(conexion)
        if reemplazar:
            conexion.execute("DELETE FROM licitaciones WHERE origen = ?", (ORIGEN,))
            conexion.execute(
                "DELETE FROM cuarentena_licitaciones WHERE origen = ?", (ORIGEN,)
            )
            conexion.commit()

        for indice, archivo in enumerate(archivos, start=1):
            try:
                for entrada in entradas_atom(archivo):
                    leidas += 1
                    motivo_preliminar = motivo_descarte_preliminar(entrada)
                    if motivo_preliminar:
                        descartadas += 1
                        motivos_descarte[motivo_preliminar] += 1
                        continue
                    fila = fila_desde_entrada(entrada)
                    if fila is None:
                        errores += 1
                        motivos_descarte["estructura_incompleta"] += 1
                        continue
                    fila["origen"] = ORIGEN
                    fila_evaluada, motivo = evaluar_y_enriquecer(fila, maestro)
                    if fila_evaluada is None:
                        descartadas += 1
                        motivo = motivo or "motivo_desconocido"
                        motivos_descarte[motivo] += 1
                        if motivo in MOTIVOS_CUARENTENA:
                            guardar_cuarentena(
                                conexion, fila, motivo, revisado_en
                            )
                        continue
                    fila = fila_evaluada
                    conexion.execute(
                        "DELETE FROM cuarentena_licitaciones "
                        "WHERE id = ? AND origen = ?",
                        (fila["id"], ORIGEN),
                    )
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
        cuarentena_total = conexion.execute(
            "SELECT COUNT(*) FROM cuarentena_licitaciones WHERE origen = ?",
            (ORIGEN,),
        ).fetchone()[0]

    conciliadas = validas + descartadas + errores
    return {
        "finalizado_en": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "archivos": len(archivos),
        "entradas_leidas": leidas,
        "entradas_validas": validas,
        "entradas_descartadas": descartadas,
        "descartadas_por_motivo": dict(sorted(motivos_descarte.items())),
        "entradas_sin_datos": errores,
        "cuarentena_total": cuarentena_total,
        "conciliacion": {
            "entradas_leidas": leidas,
            "entradas_conciliadas": conciliadas,
            "cuadra": conciliadas == leidas,
        },
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
    parser.add_argument(
        "--informe",
        type=Path,
        default=Path(__file__).with_name("auditoria_contratos_menores.json"),
        help="Ruta del informe JSON de cobertura.",
    )
    args = parser.parse_args()
    resultado = ingestar(
        args.db.resolve(),
        [directorio.resolve() for directorio in args.directorios],
        reemplazar=args.reemplazar,
    )
    informe = args.informe.resolve()
    informe.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
