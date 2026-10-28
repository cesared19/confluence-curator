// Genera las capturas de docs/screenshots contra scripts/demo_server.py.
//
//   python scripts/demo_server.py --port 8770 &
//   node scripts/screenshots.mjs http://127.0.0.1:8770
//
// Requiere Playwright (npm install playwright && npx playwright install chromium).
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:8770'
const out = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'docs', 'screenshots')

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2, locale: 'es-ES' })
const shot = (name, options = {}) => page.screenshot({ path: path.join(out, name), ...options })
const waitAnalyzed = async () => {
	for (let i = 0; i < 40 && (await page.locator('text=Analizando…').count()); i++) {
		await page.waitForTimeout(500)
		await page.reload()
	}
}

await page.goto(`${base}/index`)
await page.click('text=Sincronizar ahora')
await page.waitForSelector('.flash.ok')

// Importar los tickets de ejemplo desde Jira.
await page.goto(`${base}/jira`)
await page.fill('#q', 'project = SIM ORDER BY updated DESC')
await page.click('text=Buscar en Jira')
await page.waitForSelector('table.list')
await page.click('text=Importar y analizar los seleccionados')
await page.waitForURL(/\/\?/)
await waitAnalyzed()

// Una nota pegada a mano desde el chat.
await page.fill(
	'#text',
	'Decisión de la reunión de diseño (24/10): en la simulación las alarmas se silencian con el botón B del control derecho. En el equipo real se silencian girando la perilla de modo.'
)
await page.selectOption('#source_type', 'Reunión')
await page.fill('#source_ref', 'Minuta diseño 24/10')
await page.click('text=Analizar y sugerir ubicación')
await page.waitForURL(/\/notes\/\d+/)
await waitAnalyzed()

// Revisión de la documentación (antes de publicar, para que aparezcan sus hallazgos).
await page.goto(`${base}/audit`)
await page.fill('#audit-stale', '0')
await page.click('form[action="/audit/settings"] button:has-text("Guardar")')
await page.waitForSelector('.flash.ok')
await page.click('text=Revisar ahora')
for (let i = 0; i < 40 && (await page.locator('text=Revisión en curso…').count()); i++) {
	await page.waitForTimeout(500)
	await page.reload()
}
await page.goto(`${base}/audit`)
await shot('revision.png', { fullPage: true })

// Publicar dos propuestas para que haya historial.
for (const id of [3, 4]) {
	await page.goto(`${base}/notes/${id}`)
	await page.click('text=Aprobar y publicar')
	await page.waitForSelector('.flash.ok')
}

// Revisión automática activada.
await page.goto(`${base}/jira`)
await page.fill('#watch-jql', 'project = SIM AND status changed AFTER -1d')
await page.fill('#watch-interval', '30')
await page.selectOption('#watch-enabled', '1')
await page.click('#automatica button:has-text("Guardar")')
await page.waitForSelector('.flash.ok')
await page.click('text=Revisar ahora')
await page.waitForSelector('.flash.ok')

await page.goto(`${base}/`)
await shot('bandeja.png', { fullPage: true })

await page.goto(`${base}/notes/1`)
await page.click('summary:has-text("Vista previa")')
await shot('propuesta.png', { fullPage: true })

await page.goto(`${base}/jira?q=${encodeURIComponent('project = SIM ORDER BY updated DESC')}`)
await shot('jira.png', { fullPage: true })

await page.goto(`${base}/setup`)
await shot('asistente.png', { fullPage: true })

await page.goto(`${base}/pdf/1`)
await page.locator('details summary').first().click()
await shot('pdf.png', { fullPage: true })

await page.emulateMedia({ colorScheme: 'dark' })
await page.goto(`${base}/notes/2`)
await shot('propuesta-oscuro.png', { fullPage: true })

await page.setViewportSize({ width: 390, height: 844 })
await page.goto(`${base}/notes/1`)
await shot('movil.png', { fullPage: false })

await browser.close()
console.log(`Capturas guardadas en ${out}`)
