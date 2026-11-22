import { expect, test } from '@playwright/test'
import type { ConsoleMessage, Page, Response } from '@playwright/test'
import { randomUUID } from 'node:crypto'

const password = 'synthetic-qa-password'

async function login(page: Page, username: string) {
  await page.goto('/login', { waitUntil: 'commit' })
  await page.getByLabel('사용자명').fill(username)
  await page.getByLabel('비밀번호').fill(password)
  let consoleErrorCount = 0
  let scriptErrorCount = 0
  const onConsole = (message: ConsoleMessage) => {
    if (message.type() === 'error') consoleErrorCount += 1
  }
  const onScriptError = () => { scriptErrorCount += 1 }
  page.on('console', onConsole)
  page.on('pageerror', onScriptError)
  const started = performance.now()
  const [, loginResponse, tickets] = await Promise.all([
    page.waitForURL(url => url.pathname === '/tickets', { waitUntil: 'commit' }),
    perform(page, '/auth/login', 'POST', 200, () => page.getByRole('button', { name: '로그인', exact: true }).click()),
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/tickets'
      && response.request().method() === 'GET'),
  ])
  expect(tickets.status()).toBe(200)
  await tickets.finished()
  await page.getByRole('heading', { name: '장애 티켓', exact: true }).waitFor()
  const screenReadyMs = performance.now() - started
  const sessionStarted = performance.now()
  const session = await page.request.get('/api/v1/auth/session')
  expect(session.status()).toBe(200)
  const sessionProbeMs = Math.round(performance.now() - sessionStarted)
  page.off('console', onConsole)
  page.off('pageerror', onScriptError)
  const navigation = await page.evaluate(() => {
    const documentTiming = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
    return documentTiming && {
      path: new URL(documentTiming.name).pathname,
      responseEndMs: documentTiming.responseEnd,
      domInteractiveMs: documentTiming.domInteractive,
    }
  })
  expect(navigation?.path).toBe('/login')
  const timing = {
    path: new URL(page.url()).pathname,
    loginStatus: loginResponse.status(),
    ticketListStatus: tickets.status(),
    ticketListMs: tickets.request().timing().responseEnd,
    sessionStatus: session.status(),
    sessionProbeMs,
    screenReadyMs: Math.round(screenReadyMs),
    navigation,
    consoleErrorCount,
    scriptErrorCount,
  }
  await test.info().attach('로그인 및 티켓 화면 시간', {
    body: JSON.stringify(timing), contentType: 'application/json',
  })
  if (process.env.QA_BROWSER_TIMING === '1') console.info('QA_LOGIN ' + JSON.stringify(timing))
}

async function perform(page: Page, path: string, method: string, status: number, action: () => Promise<void>) {
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1' + path && response.request().method() === method),
    action(),
  ])
  expect(response.status()).toBe(status)
  return response
}

async function result(response: Response) {
  return await response.json()
}

async function authenticate(page: Page, origin: string, username: string) {
  const loginResponse = await page.request.post('/api/v1/auth/login', {
    headers: { Origin: origin }, data: { username, password },
  })
  expect(loginResponse.status()).toBe(200)
  return { Origin: origin, 'X-CSRF-Token': (await loginResponse.json()).csrf_token as string }
}

const symptoms = '서버 부팅 실패와 메인보드 오류 POST B101'

async function seedTicket(page: Page, headers: Record<string, string>) {
  const response = await page.request.post('/api/v1/tickets', { headers, data: {
    title: '합성 B101 ' + randomUUID(), description: symptoms, service: 'QA synthetic board',
    severity: 'P1', detected_at: new Date().toISOString(),
  } })
  expect(response.status()).toBe(201)
  return response.headers().location.replace('/api/v1', '')
}

