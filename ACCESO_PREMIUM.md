# Acceso Premium y favoritos compartidos

La aplicación permanece pública por defecto. El bloque comercial de LANDA se oculta
cuando la única cuenta autorizada inicia sesión mediante Microsoft.

## Configuración

1. Registra una aplicación de un solo tenant en Microsoft Entra ID.
2. Añade como URI de redirección web
   `https://dashboard-licitaciones.streamlit.app/oauth2callback`.
3. Crea un secreto de cliente y concede a la aplicación los permisos de Microsoft
   Graph necesarios para leer y modificar la lista de SharePoint. Limita el acceso al
   sitio utilizado por la aplicación siempre que la administración del tenant lo permita.
4. Crea una Lista de Microsoft privada con estas columnas:
   - `Title`: texto, no obligatoria y no única (es la columna predeterminada).
   - `LicitacionId`: texto, obligatoria y única.
   - `PblSinIva`: moneda en euros, no obligatoria y no única.
   - `Estado`: texto, no obligatoria y no única.
   - `LinkPlataforma`: texto, no obligatoria y no única.
   - `OrganoContratacion`: texto, no obligatoria y no única.
   - `Municipio`: texto, no obligatoria y no única.
5. Copia las claves de `.streamlit/secrets.example.toml` en los secretos del despliegue
   de Streamlit. No crees ni publiques un archivo `secrets.toml` real en GitHub.

Hasta que `[auth]` esté configurado, el botón de acceso aparece deshabilitado. Una vez
configurado el login, el modo Premium funciona aunque la lista todavía no esté conectada;
en ese caso las tarjetas indican que los favoritos están pendientes de configuración.

## Permisos funcionales

- Visitante: ve el dashboard y la presentación comercial, sin favoritos.
- Cuenta autorizada: ve el modo Premium, puede alternar favoritos y abrir la lista.
- Otra cuenta autenticada: no obtiene acceso Premium y puede cerrar sesión para cambiar
  de cuenta.
