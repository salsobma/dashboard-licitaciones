from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from sincronizar_licitaciones import (
    COLUMNAS_FUENTE,
    CPV_PREFIJOS_ADMITIDOS,
    DB_PATH,
    fecha_relevante_registro,
)


def codigos_cpv(valor: object) -> tuple[str, ...]:
    return tuple(c.strip() for c in str(valor or "").split(",") if c.strip())


def normalizar_comunidad(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conexion:
        actualizadas = conexion.execute(
            "UPDATE licitaciones SET comunidad_autonoma='Comunitat Valenciana' "
            "WHERE comunidad_autonoma IS NULL "
            "OR comunidad_autonoma NOT LIKE '%Valenc%'"
        ).rowcount
        conexion.commit()
    return actualizadas


def auditar(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conexion:
        filas = conexion.execute(
            f"SELECT {','.join(COLUMNAS_FUENTE)} FROM licitaciones"
        ).fetchall()
        cuarentena = dict(conexion.execute(
            "SELECT motivo, COUNT(*) FROM cuarentena_licitaciones GROUP BY motivo"
        ).fetchall())
        duplicados = conexion.execute(
            "SELECT COUNT(*) FROM (SELECT id FROM licitaciones "
            "GROUP BY id HAVING COUNT(*) > 1)"
        ).fetchone()[0]

    por_origen: Counter[str] = Counter()
    por_familia: Counter[str] = Counter()
    por_anio: Counter[str] = Counter()
    anteriores_inicio = 0
    sin_fecha_relevante = 0
    fuera_cpv = 0
    fuera_cv = 0
    ambas_familias = 0

    for valores in filas:
        fila = dict(zip(COLUMNAS_FUENTE, valores))
        por_origen[str(fila.get("origen") or "")] += 1
        codigos = codigos_cpv(fila.get("cpv"))
        familias = {
            prefijo
            for prefijo in CPV_PREFIJOS_ADMITIDOS
            if any(codigo.startswith(prefijo) for codigo in codigos)
        }
        por_familia.update(familias)
        ambas_familias += len(familias) > 1
        fuera_cpv += not bool(familias)
        fuera_cv += "valenc" not in str(
            fila.get("comunidad_autonoma") or ""
        ).lower()
        fecha = fecha_relevante_registro(fila)
        if fecha is None:
            sin_fecha_relevante += 1
        else:
            por_anio[str(fecha.year)] += 1
            anteriores_inicio += fecha.year < 2025

    return {
        "total": len(filas),
        "por_origen": dict(sorted(por_origen.items())),
        "por_familia_cpv": dict(sorted(por_familia.items())),
        "con_cpv_71_y_45": ambas_familias,
        "por_anio_relevante": dict(sorted(por_anio.items())),
        "fecha_relevante_anterior_2025": anteriores_inicio,
        "sin_fecha_relevante": sin_fecha_relevante,
        "duplicados": duplicados,
        "fuera_cpv_71_45": fuera_cpv,
        "fuera_comunitat_valenciana": fuera_cv,
        "cuarentena_por_motivo": cuarentena,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--informe", type=Path)
    parser.add_argument("--normalizar-comunidad", action="store_true")
    args = parser.parse_args()
    db_path = args.db.resolve()
    normalizadas = normalizar_comunidad(db_path) if args.normalizar_comunidad else 0
    resultado = auditar(db_path)
    resultado["comunidades_normalizadas_en_esta_ejecucion"] = normalizadas
    contenido = json.dumps(resultado, ensure_ascii=False, indent=2) + "\n"
    if args.informe:
        args.informe.resolve().write_text(contenido, encoding="utf-8")
    print(contenido, end="")


if __name__ == "__main__":
    main()
