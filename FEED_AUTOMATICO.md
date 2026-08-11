# Feed automático

El feed reciente se descarga y valida mediante GitHub Actions a las 09:07,
13:07, 17:07 y 21:07 en la zona horaria `Europe/Madrid`.

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
base histórica `licitaciones.db` y la interfaz no se modifican.

La tarea también puede ejecutarse manualmente desde la pestaña **Actions** del
repositorio mediante el flujo **Actualizar feed reciente**.
