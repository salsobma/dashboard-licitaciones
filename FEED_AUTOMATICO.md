# Feed automático

El feed reciente se descarga y valida mediante GitHub Actions con el cron UTC
`0 7,11,15,19 * * *`. Durante el horario de verano peninsular corresponde a
las 09:00, 13:00, 17:00 y 21:00 en España.

`actualizar_feed_cache.py` descarga hasta cuatro páginas ATOM, valida todas las
respuestas y genera `feed_cache/feed.json`. Los archivos existentes solo se
sustituyen cuando el proceso completo termina correctamente. Si la Plataforma
de Contratación falla, se conserva el último snapshot válido.

Si la primera consulta falla, el flujo realiza dos reintentos automáticos: uno
tras 2 minutos y otro tras 5 minutos adicionales. GitHub solo marca la
actualización como fallida y envía el aviso cuando han fallado los tres
intentos. El tiempo máximo del proceso es de 30 minutos.

El dashboard lee el snapshot local y mantiene la consulta directa anterior
únicamente como respaldo para un despliegue que todavía no tenga snapshot. La
base histórica `licitaciones.db` y la interfaz no se modifican desde ese
snapshot auxiliar.

La base principal se sincroniza cuatro veces al día mediante el flujo
**Sincronizar radar de licitaciones**. Cada domingo se ejecuta además una
auditoría completa: licitaciones ordinarias desde el 1 de enero de 2025 y
contratos menores desde el 1 de enero de 2025. Las fechas están configuradas
independientemente para que cada fuente se revise hasta el inicio de su base.

Mientras el histórico ordinario 2025 no esté cargado, se reintenta
automáticamente la descarga del ZIP oficial todos los días a las 04:15 UTC
(06:15 en horario de verano peninsular). Una respuesta HTML del cortafuegos
nunca se acepta como ZIP ni altera la base. Cuando la descarga y la carga
terminan correctamente se guarda un marcador permanente: los reintentos se
detienen. Los registros se concilian por identificador y fecha sin reemplazar
las novedades más recientes.

La tarea también puede ejecutarse manualmente desde la pestaña **Actions** del
repositorio mediante el flujo **Actualizar feed reciente**.
