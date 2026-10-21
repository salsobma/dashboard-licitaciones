
# Dashboard de licitaciones

Dashboard Streamlit para consultar licitaciones de la Plataforma de ContrataciÃ³n del Sector PÃºblico.

## EjecuciÃ³n

```bash
python -m venv .venv
pip install -r requirements.txt
streamlit run app.py
```

La base de datos se busca en `licitaciones.db`. Puede indicarse otra ruta con `LICITACIONES_DB_PATH`.

| Variable | Uso | Predeterminado |
| --- | --- | --- |
| `LICITACIONES_DB_PATH` | Ruta de SQLite | `licitaciones.db` |
| `DATA_CACHE_TTL_SECONDS` | CachÃ© de lectura | `300` |

## Decisiones de seguridad

- La web abre SQLite en modo de solo lectura.
- Las URL externas se validan y solo admiten HTTP/HTTPS.
- Los datos se muestran mediante componentes nativos, sin interpolaciÃ³n en HTML.
- La interfaz pÃºblica no ejecuta modelos de IA ni permite generar costes anÃ³nimos.
- Los resÃºmenes generados previamente se siguen mostrando.

## ActualizaciÃ³n

## Familias CPV incluidas

La base conserva expedientes de la Comunitat Valenciana pertenecientes a las
divisiones `71` (servicios de arquitectura e ingeniería) y `45` (trabajos de
construcción). El dashboard permite consultar una familia, la otra o ambas.
El histórico comienza el 1 de enero de 2025 según la fecha relevante del
estado contractual, no según la mera fecha técnica de actualización del feed.

## Identidad de las empresas adjudicatarias

El parser conserva para cada adjudicatario el identificador publicado por
PLACSP, su tipo (`NIF`, `OTROS` o `UTE`), el nombre original, el lote y el
resultado. El dashboard agrupa las variantes que comparten identificador y
muestra como nombre canónico la denominación más frecuente; en caso de empate
prevalece la observada más recientemente. Los nombres originales permanecen en
`adjudicatarios_detalle`. Si la fuente no publica identificador, no se inventa
una equivalencia y se utiliza el nombre normalizado.

`ingestar_todo.py` procesa los ficheros de `datos_atom`. La aplicaciÃ³n incluye la fecha de modificaciÃ³n de SQLite en la clave de cachÃ©, por lo que detecta una base actualizada sin reiniciar el servicio.
