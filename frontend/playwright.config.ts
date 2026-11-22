import { defineConfig } from '@playwright/test'

const baseURL = process.env.QA_BASE_URL ?? 'http://127.0.0.1:18085'
const url = new URL(baseURL)
if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.port === '8080' || url.origin !== baseURL) {
  throw new Error('QA_BASE_URL must be the loopback origin of the isolated QA Compose stack.')
}

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 60_000,
  reporter: 'list',
  use: { baseURL, browserName: 'chromium', trace: 'retain-on-failure' },
})
