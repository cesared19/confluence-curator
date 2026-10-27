# Cambios

## Sin publicar

- Importación de PDFs: división por capítulos (marcadores) o bloques de páginas, partes largas divididas, vista previa del texto, limpieza de encabezados y pies repetidos, y páginas escaneadas enviadas como imágenes. Cada parte elegida se analiza como una nota.

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
