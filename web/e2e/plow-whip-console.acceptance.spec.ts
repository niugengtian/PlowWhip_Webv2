import { expect, test, type Page } from '@playwright/test'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

type SeedProject = {
  project_id: string
  task_id: string
  task_title: string
  status: string
  history_total: number
  today_total: number
}

const seed = JSON.parse(readFileSync(requiredEnv('E2E_SEED_PATH'), 'utf-8')) as {
  projects: Record<'E2E Alpha' | 'E2E Beta', SeedProject>
}
const evidenceDir = requiredEnv('E2E_EVIDENCE_DIR')
const observationsPath = requiredEnv('E2E_OBSERVATIONS_PATH')
const candidateHead = requiredEnv('E2E_CANDIDATE_HEAD')
const databasePath = requiredEnv('E2E_DATA_DIR')
const port = Number(requiredEnv('E2E_PORT'))

test('isolated candidate satisfies the complete browser acceptance', async ({ page, browser }) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  const unexpectedNetworkFailures: string[] = []
  const expectedCancellations: string[] = []
  const completedAcceptanceIds: string[] = []
  const assertions: { acceptance_id: string; passed: boolean; summary: string }[] = []
  const requestCounts = new Map<string, number>()
  const screenshots: string[] = []
  const startedAt = new Date().toISOString()

  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => {
    const url = new URL(request.url())
    requestCounts.set(url.pathname, (requestCounts.get(url.pathname) ?? 0) + 1)
  })
  page.on('requestfailed', (request) => {
    const failure = `${request.method()} ${request.url()} ${request.failure()?.errorText ?? 'unknown'}`
    if (
      request.url().includes('/api/events/stream')
      && /abort|cancel/i.test(request.failure()?.errorText ?? '')
    ) expectedCancellations.push(failure)
    else unexpectedNetworkFailures.push(failure)
  })
  page.on('response', (response) => {
    if (response.status() >= 400) {
      unexpectedNetworkFailures.push(`${response.status()} ${response.request().method()} ${response.url()}`)
    }
  })

  const pass = (acceptanceId: string, summary: string) => {
    completedAcceptanceIds.push(acceptanceId)
    assertions.push({ acceptance_id: acceptanceId, passed: true, summary })
  }

  try {
    await page.goto('/')
    await expect(page.locator('#root .app-shell')).toBeVisible()
    await expect(page.getByText('Plow Whip', { exact: true })).toBeVisible()
    expect(port).not.toBe(8742)
    pass('BROWSER-01', 'System Chrome rendered the candidate DOM on the isolated service port')

    const scope = page.locator('.context-bar select').first()
    const alpha = seed.projects['E2E Alpha']
    const beta = seed.projects['E2E Beta']
    await expect(scope.locator('option')).toHaveCount(3)

    let releaseAlpha!: () => void
    let alphaRequestSeen!: () => void
    const alphaBlocked = new Promise<void>((resolvePromise) => { releaseAlpha = resolvePromise })
    const alphaSeen = new Promise<void>((resolvePromise) => { alphaRequestSeen = resolvePromise })
    await page.route(`**/api/usage?project_id=${alpha.project_id}`, async (route) => {
      alphaRequestSeen()
      await alphaBlocked
      await route.continue()
    })
    await scope.selectOption(alpha.project_id)
    await alphaSeen
    await scope.selectOption(beta.project_id)
    await expect(metric(page, '今日 Token')).toHaveText(beta.today_total.toLocaleString())
    releaseAlpha()
    await page.waitForTimeout(500)
    await expect(scope).toHaveValue(beta.project_id)
    await expect(metric(page, '今日 Token')).toHaveText(beta.today_total.toLocaleString())
    await page.unroute(`**/api/usage?project_id=${alpha.project_id}`)
    pass('BROWSER-02', 'A delayed Alpha response cannot overwrite the selected Beta project scope')

    await scope.selectOption('all')
    await page.getByRole('button', { name: '管家', exact: true }).click()
    await page.getByRole('button', { name: '与全局管家对话' }).click()
    const globalHistory = page.getByRole('navigation', { name: '全局管家历史会话' })
    await expect(globalHistory.getByText('E2E Alpha global history', { exact: true })).toBeVisible()
    await expect(globalHistory.getByText('E2E Beta global history', { exact: true })).toBeVisible()
    await expect(globalHistory.getByText('澄清中 · r0', { exact: true })).toHaveCount(2)
    await closeDialog(page)

    for (const [name, project] of Object.entries(seed.projects)) {
      await scope.selectOption(project.project_id)
      await page.getByRole('button', { name: '与项目管家对话' }).click()
      const projectHistory = page.getByRole('navigation', { name: '项目管家历史会话' })
      await expect(projectHistory.getByText(`${name} project history`, { exact: true })).toBeVisible()
      await expect(projectHistory.getByText(`${name} global history`, { exact: true })).toHaveCount(0)
      await closeDialog(page)
    }
    pass('BROWSER-03', 'Global and project Butler identities and histories stay project-correct')

    await scope.selectOption(beta.project_id)
    await page.getByRole('button', { name: '任务', exact: true }).click()
    await page.getByRole('button', { name: new RegExp(beta.task_title) }).click()
    const detail = page.locator('.detail-panel')
    await expect(detail.getByRole('heading', { name: beta.task_title })).toBeVisible()
    await expect(detail.locator('.status-pill', { hasText: '已取消' })).toBeVisible()
    await expect(detail.getByText('task.cancel', { exact: true })).toBeVisible()
    pass('BROWSER-04', 'The Beta terminal task list entry opens canonical cancelled detail and events')

    await page.getByRole('button', { name: 'Token', exact: true }).click()
    await expect(metric(page, '项目 E2E Beta全历史')).toHaveText(beta.history_total.toLocaleString())
    await expect(metric(page, '项目 E2E Beta今日')).toHaveText(beta.today_total.toLocaleString())
    await expect(page.getByTestId('token-trend-chart')).toBeVisible()
    await scope.selectOption(alpha.project_id)
    await expect(metric(page, '项目 E2E Alpha全历史')).toHaveText(alpha.history_total.toLocaleString())
    await expect(metric(page, '项目 E2E Alpha今日')).toHaveText(alpha.today_total.toLocaleString())
    await expect(page.getByTestId('token-trend-chart')).toBeVisible()
    await scope.selectOption(beta.project_id)
    await expect(metric(page, '项目 E2E Beta全历史')).toHaveText(beta.history_total.toLocaleString())
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 320, height: 900 }]) {
      await page.setViewportSize(viewport)
      const layout = await layoutFacts(page)
      expect(layout.documentOverflow).toBe(false)
      expect(layout.metricNumberOverflow).toEqual([])
    }
    pass('BROWSER-05', 'Today, history, and trend Token views follow round-trip project filtering')

    await page.getByRole('button', { name: '设置', exact: true }).click()
    for (const group of ['常规', '管家与 Provider', '持续执行与保护', '角色、规则与模板', '安全与运行时', '高级与审计']) {
      await expect(page.getByText(group, { exact: true })).toBeVisible()
    }
    const refresh = page.getByLabel('自动刷新间隔')
    await expect(refresh.locator('option')).toHaveText([
      '关闭', '5s', '10s', '30s', '1min', '5min', '10min', '1h', '2h', '4h',
    ])
    for (const value of ['0', '5000', '10000', '30000', '60000', '300000', '600000', '3600000', '7200000', '14400000']) {
      await refresh.selectOption(value)
      await expect(refresh).toHaveValue(value)
    }
    pass('BROWSER-06', 'Settings render and every required refresh interval is selectable')

    await setVisibility(page, 'hidden')
    await refresh.selectOption('5000')
    const hiddenBaseline = requestCounts.get('/api/providers') ?? 0
    await page.waitForTimeout(5_300)
    expect(requestCounts.get('/api/providers') ?? 0).toBe(hiddenBaseline)
    await setVisibility(page, 'visible')
    await expect.poll(() => requestCounts.get('/api/providers') ?? 0).toBe(hiddenBaseline + 1)
    await page.waitForTimeout(800)
    expect(requestCounts.get('/api/providers') ?? 0).toBe(hiddenBaseline + 1)
    await refresh.selectOption('0')
    pass('BROWSER-07', 'Hidden pages pause polling and visibility recovery performs one refresh without a timer storm')

    await page.getByRole('button', { name: '任务', exact: true }).click()
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }]) {
      await page.setViewportSize(viewport)
      await expect(page.getByTestId('unified-task-workspace')).toBeVisible()
      const layout = await layoutFacts(page)
      expect(layout.documentOverflow).toBe(false)
      expect(layout.projectScopeWrapped).toBe(false)
      expect(layout.contextButtonOverflow).toEqual([])
      expect(layout.metricNumberOverflow).toEqual([])
      expect(layout.terminalLaneOverflow).toBe(false)
      const screenshot = join(evidenceDir, `layout-${viewport.width}x${viewport.height}.png`)
      await page.screenshot({ path: screenshot, fullPage: true })
      screenshots.push(screenshot)
    }
    pass('BROWSER-08', '1440x900 and 1024x768 layouts retain unwrapped controls, readable numbers, and bounded terminal lane')

    await page.waitForTimeout(200)
    expect(consoleErrors).toEqual([])
    expect(pageErrors).toEqual([])
    expect(unexpectedNetworkFailures).toEqual([])
    pass('BROWSER-09', 'Console errors, unhandled page errors, and unexpected failed requests are zero')
  } finally {
    mkdirSync(evidenceDir, { recursive: true })
    writeFileSync(observationsPath, `${JSON.stringify({
      candidateHead,
      databasePath,
      port,
      browserVersion: browser.version(),
      startedAt,
      finishedAt: new Date().toISOString(),
      viewports: [{ width: 1440, height: 900 }, { width: 1024, height: 768 }],
      screenshots,
      completedAcceptanceIds,
      failedAcceptanceIds: completedAcceptanceIds.length === 9 ? [] : ['BROWSER-GATE'],
      assertions,
      consoleErrors,
      pageErrors,
      unexpectedNetworkFailures,
      expectedCancellations,
      requestCounts: Object.fromEntries(requestCounts),
    }, null, 2)}\n`)
  }
})