async function seedReport(page: Page, headers: Record<string, string>, shift: string) {
  const ticketPath = await seedTicket(page, headers)
  const ticketId = ticketPath.split('/').at(-1)
  const preview = await page.request.get(`/api/v1/shift-instances/${shift}/handover-preview`)
  expect(preview.status()).toBe(200)
  const { items }: { items: { id: string }[] } = await preview.json()
  const created = await page.request.post('/api/v1/handovers', { headers, data: {
    outgoing_shift_id: shift, include_ticket_ids: [ticketId],
    exclude_ticket_ids: items.map(item => item.id).filter(id => id !== ticketId),
  } })
  expect(created.status()).toBe(201)
  return created.headers().location.replace('/api/v1', '')
}

test('로그인 후 티켓 화면이 준비되고 브라우저 기록을 보존한다', async ({ page }) => {
  await login(page, 'qa-outgoing')
  await page.getByRole('link', { name: '새 티켓' }).waitFor()
  await page.goBack({ waitUntil: 'commit' })
  await page.waitForURL(url => url.pathname === '/login', { waitUntil: 'commit' })
  await page.getByRole('heading', { name: '장애 티켓', exact: true }).waitFor()
})

test('장애 접수와 새로고침 후 티켓 조회', async ({ page, baseURL }) => {
  const title = '합성 B101 <b>문자</b> ' + randomUUID()
  await authenticate(page, baseURL!, 'qa-outgoing')
  await page.goto('/tickets/new', { waitUntil: 'commit' })
  await page.getByLabel('제목').fill(title)
  await page.getByLabel('증상').fill(symptoms)
  await page.getByLabel('영향 서비스').fill('QA synthetic board')
  await page.getByLabel('우선순위').selectOption('P1')
  const created = await perform(page, '/tickets', 'POST', 201,
    () => page.getByRole('button', { name: '저장', exact: true }).click())
  const ticketPath = created.headers().location.replace('/api/v1', '')
  await page.waitForURL(url => url.pathname === ticketPath, { waitUntil: 'commit' })
  const ticketHeading = page.getByRole('heading', { name: title, exact: true })
  await ticketHeading.waitFor()
  await expect(ticketHeading.locator('b')).toHaveCount(0)
  await page.reload({ waitUntil: 'commit' })
  await page.getByText(symptoms, { exact: true }).waitFor()
})

test('근거 조회와 장애 분석', async ({ page, baseURL }) => {
  const headers = await authenticate(page, baseURL!, 'qa-outgoing')
  const ticketPath = await seedTicket(page, headers)
  const ticketId = ticketPath.slice('/tickets/'.length)
  await page.goto('/knowledge/search', { waitUntil: 'commit' })
  await page.getByLabel('장애 상황').fill(symptoms)
  // Pause the real request so the loading indicator is observable at any worker speed.
  await page.route('**/api/v1/knowledge/search', async route => {
    await expect(page.getByRole('status')).toHaveText('검색 중입니다.')
    await route.continue()
  }, { times: 1 })
  const searchResponse = await perform(page, '/knowledge/search', 'POST', 200,
    () => page.getByRole('button', { name: '검색', exact: true }).click())
  const search = await result(searchResponse)
  expect(search.results.map((hit: { source_type: string }) => hit.source_type)).toEqual(expect.arrayContaining(['SOP', 'TICKET']))
  await expect(page.getByRole('link', { name: '[가상 SOP] 서버 부팅 실패와 메인보드 오류' })).toBeVisible()

  await page.goto('/agent?ticket=' + encodeURIComponent(ticketId), { waitUntil: 'commit' })
  await page.getByLabel('분석할 장애').fill(symptoms)
  const analysis = await perform(page, '/agent/analyze', 'POST', 202,
    () => page.getByRole('button', { name: '분석 요청' }).click())
  expect(analysis.request().postDataJSON().ticket_id).toBe(ticketId)
  await expect(page.getByRole('heading', { name: '분석 요약' })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('heading', { name: '참고 출처' })).toBeVisible()
  await expect(page.getByText('fake-analysis-v1', { exact: false })).toBeVisible()
})

