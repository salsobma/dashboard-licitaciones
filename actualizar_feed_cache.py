from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from feed_parser import procesar_paginas


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "feed_cache"
FEED_URLS = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
    "https://contrataciondelestado.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
)
MAX_PAGINAS = 4
ATOM_FEED = "{http://www.w3.org/2005/Atom}feed"
ATOM_UPDATED = "{http://www.w3.org/2005/Atom}updated"
ATOM_LINK = "{http://www.w3.org/2005/Atom}link"
DOMINIOS_PERMITIDOS = {
    "contrataciondelsectorpublico.gob.es",
    "contrataciondelestado.es",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://contrataciondelsectorpublico.gob.es/datosabiertos",
    "Cache-Control": "no-cache",
}


def variantes_url(url: str) -> list[str]:
    variantes = [url]
    dominios = (
        "contrataciondelsectorpublico.gob.es",
        "contrataciondelestado.es",
    )
    for origen in dominios:
        if origen in url:
            for destino in dominios:
                candidata = url.replace(origen, destino)
                if candidata not in variantes:
                    variantes.append(candidata)
    return variantes


def descargar_pagina(url: str) -> tuple[bytes, ET.Element, str]:
    if urlparse(url).hostname not in DOMINIOS_PERMITIDOS:
        raise RuntimeError("El feed enlaza a un dominio no permitido.")
    errores: list[str] = []
    for ronda in range(2):
        for candidata in variantes_url(url):
            try:
                respuesta = requests.get(candidata, headers=HEADERS, timeout=45)
                respuesta.raise_for_status()
                contenido = respuesta.content
                if b"Web Application Firewall" in contenido[:4096]:
                    raise RuntimeError("el cortafuegos rechazó temporalmente la consulta")
                raiz = ET.fromstring(contenido)
                if raiz.tag != ATOM_FEED:
                    raise RuntimeError("la respuesta no contiene un feed ATOM válido")
                return contenido, raiz, candidata
            except Exception as error:
                errores.append(f"{candidata}: {error}")
        if ronda == 0:
            time.sleep(2)
    raise RuntimeError(
        "El feed oficial no está disponible. "
        f"Último intento: {errores[-1] if errores else 'error desconocido'}"
    )


def enlace_siguiente(raiz: ET.Element, url_real: str) -> str | None:
    for enlace in raiz.findall(ATOM_LINK):
        if enlace.attrib.get("rel") == "next" and enlace.attrib.get("href"):
            siguiente = urljoin(url_real, enlace.attrib["href"])
            if urlparse(siguiente).hostname not in DOMINIOS_PERMITIDOS:
                raise RuntimeError("La página siguiente usa un dominio no permitido.")
            return siguiente
    return None


def generar_snapshot(destino: Path) -> dict[str, object]:
    destino.mkdir(parents=True, exist_ok=True)
    siguiente: str | None = FEED_URLS[0]
    visitadas: set[str] = set()
    paginas: list[dict[str, object]] = []
    raices: list[ET.Element] = []
    fecha_feed: str | None = None

    while siguiente and len(paginas) < MAX_PAGINAS:
        if siguiente in visitadas:
            break
        visitadas.add(siguiente)
        contenido, raiz, url_real = descargar_pagina(siguiente)
        numero = len(paginas) + 1
        raices.append(raiz)
        if fecha_feed is None:
            nodo_fecha = raiz.find(ATOM_UPDATED)
            fecha_feed = nodo_fecha.text.strip() if nodo_fecha is not None and nodo_fecha.text else None
        paginas.append({"numero": numero, "url": url_real, "bytes": len(contenido)})
        siguiente = enlace_siguiente(raiz, url_real)

    if not paginas:
        raise RuntimeError("No se ha descargado ninguna página del feed.")

    filas, bajas = procesar_paginas(raices)
    snapshot = {"filas": filas, "bajas": bajas}
    (destino / "feed.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifiesto: dict[str, object] = {
        "version": 1,
        "fecha_feed": fecha_feed,
        "sincronizado_en": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "paginas": paginas,
        "completo": siguiente is None or len(paginas) == MAX_PAGINAS,
        "registros": len(filas),
        "bajas": len(bajas),
    }
    (destino / "manifest.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifiesto


def publicar_snapshot(temporal: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    nuevos = {archivo.name for archivo in temporal.iterdir() if archivo.is_file()}
    for archivo in temporal.iterdir():
        if archivo.is_file():
            os.replace(archivo, CACHE_DIR / archivo.name)
    for anterior in CACHE_DIR.iterdir():
        if anterior.is_file() and anterior.name not in nuevos:
            anterior.unlink()


def main() -> None:
    temporal = Path(tempfile.mkdtemp(prefix="feed_cache_", dir=BASE_DIR))
    try:
        manifiesto = generar_snapshot(temporal)
        publicar_snapshot(temporal)
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
    print(
        "Feed validado y guardado: "
        f"{len(manifiesto['paginas'])} páginas, fecha {manifiesto['fecha_feed']}"
    )


if __name__ == "__main__":
    main()
