# Cambios

## Sin publicar

- Importación desde el chat: página **Chat** que agrupa los mensajes de un canal en conversaciones (hilos y mensajes cercanos), indica cuáles ya se importaron o siguieron después, y crea una nota por conversación con autores, horas, enlace e imágenes. Primer conector: Slack, con el token de una app interna; se configura y se prueba en el asistente (paso «Chat»).

## 0.0.3-beta (2026-10-28)

- Asistente de configuración en el navegador: aparece la primera vez y queda en **Ajustes**. Prueba la conexión con Confluence (y permite elegir el espacio de una lista), Jira, Claude y Ollama, y guarda todo sin editar archivos. Los cambios se aplican sin reiniciar.
- Lanzadores de doble clic para Windows, Mac y Linux: preparan el entorno, instalan lo necesario y abren el navegador.
- `python -m curator --open` abre el navegador; si la herramienta ya está en marcha, solo lo abre.
- Docker: la configuración pasa a `/data/config.env` en el volumen y se hace desde el asistente. Quien usaba `.env` con Docker puede copiarlo al volumen o volver a configurarla en **Ajustes**.
- Corregido: el título de la pestaña en la página Jira mostraba HTML.

## 0.0.2-beta (2026-10-27)

- Importación de PDFs: división por capítulos (marcadores) o bloques de páginas, partes largas divididas, vista previa del texto, limpieza de encabezados y pies repetidos, y páginas escaneadas enviadas como imágenes. Cada parte elegida se analiza como una nota.
- Revisión de la documentación: detecta páginas que mencionan tickets de Jira actualizados después, contradicciones entre secciones de páginas distintas (con IA) y páginas sin cambios hace tiempo. Cada hallazgo se corrige con una nota dirigida a la sección o se descarta, y se resuelve solo cuando cambia la página. Se ejecuta a pedido o cada N horas.
- Se guarda la fecha de última edición de cada página. Las bases de datos de 0.0.1 se actualizan solas al arrancar.

## 0.0.1-beta (2026-10-27)

Primera versión beta.

- Bandeja para pegar texto e imágenes (chat, Jira, manuales, reuniones) y recibir propuestas de dónde documentarlos en un espacio de Confluence Cloud: actualizar una sección, agregar a una página o crear una página nueva.
- Cada propuesta muestra la ubicación, el porqué, la confianza, las contradicciones con lo publicado, las diferencias y una vista previa. Se puede editar, elegir otra ubicación o descartar.
- Publicación solo con aprobación. Antes de publicar se relee la página en vivo y se reemplaza solo la sección afectada; si cambió mientras tanto, se detiene. Las imágenes se suben como adjuntos.
- Tres modos de análisis: Claude (`anthropic`), modelo local con Ollama (`ollama`) y sin IA (`mock`).
- Importación desde Jira Cloud por JQL o claves: una nota por ticket con datos, descripción, comentarios, enlace e imágenes adjuntas. Indica qué tickets ya se importaron y cuáles cambiaron.
- Revisión automática de Jira con una consulta guardada e intervalo configurable.
- Análisis en segundo plano, de a una nota, que se retoma si la aplicación se reinicia.
- Búsqueda en la documentación indexada, historial de cambios, modo claro y oscuro, diseño para móvil.
- Modo demo con Confluence y Jira simulados; Docker Compose; usuario y contraseña opcionales.