test('티켓 화면에서 후속 작업을 기록한다', async ({ page, baseURL }) => {
  const headers = await authenticate(page, baseURL!, 'qa-outgoing')
  const ticketPath = await seedTicket(page, headers)
  await page.goto(ticketPath, { waitUntil: 'commit' })
  await page.getByRole('button', { name: '후속 작업 추가' }).click()
  await page.getByLabel('작업명').fill('합성 진단 관측 기록')
  await page.getByLabel('작업 상태').selectOption('done')
  await perform(page, ticketPath + '/work', 'POST', 201,
    () => page.getByRole('button', { name: '작업 저장' }).click())
  await page.getByText('합성 진단 관측 기록', { exact: true }).waitFor()
})

test('지연된 회차 응답 뒤에도 올바른 회차에서 인계 초안을 작성한다', async ({ page, baseURL }) => {
  const shift = process.env.QA_SHIFT_ID
  if (!shift) throw new Error('Set QA_SHIFT_ID from the isolated test roster.')
  const headers = await authenticate(page, baseURL!, 'qa-outgoing')
  await seedTicket(page, headers)

  // Deliver the first development effect's roster response after the second one and the operator's selection.
  let releaseRoster!: () => void
  const release = new Promise<void>(resolve => { releaseRoster = resolve })
  await page.route('**/api/v1/shift-instances', async route => {
    const response = await route.fetch()
    await release
    await route.fulfill({ response })
  }, { times: 1 })
  await page.goto('/handovers', { waitUntil: 'commit' })
  const selectedShift = page.getByLabel('인계 근무 회차')
  const preview = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/' + shift + '/handover-preview'))
  await selectedShift.selectOption(shift)
  expect((await preview).status()).toBe(200)
  const delayedRoster = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/shift-instances'))
  releaseRoster()
  await (await delayedRoster).finished()
  await expect(selectedShift).toHaveValue(shift)
  const createReport = page.getByRole('button', { name: '보고서 초안 만들기' })
  const report = await perform(page, '/handovers', 'POST', 201, () => createReport.click())
  expect(report.request().postDataJSON().outgoing_shift_id).toBe(shift)
  const reportPath = report.headers().location.replace('/api/v1', '')
  await page.waitForURL(url => url.pathname === reportPath, { waitUntil: 'commit' })
  await page.getByRole('heading', { name: '추가 확인 요청' }).first().waitFor()
})

test('인계 초안을 분석하고 수정한 내용을 다시 연다', async ({ page, baseURL }) => {
  const shift = process.env.QA_EDIT_SHIFT_ID
  if (!shift) throw new Error('Set QA_EDIT_SHIFT_ID to a third outgoing shift from the test roster.')
  const headers = await authenticate(page, baseURL!, 'qa-outgoing')
  const reportPath = await seedReport(page, headers, shift)
  await page.goto(reportPath, { waitUntil: 'commit' })
  await perform(page, reportPath + '/analyze', 'POST', 202, () => page.getByRole('button', { name: 'AI 분석 실행' }).click())
  await expect(page.getByRole('heading', { name: '주요 장애' })).toBeVisible({ timeout: 20_000 })
  await page.getByLabel('인계 요약').fill('합성 인계 요약')
  await perform(page, reportPath, 'PUT', 200, () => page.getByRole('button', { name: '초안 저장' }).click())
  await page.reload({ waitUntil: 'commit' })
  await page.getByLabel('인계 요약').waitFor()
  await expect(page.getByLabel('인계 요약')).toHaveValue('합성 인계 요약')
})

