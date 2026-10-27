# Confluence Curator

Herramienta para consolidar en Confluence la información del proyecto que hoy está dispersa en chats, tickets de Jira y manuales del producto.

Le das un texto o una imagen y la herramienta:

1. Busca en el espacio de Confluence las secciones relacionadas.
2. Propone dónde va: actualizar una sección, agregar al final de una página o crear una página nueva. Muestra el porqué, una confianza y las contradicciones con lo ya publicado (por ejemplo, "antes 8 bar, ahora 7,5 bar").
3. Te muestra las diferencias y una vista previa. Puedes editar el contenido, elegir otra ubicación o descartarlo.
4. **Solo publica cuando lo apruebas.** Antes de publicar vuelve a leer la página en vivo; si alguien cambió esa sección mientras tanto, se detiene y te pide volver a analizar. Las imágenes se suben como adjuntos de la página.

Cada publicación queda en el historial de versiones de Confluence, con el resumen del cambio y el número de nota, y en la página *Historial* de la herramienta.

## Cómo decide dónde va la información

- **Con IA (Claude):** lee la nota, las imágenes, el árbol de páginas del espacio y las secciones más parecidas, y redacta el cambio respetando el estilo de la página. Distingue entre lo que hace el producto real y lo que decide el proyecto para la simulación, y si no queda claro lo indica.
- **Sin IA (modo de prueba):** agrega la nota tal cual a la sección con más palabras en común. Sirve para probar el flujo sin enviar información a ningún servicio externo.

Si no hay `ANTHROPIC_API_KEY`, se usa el modo sin IA. **Antes de activar Claude, confirma que la política de tu empresa permite enviar a la API de Anthropic la información del proyecto y el contenido del espacio.** Con la IA activada se envían, en cada análisis, la nota, sus imágenes, los títulos de las páginas y el contenido de las secciones candidatas.

## Puesta en marcha

Requisitos: Python 3.11 o superior.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # completa CONFLUENCE_* y, si aplica, ANTHROPIC_API_KEY
python -m curator      # http://127.0.0.1:8000
```

Después ve a **Índice → Sincronizar ahora** para que la herramienta lea las páginas del espacio.

Para probar sin credenciales, usa un Confluence simulado con páginas de ejemplo:

```bash
CURATOR_DEMO=1 python -m curator
```

Con Docker: `docker compose up -d --build` (usa `.env` y publica la interfaz solo en `127.0.0.1:8000`).

### Credenciales de Confluence

- `CONFLUENCE_BASE_URL`: `https://tu-empresa.atlassian.net`.
- `CONFLUENCE_EMAIL` y `CONFLUENCE_API_TOKEN`: una cuenta con permiso de ver, editar y adjuntar en el espacio. El token se crea en <https://id.atlassian.com/manage-profile/security/api-tokens>. Las ediciones aparecen a nombre de esa cuenta.
- `CONFLUENCE_SPACE_KEY`: la clave del espacio (está en la URL, `/wiki/spaces/CLAVE/...`).

Si la herramienta corre en un servidor compartido, define `CURATOR_USER` y `CURATOR_PASSWORD` para pedir usuario y contraseña.

## Uso diario

- **Bandeja:** pega el texto (un hilo de chat, la descripción y los comentarios de un ticket, un fragmento del manual), indica la fuente y adjunta imágenes si hacen falta. Una nota puede generar varias propuestas si toca varios temas.
- **Revisar:** en cada propuesta revisa las diferencias y la vista previa. *Editar contenido* permite corregir el XHTML antes de publicar; `[[IMAGEN:n]]` coloca la imagen n de la nota.
- **¿No es el lugar correcto?** Elige una alternativa sugerida, una página destino o escribe una indicación ("es una decisión del proyecto, no del producto") y vuelve a analizar.
- **Buscar:** consulta rápida de qué dice hoy la documentación sobre un tema.
- **Índice:** sincroniza cuando se edite Confluence por fuera de la herramienta.

Consejo para la consolidación inicial: empieza por la información de mayor autoridad (manuales del producto, decisiones cerradas) y deja para después los hilos de chat. Así la herramienta tiene una base contra la cual detectar contradicciones.

## Desarrollo

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Estructura:

- `curator/confluence.py`: cliente de Confluence Cloud (API v2; v1 para adjuntos).
- `curator/storage.py`: divide las páginas en secciones por encabezados sobre el texto original, de modo que al reemplazar una sección el resto de la página no cambia. También valida el XHTML.
- `curator/search.py`: búsqueda BM25 sin acentos (español e inglés).
- `curator/advisor.py`: asesores (Claude con salida estructurada, y modo sin IA).
- `curator/service.py`: flujo de notas, propuestas y publicación.
- `curator/web.py` y `curator/templates/`: interfaz web (FastAPI + Jinja).
- `curator/demo.py`: Confluence simulado.

## Limitaciones conocidas

- Trabaja con un solo espacio a la vez.
- Las secciones se definen por encabezados (`h1` a `h6`). En páginas sin encabezados, la página entera es una sola sección.
- No lee directamente Slack, Teams ni Jira: la información se pega a mano. Es el siguiente paso natural (ver abajo).

## Próximos pasos posibles

- Importar directamente desde Jira (por JQL) y desde el chat que usa el equipo.
- Cargar PDFs de manuales completos y repartirlos en varias propuestas.
- Revisión periódica que señale páginas desactualizadas o contradictorias entre sí.
