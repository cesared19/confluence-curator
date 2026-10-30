# Cambios

## 1.0.0-beta (2026-10-30)

Primera versión 1.0, todavía en prueba.

- **Actualizador integrado:** la herramienta busca versiones nuevas en GitHub una vez al día, avisa arriba de cada página y se actualiza con un botón en **Actualizaciones**: respalda la versión actual y la base de datos, reemplaza los archivos sin tocar la configuración ni los datos, instala lo necesario y se reinicia sola. Permite volver a la versión anterior y usar un token de GitHub si el repositorio es privado. Con Docker o con una copia de git explica cómo actualizar.
- Licencia Apache 2.0.
- Si la IA acierta la página pero da un número de sección que no existe (frecuente con modelos locales), la herramienta lo corrige cuando no hay duda: la página tiene una sola sección, la propuesta empieza con el título de una sección real, se le mostró una sola sección de esa página, o elegiste la sección al analizar de nuevo.
- Si no se puede saber, la propuesta muestra las secciones reales de la página para elegir dónde va (o agregar al final) y analizar de nuevo; mientras tanto no se ofrece publicar.
- La consulta a la IA incluye la lista explícita de ubicaciones válidas, para que los modelos chicos se equivoquen menos.

## 0.0.5-beta (2026-10-30)

- Modelo local (Ollama): la respuesta llega por partes. El bloque Actividad muestra si el modelo está leyendo el contexto o redactando y cuántos caracteres lleva, y **Detener** corta la generación en Ollama de inmediato (antes había que esperar a que terminara, hasta 15 minutos).
- Ollama recibe 5 secciones candidatas por nota en vez de 10 y un contexto ajustado al tamaño de cada consulta, para responder antes y usar menos memoria; si algo no cabe, se avisa en lugar de recortarlo en silencio.
- Una detención durante la respuesta de la IA deja la nota como «Detenida», no como error.
- «Analizar de nuevo» no se ofrece mientras la nota se está analizando, y una nota no entra dos veces a la cola.
- El asistente indica qué tamaño de modelo local conviene según la memoria del equipo.
- El bloque **Actividad** tiene su lugar en la interfaz: una columna a la derecha del contenido en pantallas anchas (sigue el desplazamiento) y una franja plegable bajo el menú en las angostas. Ya no flota sobre la página.
- Las tareas detenidas que esperan a que termine una respuesta de la IA se muestran como «Deteniendo…», y el botón Detener se oculta cuando no queda nada por detener.
- Corregido: después de actualizar, el navegador podía seguir usando la hoja de estilos de la versión anterior (y mostrar el bloque Actividad sin formato). Ahora cada versión de los estilos tiene su propia dirección.
- La fecha de la última sincronización en Índice se muestra en formato corto.

## 0.0.4-beta (2026-10-30)

- Bloque **Actividad** en todas las páginas: muestra en vivo qué hace la herramienta, la cola de análisis y los últimos resultados, se abre solo cuando algo empieza y actualiza la página cuando termina algo suyo.
- Botón **Detener**: cancela lo que está en curso y la cola de análisis (descarta la respuesta de la IA si llega después) y pausa las revisiones automáticas hasta **Reanudar**.
- Las importaciones (Jira, chat, PDF), la sincronización y *Revisar ahora* de Jira pasan a segundo plano; después de importar se vuelve a la bandeja y el avance se sigue en Actividad.
- Un análisis anterior se reemplaza solo cuando el nuevo termina bien: si se detiene o falla, las propuestas anteriores se conservan.
- Importación desde el chat: página **Chat** que agrupa los mensajes de un canal en conversaciones (hilos y mensajes cercanos), indica cuáles ya se importaron o siguieron después, y crea una nota por conversación con autores, horas, enlace e imágenes. Primer conector: Slack, con el token de una app interna; se configura y se prueba en el asistente (paso «Chat»).
- La bandeja ya no se recarga sola mientras se escribe en ella.

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
