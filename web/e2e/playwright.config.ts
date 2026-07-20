import { defineConfig } from '@playwright/test'

const evidenceDir = process.env.E2E_EVIDENCE_DIR
const chromePath = process.env.E2E_CHROME_PATH

if (!evidenceDir || !chromePath || !process.env.E2E_BASE_URL) {
  throw new Error('E2E runner must provide evidence directory, Chrome path, and base URL')
}

export default defineConfig({
  testDir: '.',
  testMatch: 'plow-whip-console.acceptance.spec.ts',
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  outputDir: `${evidenceDir}/test-results`,
  reporter: [
    ['line'],
    ['json', { outputFile: `${evidenceDir}/playwright-report.json` }],
  ],
  use: {
    baseURL: process.env.E2E_BASE_URL,
    viewport: { width: 1440, height: 900 },
    launchOptions: { executablePath: chromePath },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
