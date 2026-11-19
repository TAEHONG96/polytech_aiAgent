import { expect, test } from '@playwright/test'
import type { Page, Response } from '@playwright/test'
import { randomUUID } from 'node:crypto'

const password = 'synthetic-qa-password'

async function login(page: Page, username: string) {
  await page.goto('/login')
  await page.getByLabel('사용자명').fill(username)
  await page.getByLabel('비밀번호').fill(password)
  await perform(page, '/auth/login', 'POST', 200, () => page.getByRole('button', { name: '로그인', exact: true }).click())
  await expect(page).toHaveURL(/\/tickets$/)
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

test('장애 접수, 근거 조회, 보고서 편집·확정과 다음 교대 인수', async ({ page, browser }) => {
  const shift = process.env.QA_SHIFT_ID
  if (!shift) throw new Error('Set QA_SHIFT_ID from the isolated test roster.')
  const title = '합성 B101 <b>문자</b> ' + randomUUID()
  const symptoms = '서버 부팅 실패와 메인보드 오류 POST B101'
  await login(page, 'qa-outgoing')
  await page.getByRole('link', { name: '새 티켓' }).click()
  await page.getByLabel('제목').fill(title)
  await page.getByLabel('증상').fill(symptoms)
  await page.getByLabel('영향 서비스').fill('QA synthetic board')
  await page.getByLabel('우선순위').selectOption('P1')
  const created = await perform(page, '/tickets', 'POST', 201,
    () => page.getByRole('button', { name: '저장', exact: true }).click())
  const ticketPath = created.headers().location.replace('/api/v1', '')
  await expect(page).toHaveURL(new RegExp(ticketPath + '$'))
  const ticketHeading = page.getByRole('heading', { name: title, exact: true })
  await expect(ticketHeading).toBeVisible()
  await expect(ticketHeading.locator('b')).toHaveCount(0)
  await page.reload()
  await expect(page.getByText(symptoms, { exact: true })).toBeVisible()

  await page.getByRole('link', { name: '지식 검색' }).click()
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

  await page.goto(ticketPath)
  await page.getByRole('link', { name: 'AI 분석', exact: true }).click()
  await page.getByLabel('분석할 장애').fill(symptoms)
  await perform(page, '/agent/analyze', 'POST', 202, () => page.getByRole('button', { name: '분석 요청' }).click())
  await expect(page.getByRole('heading', { name: '분석 요약' })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('heading', { name: '참고 출처' })).toBeVisible()
  await expect(page.getByText('fake-analysis-v1', { exact: false })).toBeVisible()

  await page.goto(ticketPath)
  await page.getByRole('button', { name: '후속 작업 추가' }).click()
  await page.getByLabel('작업명').fill('합성 진단 관측 기록')
  await page.getByLabel('작업 상태').selectOption('done')
  await perform(page, ticketPath + '/work', 'POST', 201,
    () => page.getByRole('button', { name: '작업 저장' }).click())
  await expect(page.getByText('합성 진단 관측 기록', { exact: true })).toBeVisible()

  // Deliver the first development effect's roster response after the second one and the operator's selection.
  let releaseRoster!: () => void
  const release = new Promise<void>(resolve => { releaseRoster = resolve })
  await page.route('**/api/v1/shift-instances', async route => {
    const response = await route.fetch()
    await release
    await route.fulfill({ response })
  }, { times: 1 })
  await page.getByRole('link', { name: '교대 인수인계' }).click()
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
  await expect(page).toHaveURL(new RegExp(reportPath + '$'))
  await expect(page.getByRole('heading', { name: '추가 확인 요청' }).first()).toBeVisible()
  await perform(page, reportPath + '/analyze', 'POST', 202, () => page.getByRole('button', { name: 'AI 분석 실행' }).click())
  await expect(page.getByRole('heading', { name: '주요 장애' })).toBeVisible({ timeout: 20_000 })
  await page.getByLabel('인계 요약').fill('합성 인계 요약')
  await perform(page, reportPath, 'PUT', 200, () => page.getByRole('button', { name: '초안 저장' }).click())
  await page.reload()
  await expect(page.getByLabel('인계 요약')).toHaveValue('합성 인계 요약')
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

  const receivingPage = await browser.newPage({ baseURL: process.env.QA_BASE_URL ?? 'http://127.0.0.1:18085' })
  try {
    await login(receivingPage, 'qa-incoming')
    await receivingPage.goto(reportPath)
    const accepted = await result(await perform(receivingPage, reportPath + '/acknowledge', 'POST', 200,
      () => receivingPage.getByRole('button', { name: '인수 확인' }).click()))
    expect(accepted.status).toBe('ACKNOWLEDGED')
    expect(accepted.items).toEqual(confirmed.items)
    await receivingPage.reload()
    await expect(receivingPage.getByText('합성 인계 요약', { exact: true })).toBeVisible()
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

  for (let attempt = 0; attempt < 12; attempt += 1) {
    const response = await request.post('/api/v1/auth/login', {
      headers: { Origin: baseURL! }, data: { username: 'qa-outgoing', password: 'synthetic-wrong-password' },
    })
    if (response.status() === 429) {
      expect(response.headers()['content-type']).toContain('application/json')
      expect((await response.json()).error.code).toBe('SIGNIN_RATE_LIMIT')
      return
    }
    expect(response.status()).toBe(401)
  }
  throw new Error('Expected Nginx to enforce its configured sign-in limit.')
})
