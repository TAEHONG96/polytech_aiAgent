import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

type Ticket = { id: string; title: string; status: string }
type HintRun = { id: string; level: number; answer: string; status: string; model_calls: number; embedding_calls: number;
  execution_mode: string; sources: { title: string; source_url: string }[]; can_retry: boolean; error?: { code: string } }

test('adaptive offers are free until clicked and connect progressive hints to an approved SOP workflow', async ({ page, baseURL }, info) => {
  const mode = process.env.QA_TUTOR_SMOKE
  test.skip(mode !== 'mock' && mode !== 'gemini', 'Requires a seeded synthetic corpus')
  test.setTimeout(420_000)
  const common = { Origin: baseURL! }
  const credentials = { username: `kiro-tutor-${randomUUID().slice(0, 8)}`, password: `Synthetic-${randomUUID()}` }
  expect((await page.request.post('/api/v1/auth/register', { headers: common, data: credentials })).status()).toBe(201)
  const login = await page.request.post('/api/v1/auth/login', { headers: common, data: credentials })
  expect(login.ok()).toBeTruthy()
  const headers = { ...common, 'X-CSRF-Token': (await login.json()).csrf_token as string, 'Idempotency-Key': randomUUID() }
  const start = await page.request.post('/api/v1/simulation-runs', { headers,
    data: { scenario_key: 'synthetic-ops-memory', scenario_version: '1.0' } })
  expect(start.status()).toBe(201)
  const apiRun = start.headers().location
  const run = apiRun.slice('/api/v1'.length)
  const { items } = await (await page.request.get(`${apiRun}/tickets?scope=all`)).json() as { items: Ticket[] }
  const memory = items.find(ticket => /ECC/i.test(ticket.title) && ticket.status !== 'resolved')!
  expect(memory).toBeDefined()
  const others = items.filter(ticket => ticket.id !== memory.id)
  const detail = (ticket: Ticket) => `${run}/tickets/${ticket.id}#knowledge`
  const pet = page.getByRole('button', { name: '키로 컴패니언' })
  const panel = page.getByRole('group', { name: '키로 힌트' })
  const hintPosts: string[] = []
  const workflowPosts: string[] = []
  let manualRetries = 0
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (/\/companion\/tutor\/.+\/hints$/.test(path) && request.method() === 'POST') hintPosts.push(path)
    if (path === '/api/v1/companion/chat' && request.method() === 'POST') workflowPosts.push(path)
  })

  async function search(query: string, retry = true): Promise<number> {
    const request = page.waitForResponse(response => new URL(response.url()).pathname === `${apiRun}/knowledge/search`
      && response.request().method() === 'POST')
    await page.locator('[data-companion-anchor="sop-query"]').fill(query)
    await page.locator('[data-companion-anchor="search-sop"]').click()
    const response = await request
    if (mode === 'gemini' && retry && [429, 503].includes(response.status())) {
      await info.attach('temporary-search-error', { body: String(response.status()), contentType: 'text/plain' })
      await expect(page.locator('[data-companion-anchor="search-sop"]')).toBeEnabled()
      return search(query, false)
    }
    expect(response.ok()).toBeTruthy()
    const body = await response.json() as { results: unknown[]; tutor_receipt: string }
    expect(body.tutor_receipt).toBeTruthy()
    await expect(page.locator('[data-companion-anchor="search-sop"]')).toBeEnabled()
    return body.results.length
  }

  async function missTwice({ checkFirstHint = false } = {}) {
    const queries = ['퀼팅 도자기 해파리 화판 압화 도감', '고대 도자기 수채화 안료 지느러미', '나침반 퀼트 점토 전시 미술',
      'zzqxvpbwv gcqjzf xkpxn czwq']
    let consecutive = 0
    for (const query of queries) {
      consecutive = await search(query) === 0 ? consecutive + 1 : 0
      if (checkFirstHint && consecutive === 1) {
        await expect(page.getByText('화면 안내', { exact: true })).toBeVisible()
        await pet.press('Enter')
        await expect(panel).toContainText('화면 안내')
        await expect(panel).toContainText('5분 안에 두 번 연속')
        await expect(panel.getByRole('button', { name: '조금 더 알려줘' })).toHaveCount(0)
        checkFirstHint = false
      }
      if (consecutive === 2) break
    }
    expect(consecutive).toBe(2)
  }

  await page.goto(detail(memory))
  await expect(pet).toBeVisible()
  await page.getByText('컴패니언', { exact: true }).click()
  await page.getByLabel('맞춤 힌트 빈도').selectOption('high')
  await page.getByText('컴패니언', { exact: true }).click()
  await missTwice({ checkFirstHint: true })
  await expect(pet).toHaveAttribute('data-adaptive-hint', 'ready', { timeout: 30_000 })
  await expect(page.getByText('맞춤 힌트', { exact: true })).toBeVisible()
  await expect(pet).toHaveAttribute('data-state', 'hint')
  expect(hintPosts).toHaveLength(0)
  expect(workflowPosts).toHaveLength(0)
  await expect(panel).toHaveCount(0)

  async function nextHint(level: number, click: () => Promise<unknown>) {
    const waitForHint = (exclude?: string) => page.waitForResponse(async response => {
      if (!/\/companion\/tutor\/.+\/hints\/.+/.test(new URL(response.url()).pathname) || !response.ok()) return false
      const body = await response.json() as HintRun
      return body.id !== exclude && body.level === level && (body.status === 'succeeded' || body.status === 'failed')
    }, { timeout: 110_000 })
    const settled = waitForHint()
    await click()
    let body = await (await settled).json() as HintRun
    if (mode === 'gemini' && body.status === 'failed' && body.can_retry
        && ['AI_UNAVAILABLE', 'AI_RATE_LIMIT', 'AI_TIMEOUT'].includes(body.error?.code ?? '')) {
      await info.attach(`hint-level-${level}-temporary-error`, { body: JSON.stringify(body), contentType: 'application/json' })
      const retry = panel.getByRole('button', { name: '힌트 다시 요청' })
      await expect(retry).toBeVisible()
      const retried = waitForHint(body.id)
      manualRetries++
      await retry.click()
      body = await (await retried).json() as HintRun
    }
    await info.attach(`hint-level-${level}`, { body: JSON.stringify(body), contentType: 'application/json' })
    expect(body.status).toBe('succeeded')
    expect(body.execution_mode).toBe(mode)
    expect(body.model_calls).toBe(1)
    await expect(panel).toContainText(`맞춤 힌트 ${level}/3단계`)
    await expect(panel).toContainText(body.answer)
    for (const source of body.sources) {
      await expect(panel.getByRole('link', { name: source.title, exact: true })).toHaveCSS('pointer-events', 'auto')
    }
    return body
  }

  const level1 = await nextHint(1, () => pet.press('Enter'))
  expect(level1.answer).toMatch(/ECC|메모리|슬롯/i)
  if (mode === 'mock') expect(level1.sources.length).toBeGreaterThan(0)
  const level2 = await nextHint(2, () => panel.getByRole('button', { name: '조금 더 알려줘' }).click())
  const level3 = await nextHint(3, () => panel.getByRole('button', { name: '조금 더 알려줘' }).click())
  expect(new Set([level1.answer, level2.answer, level3.answer]).size).toBe(3)
  expect(hintPosts).toHaveLength(3 + manualRetries)
  await expect(panel.getByRole('button', { name: '조금 더 알려줘' })).toHaveCount(0)
  await panel.getByRole('button', { name: '관련 SOP 비교 요청' }).click()
  const chat = page.getByRole('region', { name: '키로 채팅' })
  await expect(chat.getByLabel('키로에게 질문')).toHaveValue(/승인 SOP를 찾아 비교/)
  expect(workflowPosts).toHaveLength(0)
  await chat.getByRole('button', { name: '전송' }).click()
  const action = chat.getByRole('group', { name: '키로 실행 제안' })
  await expect(action.getByRole('button', { name: '이 문서 선택' })).toBeVisible({ timeout: 80_000 })
  await expect(page).toHaveURL(baseURL + detail(memory))
  await action.getByRole('radio').first().check()
  await action.getByRole('button', { name: '이 문서 선택' }).click()
  const approval = action.getByRole('button', { name: '문서 열기' })
  await expect(approval).toBeVisible()
  await expect(page).toHaveURL(baseURL + detail(memory))
  await approval.click()
  await expect(page.locator('[data-companion-destination="sop"]')).toBeVisible()
  await expect(chat.getByRole('region', { name: '키로 워크플로' })).toContainText('4/4단계 완료')

  if (mode === 'mock') {
    await chat.getByRole('button', { name: '닫기', exact: true }).click()
    await page.goto(detail(others[0]))
    await page.getByText('컴패니언', { exact: true }).click()
    await page.getByLabel('애니메이션 정지').check()
    await page.getByText('컴패니언', { exact: true }).click()
    await missTwice()
    await expect(pet).not.toHaveAttribute('data-adaptive-hint', 'ready')
    await page.getByText('컴패니언', { exact: true }).click()
    await page.getByLabel('애니메이션 정지').uncheck()
    await page.getByText('컴패니언', { exact: true }).click()
    await expect(pet).toHaveAttribute('data-adaptive-hint', 'ready', { timeout: 15_000 })
    const expired = page.waitForRequest(request => request.url().includes('/companion/tutor/') && request.url().endsWith('/feedback')
        && request.postDataJSON()?.event === 'EXPIRED', { timeout: 18_000 })
    await expired
    await expect(pet).not.toHaveAttribute('data-adaptive-hint', 'ready')
    expect(hintPosts).toHaveLength(3)
    await page.goto(detail(others[1]))
    expect(await search('메모리 ECC 오류 재진단')).toBeGreaterThan(0)
    await expect(pet).not.toHaveAttribute('data-adaptive-hint', 'ready')
    await pet.press('Enter')
    await expect(chat).toBeVisible()
    await missTwice()
    await page.waitForTimeout(2800)
    await expect(pet).not.toHaveAttribute('data-adaptive-hint', 'ready')
    await chat.getByRole('button', { name: '닫기', exact: true }).click()
    await expect(pet).toHaveAttribute('data-adaptive-hint', 'ready', { timeout: 25_000 })
    await page.goto(detail(others[2]))
    await expect(pet).not.toHaveAttribute('data-adaptive-hint', 'ready')
    expect(hintPosts).toHaveLength(3)

    await missTwice()
    await expect(pet).toHaveAttribute('data-adaptive-hint', 'ready', { timeout: 25_000 })
    await page.route('**/companion/tutor/*/hints', route => route.fulfill({ status: 503,
      json: { error: { code: 'AI_UNAVAILABLE', message: '합성 연결 오류' } } }), { times: 1 })
    await pet.press('Enter')
    await expect(panel).toContainText('합성 연결 오류')
    await expect(panel.getByRole('button', { name: '힌트 다시 요청' })).toBeVisible()
    expect(hintPosts).toHaveLength(4)
    await panel.getByRole('button', { name: '힌트 닫기' }).click()
    await expect(panel).toHaveCount(0)
    expect(hintPosts).toHaveLength(4)
  }
})
