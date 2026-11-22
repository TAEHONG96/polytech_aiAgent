import { defineConfig } from '@playwright/test'
import systemConfig from './playwright.config'

export default defineConfig({ ...systemConfig, testDir: './e2e-training' })