function metric(page: Page, label: string) {
  return page.locator('.metric-card').filter({
    has: page.locator('span', { hasText: label }),
  }).locator('strong')
}

async function closeDialog(page: Page) {
  await page.locator('.butler-dialog .drawer-head').getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('.butler-dialog')).toHaveCount(0)
}

async function setVisibility(page: Page, state: 'hidden' | 'visible') {
  await page.evaluate((nextState) => {
    const documentWithState = document as Document & { __e2eVisibility?: string }
    documentWithState.__e2eVisibility = nextState
    if (!Object.prototype.hasOwnProperty.call(document, 'visibilityState')) {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => documentWithState.__e2eVisibility,
      })
    }
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
}

async function layoutFacts(page: Page) {
  return page.evaluate(() => {
    const overflowing = (selector: string) => [...document.querySelectorAll<HTMLElement>(selector)]
      .filter((element) => element.offsetParent !== null && (
        element.scrollWidth > element.clientWidth + 1
        || element.scrollHeight > element.clientHeight + 1
      ))
      .map((element) => element.textContent?.trim().slice(0, 80) || selector)
    const scope = document.querySelector<HTMLElement>('.scope-control > span')
    const terminalLane = [...document.querySelectorAll<HTMLElement>('.compact-lane')]
      .find((element) => element.textContent?.includes('已终态'))
    return {
      documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      projectScopeWrapped: scope
        ? getComputedStyle(scope).whiteSpace !== 'nowrap' || scope.getClientRects().length !== 1
        : true,
      contextButtonOverflow: overflowing('.context-actions button'),
      metricNumberOverflow: overflowing('.metric-card strong'),
      terminalLaneOverflow: terminalLane
        ? terminalLane.scrollWidth > terminalLane.clientWidth + 1
        : true,
    }
  })
}

function requiredEnv(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required`)
  return value
}
