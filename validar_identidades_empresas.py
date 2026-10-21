from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def normalizar_id(valor: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(valor or "").upper())


def auditar(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conexion:
        filas = conexion.execute(
            "SELECT origen, adjudicatario, adjudicatarios_detalle FROM licitaciones"
        ).fetchall()

    con_adjudicatario = 0
    con_detalle = 0
    con_identificador = 0
    por_origen = defaultdict(Counter)
    aliases = defaultdict(Counter)
    tipos = Counter()
    nombres_a_ids = defaultdict(set)
    for origen, adjudicatario, detalle_json in filas:
        if str(adjudicatario or "").strip():
            con_adjudicatario += 1
            por_origen[origen]["con_adjudicatario"] += 1
        try:
            detalles = json.loads(detalle_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            detalles = []
        if detalles:
            con_detalle += 1
            por_origen[origen]["con_detalle"] += 1
        ids_fila = set()
        vistos = set()
        for detalle in detalles if isinstance(detalles, list) else []:
            if not isinstance(detalle, dict):
                continue
            identificador = normalizar_id(detalle.get("identificador"))
            nombre = str(detalle.get("nombre") or "").strip()
            tipo = str(detalle.get("tipo_identificador") or "ID").upper()
            if not identificador:
                continue
            ids_fila.add(identificador)
            tipos[tipo] += 1
            firma = (identificador, nombre)
            if firma in vistos:
                continue
            vistos.add(firma)
            if nombre:
                aliases[identificador][nombre] += 1
                nombres_a_ids[nombre.upper()].add(identificador)
        if ids_fila:
            con_identificador += 1
            por_origen[origen]["con_identificador"] += 1

    ids_con_variantes = {
        identificador: dict(conteo.most_common())
        for identificador, conteo in aliases.items()
        if len(conteo) > 1
    }
    nombres_con_varios_ids = {
        nombre: sorted(ids_)
        for nombre, ids_ in nombres_a_ids.items()
        if len(ids_) > 1
    }
    return {
        "registros": len(filas),
        "registros_con_adjudicatario": con_adjudicatario,
        "registros_con_detalle": con_detalle,
        "registros_con_identificador_fiscal": con_identificador,
        "cobertura_identificador_sobre_adjudicatarios_pct": round(
            con_identificador * 100 / con_adjudicatario, 2
        ) if con_adjudicatario else 0,
        "identificadores_unicos": len(aliases),
        "identificadores_con_variantes_de_nombre": len(ids_con_variantes),
        "nombres_compartidos_por_varios_identificadores": len(
            nombres_con_varios_ids
        ),
        "tipos_identificador": dict(tipos.most_common()),
        "por_origen": {
            origen: dict(conteo) for origen, conteo in por_origen.items()
        },
        "ejemplos_variantes": dict(list(sorted(
            ids_con_variantes.items(),
            key=lambda item: (-len(item[1]), item[0]),
        ))[:25]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("licitaciones.db"))
    parser.add_argument("--informe", type=Path)
    args = parser.parse_args()
    resultado = auditar(args.db)
    texto = json.dumps(resultado, ensure_ascii=False, indent=2) + "\n"
    if args.informe:
        args.informe.write_text(texto, encoding="utf-8")
    print(texto)


if __name__ == "__main__":
    main()
