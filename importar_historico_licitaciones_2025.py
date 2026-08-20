from __future__ import annotations

import argparse
import json
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from ingestar_contratos_menores import ingestar
from sincronizar_licitaciones import DB_PATH, METADATA_PATH, leer_metadata


URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3_2025.zip"
)


def descargar(destino: Path) -> None:
    cabeceras = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": "https://www.hacienda.gob.es/",
    }
    errores = []
    for espera in (0, 30, 120):
        if espera:
            time.sleep(espera)
        try:
            with requests.get(URL, headers=cabeceras, timeout=300, stream=True) as respuesta:
                respuesta.raise_for_status()
                with destino.open("wb") as salida:
                    for bloque in respuesta.iter_content(1024 * 1024):
                        if bloque:
                            salida.write(bloque)
            if not zipfile.is_zipfile(destino):
                muestra = destino.read_bytes()[:300].decode("utf-8", errors="replace")
                raise RuntimeError(f"la fuente no devolvió un ZIP: {muestra}")
            return
        except Exception as error:
            errores.append(str(error))
    raise RuntimeError(f"No se pudo descargar el histórico 2025: {errores[-1]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--metadata", type=Path, default=METADATA_PATH)
    parser.add_argument(
        "--informe",
        type=Path,
        default=Path(__file__).with_name("auditoria_licitaciones_2025.json"),
    )
    parser.add_argument("--forzar", action="store_true")
    args = parser.parse_args()

    metadata = leer_metadata(args.metadata)
    if metadata.get("historico_licitaciones_2025_cargado") and not args.forzar:
        print("El histórico de licitaciones 2025 ya está cargado.")
        return

    with tempfile.TemporaryDirectory(prefix="licitaciones_2025_") as temporal:
        raiz = Path(temporal)
        comprimido = raiz / "licitaciones_2025.zip"
        descargar(comprimido)
        extraido = raiz / "atom"
        with zipfile.ZipFile(comprimido) as archivo:
            archivo.extractall(extraido)
        directorios = sorted({ruta.parent for ruta in extraido.rglob("*.atom")})
        if not directorios:
            raise RuntimeError("El ZIP oficial no contiene archivos ATOM.")
        resultado = ingestar(
            args.db.resolve(), directorios, reemplazar=False,
            origen="perfil_plataforma",
        )

    args.informe.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    metadata = leer_metadata(args.metadata)
    metadata.update({
        "historico_licitaciones_2025_cargado": True,
        "historico_licitaciones_2025_cargado_en": datetime.now(
            ZoneInfo("Europe/Madrid")
        ).isoformat(),
    })
    args.metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
