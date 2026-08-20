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
| Sin CPV | 422.701 |
| CPV distinto de 71 | 437.466 |
| Fuera de la Comunitat Valenciana | 20.612 |
| Adjudicación anterior a 2025 | 751 |
| Sin adjudicatario | 11 |

Las 11 versiones sin adjudicatario corresponden a 9 expedientes únicos, que
se conservan en la tabla `cuarentena_licitaciones` de SQLite.

## Reglas de cobertura

- La clasificación territorial usa, en este orden, el código postal y el
  maestro `provincias.xlsx`, la provincia/comunidad indicada por el XML y,
  como última prueba, el código NUTS `ES52`, `ES521`, `ES522` o `ES523`.
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

## Auditoría del feed ordinario

La auditoría anual iniciada el 20 de agosto de 2026 fue interrumpida por la
Plataforma en una página del 23 de julio de 2026. La transacción se revirtió y
el checkpoint no avanzó. Debe reintentarse mediante el flujo programado hasta
obtener una conciliación completa.
