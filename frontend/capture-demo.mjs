import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

const root = resolve(import.meta.dirname, '..')
const directory = resolve(root, 'docs/images')
const server = spawn(process.env.PYTHON || 'python', ['-B', 'jobhunt.py', 'demo', '--port', '0'], {
  cwd: root, stdio: ['ignore', 'pipe', 'inherit'],
})
let browser
try {
  const origin = await new Promise((accept, reject) => {
    const timer = setTimeout(() => reject(new Error('Demo did not start in time')), 15000)
    server.once('exit', code => { clearTimeout(timer); reject(new Error('Demo exited: ' + code)) })
    server.stdout.once('data', data => {
      clearTimeout(timer)
      const url = String(data).match(/http:\/\/127\.0\.0\.1:\d+/)
      if (url) accept(url[0]); else reject(new Error('Unexpected demo startup message'))
    })
  })
  await mkdir(directory, { recursive: true })
  const channel = process.env.BROWSER_CHANNEL || 'chrome'
  browser = await chromium.launch({ channel, headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1020 }, deviceScaleFactor: 1 })
  await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort())
  const captures = [
    ['01-overview','/','Hushpine Systems'],
    ['02-jobs','/jobs','Hushpine Systems'],
    ['03-job-detail','/jobs?id=1','Application platform'],
    ['04-resume','/resumes?id=1','Download LaTeX'],
    ['05-outreach','/outreach?id=1','rowan@example.com'],
    ['06-auto-apply','/auto-apply','Which shift do you prefer?'],
  ]
  for (const [name, route, ready] of captures) {
    await page.goto(origin + route)
    await page.getByText(ready, { exact: false }).first().waitFor()
    if (name === '04-resume') {
      // Opening the print preview avoids clipping the resume frame in full-page captures.
      const href = await page.getByRole('link', { name: 'Open preview' }).getAttribute('href')
      await page.goto(origin + href)
      await page.getByText('Casey Wrenfield', { exact: false }).first().waitFor()
    }
    await page.screenshot({ path: resolve(directory, name + '.png'), animations: 'disabled', fullPage: true })
  }
  console.log('Captured six fictional demo screens.')
} finally {
  if (browser) await browser.close()
  server.kill()
}
