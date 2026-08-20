# Auditoría de cobertura

## Contratos menores 2025-2026

Auditoría ejecutada el 20 de agosto de 2026 sobre los 1.779 archivos ATOM
históricos facilitados.

| Concepto | Resultado |
| --- | ---: |
| Entradas leídas | 886.562 |
| Entradas conciliadas | 886.562 |
| Conciliación | Correcta |
| Versiones válidas | 5.021 |
| Contratos únicos almacenados | 4.913 |
| Contratos recuperados mediante NUTS | 317 |
| Registros en cuarentena | 9 |
| Duplicados | 0 |
| Integridad SQLite | Correcta |

### Motivos de descarte

| Motivo | Versiones |
| --- | ---: |
| Sin CPV | 422.694 |
| CPV distinto de 71 | 437.471 |
| Fuera de la Comunitat Valenciana | 20.614 |
| Adjudicación anterior a 2025 | 751 |
| Sin adjudicatario | 11 |

Las 11 versiones sin adjudicatario corresponden a 9 expedientes únicos, que
se conservan en la tabla `cuarentena_licitaciones` de SQLite.

## Reglas de cobertura

- El filtro CPV reúne la clasificación general y la declarada en cada lote.
- La clasificación territorial revisa tanto la ubicación general como las
  ubicaciones de los lotes y prioriza cualquier evidencia valenciana. Después
  usa el código postal y el maestro `provincias.xlsx`, la provincia/comunidad
  indicada por el XML y el código NUTS `ES52`, `ES521`, `ES522` o `ES523`.
- Una actualización sin evidencia territorial hereda la ubicación de la
  versión anterior del mismo identificador. Los demás campos sí se actualizan.
- Una entrada nueva con CPV 71 pero sin evidencia territorial verificable se
  conserva en cuarentena.
- Cada sincronización informa de insertados, actualizados, registros sin
  cambios, versiones antiguas, ubicación heredada y descartes por motivo.
- GitHub Actions falla si la conciliación no cuadra, SQLite no supera la
  comprobación de integridad o aparecen identificadores duplicados.

El detalle estructurado de esta ejecución está disponible en
`auditoria_contratos_menores.json`.

## Licitaciones ordinarias 2026

Auditoría local ejecutada el 20 de agosto de 2026 sobre los 864 archivos ATOM
disponibles. Se leyeron y conciliaron 430.393 entradas. La base pasó de 1.650 a
1.775 licitaciones únicas válidas al incorporar CPV y ubicación por lote: se
recuperaron 125 expedientes. Hay cero duplicados y cero registros ordinarios en
cuarentena.

Después se conciliaron las versiones más recientes ya obtenidas mediante el
feed: se añadieron 18 expedientes y se actualizaron 73 sin perder los datos
recuperados de los ATOM. Tras retirar 20 expedientes anteriores al comienzo
acordado del histórico, la base ordinaria resultante contiene 1.773
licitaciones y llega hasta el 19 de agosto de 2026.

El detalle estructurado está disponible en `auditoria_licitaciones_2026.json`.

## Fechas contractuales recuperadas

Además de la fecha de adjudicación, se guardan el identificador del contrato,
la fecha de formalización y la fecha de inicio cuando la fuente los publica.
En esta auditoría se encontraron fechas de formalización en 370 contratos
menores y 1.091 licitaciones ordinarias.

## Automatización

Las sincronizaciones incrementales se ejecutan cuatro veces al día. Los
domingos se lanza automáticamente una auditoría histórica de ambas fuentes
desde el 1 de enero de 2025. No requiere archivos aportados manualmente, aunque
la ejecución depende de que PLACSP no esté en mantenimiento.