test('인계 준비 검증, 보고서 확정과 다음 교대 인수', async ({ page, browser, baseURL }) => {
  const shift = process.env.QA_APPROVAL_SHIFT_ID
  if (!shift) throw new Error('Set QA_APPROVAL_SHIFT_ID to a second outgoing shift from the test roster.')
  const headers = await authenticate(page, baseURL!, 'qa-outgoing')
  const reportPath = await seedReport(page, headers, shift)
  await page.goto(reportPath, { waitUntil: 'commit' })
  await page.getByLabel('인계 요약').fill('합성 인계 요약')
  await perform(page, reportPath, 'PUT', 200, () => page.getByRole('button', { name: '초안 저장' }).click())
  const ready = page.getByRole('button', { name: '인계 준비 완료' })
  await perform(page, reportPath, 'PUT', 422, () => ready.click())
  await expect(page.getByRole('alert')).toContainText('모든 누락 항목의 답변')
  for (const answer of await page.getByPlaceholder('답변 또는 미확인 사유').all()) {
    await answer.fill('합성 자료 재확인 예정')
  }
  await perform(page, reportPath, 'PUT', 200, () => ready.click())
  const confirmed = await result(await perform(page, reportPath + '/confirm', 'POST', 200,
    () => page.getByRole('button', { name: '최종 확정' }).click()))
  expect(confirmed.status).toBe('CONFIRMED')

  const receivingPage = await browser.newPage({ baseURL })
  try {
    await authenticate(receivingPage, baseURL!, 'qa-incoming')
    await receivingPage.goto(reportPath, { waitUntil: 'commit' })
    const accepted = await result(await perform(receivingPage, reportPath + '/acknowledge', 'POST', 200,
      () => receivingPage.getByRole('button', { name: '인수 확인' }).click()))
    expect(accepted.status).toBe('ACKNOWLEDGED')
    expect(accepted.items).toEqual(confirmed.items)
    await receivingPage.reload({ waitUntil: 'commit' })
    await receivingPage.getByText('합성 인계 요약', { exact: true }).waitFor()
    await expect(receivingPage.getByRole('button', { name: '인수 확인' })).toHaveCount(0)
  } finally {
    await receivingPage.close()
  }
})

test('Nginx는 상태 점검을 제한하고 크기 및 로그인 한도를 알린다', async ({ request, baseURL }) => {
  expect((await request.get('/health')).status()).toBe(200)
  expect((await request.get('/api/v1/ready')).status()).toBe(404)
  const preflight = await request.fetch('/api/v1/tickets', {
    method: 'OPTIONS', headers: { Origin: 'https://qa-other.invalid', 'Access-Control-Request-Method': 'POST' },
  })
  expect(preflight.headers()['access-control-allow-origin']).toBeUndefined()
  const otherOrigin = await request.post('/api/v1/auth/login', {
    headers: { Origin: 'https://qa-other.invalid' }, data: { username: 'qa-outgoing', password },
  })
  expect(otherOrigin.status()).toBe(403)

  const oversized = await request.post('/api/v1/tickets', { data: { description: 'x'.repeat(129 * 1024) } })
  expect(oversized.status()).toBe(413)
  expect(oversized.headers()['content-type']).toContain('application/json')
  expect((await oversized.json()).error).toMatchObject({
    code: 'REQUEST_TOO_LARGE', request_id: oversized.headers()['x-request-id'],
  })
  const oversizedSop = await request.post('/api/v1/sops', { multipart: {
    file: { name: 'qa.txt', mimeType: 'text/plain', buffer: Buffer.alloc(8 * 1024 * 1024, 'x') },
  } })
  expect(oversizedSop.status()).toBe(413)
  expect((await oversizedSop.json()).error.code).toBe('REQUEST_TOO_LARGE')

  // A sequential series can refill the leaky bucket while it waits for the API.
  const attempts = await Promise.all(Array.from({ length: 18 }, () =>
    request.post('/api/v1/auth/login', {
      headers: { Origin: baseURL! }, data: { username: 'qa-outgoing', password: 'synthetic-wrong-password' },
    }),
  ))
  const limited = attempts.filter(response => response.status() === 429)
  expect(attempts.map(response => response.status()).every(status => status === 401 || status === 429)).toBe(true)
  expect(limited.length).toBeGreaterThan(0)
  for (const response of limited) {
    expect(response.headers()['content-type']).toContain('application/json')
    expect((await response.json()).error).toMatchObject({
      code: 'SIGNIN_RATE_LIMIT', request_id: response.headers()['x-request-id'],
    })
  }
})
