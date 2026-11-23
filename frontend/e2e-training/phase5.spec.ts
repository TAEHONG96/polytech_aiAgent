import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

const password = 'synthetic-phase5-browser-password'
const api = '/api/v1'
const completion = '저장된 보고서를 최종 확정하고 실습을 종료합니다.'
const memory = '메모리 진단 경보'

function qaCommand(args: string[]) {
  const project = process.env.QA_PHASE5_PROJECT
  if (!project || !/^polytech-qa-phase5(?:-[a-z0-9]+)*$/.test(project)) throw new Error('Set QA_PHASE5_PROJECT to the isolated Fake QA project.')
  const networks = process.env.QA_PHASE5_NETWORKS
  execFileSync('docker', ['compose', '--env-file', '.env.example', '-p', project, '-f', 'compose.yaml',
    '-f', 'compose.override.yaml', ...(networks ? ['-f', networks] : []), ...args],
  { cwd: resolve(process.cwd(), '..'), timeout: 35_000, stdio: ['ignore', 'pipe', 'pipe'] })
}

const control = (...args: string[]) => qaCommand(['run', '--rm', '--no-deps', '-T', 'test', 'python', '-m', 'tests.qa_phase5_control', ...args])

async function account(page: Page, join = false) {
  const username = 'p5-' + randomUUID()
  await page.goto('/login')
  if (join) {
    await page.getByRole('link', { name: '교육 계정 만들기' }).click()
    await page.getByLabel('사용자명').fill(username)
    await page.getByLabel('비밀번호').fill(password)
    await page.getByRole('button', { name: '교육 계정 만들기' }).click()
    await expect(page.getByRole('status')).toContainText('교육 계정이 만들어졌습니다')
  } else {
    control('provision', username)
  }
  await page.getByLabel('사용자명').fill(username)
  await page.getByLabel('비밀번호').fill(password)
  await page.getByRole('button', { name: '로그인', exact: true }).click()
  await expect(page).toHaveURL(/\/simulation$/)
}

async function start(page: Page) {
  await page.getByRole('button', { name: 'dco-basic v1.1 시작', exact: true }).click()
  await expect(page).toHaveURL(/\/simulation-runs\/[\da-f-]{36}$/)
  await expect(page.locator('#tickets')).toContainText(memory)
  return new URL(page.url()).pathname
}

async function ticket(page: Page, title = memory) {
  await page.locator('#tickets').getByRole('link', { name: title, exact: true }).click()
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
  return new URL(page.url()).pathname
}

async function writes(page: Page, origin: string) {
  const session = await page.request.get(api + '/auth/session')
  expect(session.status()).toBe(200)
  return { Origin: origin, 'X-CSRF-Token': (await session.json()).csrf_token }
}

async function finalize(page: Page) {
  const outgoing = page.locator('#outgoing')
  await outgoing.getByRole('checkbox', { name: completion }).check()
  const posted = page.waitForResponse(response => response.url().endsWith('/outgoing-handover/finalize') && response.request().method() === 'POST')
  await outgoing.getByRole('button', { name: '인계 최종 확정' }).click()
  expect((await posted).status()).toBe(200)
  await expect(page.getByRole('heading', { name: '실습 평가', exact: true })).toBeVisible()
}

async function action(page: Page, label: string, message: RegExp) {
  const actions = page.locator('#actions')
  const button = actions.getByRole('button', { name: label, exact: true })
  const card = actions.locator('.training-tile').filter({ has: page.getByRole('button', { name: label, exact: true }) })
  await button.click()
  await expect(card.getByText('수행 완료', { exact: true })).toBeVisible()
  await expect(card.getByText(message)).toBeVisible()
  await expect(button).toBeDisabled()
  await expect(actions.locator('ol').getByText(message)).toBeVisible()
}

async function memoryActions(page: Page) {
  await action(page, 'BMC 진단', /가상 메모리 진단을 실행했습니다/)
  await action(page, '메모리 점검', /가상 메모리 슬롯의 오류를 확인했습니다/)
  await action(page, '가상 메모리 교체', /가상 메모리를 교체했습니다/)
}

test('가입·로그인·모바일 근무 시작과 Bot 인계를 건너 자유 탐색한다', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 850 })
  await account(page, true)
  await page.reload()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '본문으로 이동' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('main')).toBeFocused()
  const run = await start(page)
  await expect(page.locator('#schedule').getByRole('listitem').filter({ hasText: 'Bot' })).toHaveCount(2)
  await expect(page.getByRole('heading', { name: '이전 Bot 인계 보고서' })).toBeVisible()
  await page.locator('#schedule').getByRole('link', { name: '스토리지 응답 지연' }).click()
  await expect(page).toHaveURL(new RegExp(run + '/tickets/'))
  await page.getByRole('navigation', { name: '티켓 실습 도구' }).getByRole('link', { name: '가상 조치' }).click()
  await expect(page.locator('#actions')).toBeFocused()
  await expect(page.locator('#actions').getByRole('button', { name: 'BMC 진단' })).toBeEnabled()
  await page.getByRole('navigation', { name: '실습 탐색' }).getByRole('link', { name: '발신 인계' }).click()
  await expect(page.locator('#outgoing')).toBeFocused()
  await expect(page.locator('#outgoing')).toContainText('미해결 티켓이 있어도 확정할 수 있습니다')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
})

test('실제 가상 조치와 정상 재진단 뒤 저장된 티켓을 해결한다', async ({ page }) => {
  await account(page)
  const run = await start(page)
  const target = await ticket(page)
  await memoryActions(page)
  await action(page, '재진단', /가상 재진단을 통과했습니다/)
  await page.reload()
  await expect(page.locator('.training-actions').getByRole('button', { name: '재진단', exact: true })).toBeDisabled()
  await expect(page.locator('.training-actions').getByText('가상 재진단을 통과했습니다.')).toBeVisible()
  const heading = page.locator('#ticket-summary')
  await heading.getByText('티켓 상태 변경', { exact: true }).click()
  for (const state of ['in_progress', 'resolved']) {
    await heading.getByLabel('다음 상태').selectOption(state)
    await heading.getByLabel('변경 사유').fill('가상 진단 결과를 확인했습니다.')
    if (state === 'resolved') await heading.getByLabel('해결 요약').fill('가상 메모리 교체와 재진단 결과 정상')
    const saved = page.waitForResponse(response => response.url().endsWith('/transitions'))
    await heading.getByRole('button', { name: '상태 저장' }).click()
    expect((await saved).status()).toBe(200)
  }
  await expect(heading).toContainText('해결 기록 가상 메모리 교체와 재진단 결과 정상')
  expect((await (await page.request.get(api + target)).json()).status).toBe('resolved')
  await page.goto(run + '#outgoing')
  await finalize(page)
  await page.locator('#evaluation').getByRole('button', { name: memory, exact: true }).click()
  await expect(page.locator('#evaluation')).toContainText('조치 후 재검증 · 충족')
  const work = page.locator('#evaluation').getByRole('link', { name: '[행동 기록]', exact: true }).first()
  await work.click()
  await expect(page).toHaveURL(new RegExp(target + '#work$'))
  await expect(page.locator('#work')).toBeFocused()
})

test('무조치 인계 확정은 경고와 미관측 평가를 표시하고 재실습에서 이력을 보존한다', async ({ page }) => {
  await account(page)
  const run = await start(page)
  await finalize(page)
  const evaluation = page.locator('#evaluation')
  await expect(evaluation).toContainText('대응 기록 없음')
  await expect(evaluation).toContainText('가상 조치 타당성 · 미관측')
  await expect(page.locator('#outgoing').getByRole('heading', { name: '확정 경고' })).toBeVisible()
  await expect(page.locator('#outgoing').getByRole('button', { name: '인계 최종 확정' })).toHaveCount(0)
  await page.getByRole('link', { name: '내 실습', exact: true }).click()
  await expect(page.getByRole('link', { name: '완료 결과 보기' })).toHaveAttribute('href', run + '#evaluation')
  const replay = await start(page)
  expect(replay).not.toBe(run)
  await page.goto(run + '#evaluation')
  await expect(evaluation).toContainText('가상 조치 타당성 · 미관측')
})

test('선행 조건에 어긋난 조치를 거부하고 재검증 누락을 설명한다', async ({ page, baseURL }) => {
  await account(page)
  const run = await start(page)
  const target = await ticket(page)
  await expect(page.locator('#actions').getByRole('button', { name: '가상 메모리 교체' })).toBeDisabled()
  const headers = { ...await writes(page, baseURL!), 'Idempotency-Key': randomUUID() }
  const wrong = await page.request.post(api + target + '/actions', {
    headers, data: { action_code: 'REPLACE_MEMORY', parameters: {} },
  })
  expect(wrong.status()).toBe(409)
  expect((await wrong.json()).error.code).toBe('ACTION_PRECONDITION_FAILED')
  await memoryActions(page)
  await page.goto(run + '#outgoing')
  await finalize(page)
  await page.locator('#evaluation').getByRole('button', { name: memory, exact: true }).click()
  await expect(page.locator('#evaluation')).toContainText('조치 후 재검증 · 보완 필요')
  await expect(page.locator('#evaluation').getByRole('heading', { name: 'AI 피드백' })).toBeVisible()
  await expect(page.locator('#evaluation')).not.toContainText('수행한 가상 조치 · RERUN_DIAGNOSTICS')
})

test('Fake 공급자의 분석과 설명이 실패해도 규칙 근거와 인계가 유지된다', async ({ page }) => {
  await account(page)
  const run = await start(page)
  const target = await ticket(page)
  qaCommand(['stop', 'worker'])
  try {
    const analysis = page.locator('#analysis')
    await analysis.getByLabel('분석 질문').fill('합성 메모리 오류의 점검 순서를 제안해 주세요.')
    await analysis.getByRole('button', { name: '분석 요청' }).click()
    await expect(analysis.getByRole('status')).toContainText('분석 중')
    const analysisUrl = page.url()
    await page.goto(run + '#outgoing')
    await finalize(page)
    control('fail', run.split('/').at(-1)!)
    await expect(page.locator('#evaluation').getByRole('status')).toContainText('AI 설명 실패')
    await expect(page.locator('#evaluation').getByRole('status')).toContainText('규칙 평가 준비됨')
    await expect(page.locator('#evaluation')).toContainText('미해결 업무 인계 · 보완 필요')
    await page.goto(analysisUrl)
    await expect(page.locator('#analysis').getByRole('alert')).toContainText('AI 분석 실패')
    await expect(page.locator('#analysis').getByRole('button', { name: '분석 요청' })).toHaveCount(0)
    expect(new URL(page.url()).pathname).toBe(target)
  } finally { qaCommand(['start', 'worker']) }
})

test('동일 계정 두 탭이 동시에 서로 다른 요청으로 확정하면 한 번만 완료한다', async ({ page, context }) => {
  await account(page)
  const run = await start(page)
  const outgoing = page.locator('#outgoing')
  await outgoing.getByRole('button', { name: '인계 자료 다시 수집' }).click()
  await expect(outgoing).toContainText('초안 · 리비전 1')
  const other = await context.newPage()
  await other.goto(run + '#outgoing')
  const tabs = [page, other]
  const waiting: Route[] = []
  for (const tab of tabs) {
    await tab.locator('#outgoing').getByRole('checkbox', { name: completion }).check()
    await tab.route('**/outgoing-handover/finalize', async route => {
      waiting.push(route)
      if (waiting.length === 2) await Promise.all(waiting.map(request => request.continue()))
    })
  }
  const replies = tabs.map(tab => tab.waitForResponse(response => response.url().endsWith('/outgoing-handover/finalize')))
  await Promise.all(tabs.map(tab => tab.locator('#outgoing').getByRole('button', { name: '인계 최종 확정' }).click()))
  const responses = await Promise.all(replies)
  expect(responses.map(response => response.status()).sort()).toEqual([200, 409])
  const loser = tabs[responses.findIndex(response => response.status() === 409)].locator('#outgoing')
  await expect(loser.getByRole('alert')).toContainText('보고서 확정 또는 수정 충돌')
  await loser.getByRole('button', { name: '최신 보고서 열기' }).click()
  await expect(loser).toContainText('확정 · 읽기 전용 · 리비전 2')
  await other.close()
})

test('다른 사용자와 동일 사용자 다른 실행의 직접 주소 및 공유 운영 API를 거부한다', async ({ page, browser, baseURL }) => {
  await account(page)
  const first = await start(page)
  const firstTicket = await ticket(page)
  await page.goto(first + '#outgoing')
  await finalize(page)
  const other = await browser.newPage({ baseURL })
  try {
    await account(other)
    expect((await other.request.get(api + '/tickets')).status()).toBe(403)
    expect((await other.request.get(api + first + '/evaluation')).status()).toBe(404)
    await other.goto(firstTicket)
    await expect(other.getByRole('alert')).toContainText('실습 실행을 찾을 수 없습니다')
  } finally { await other.close() }
  await page.goto('/simulation')
  const second = await start(page)
  await finalize(page)
  const wrong = second + '/tickets/' + firstTicket.split('/').at(-1)
  await page.goto(wrong)
  await expect(page.getByRole('alert')).toContainText('티켓을 찾을 수 없습니다')
  expect((await page.request.get(api + second + '/evaluation/tickets/' + firstTicket.split('/').at(-1))).status()).toBe(404)
})

test('승인 SOP와 과거 사고의 링크, 검색 및 고정 평가 근거는 승인 철회를 따르도록 검증한다', async ({ page, baseURL }) => {
  await account(page)
  const run = await start(page)
  const target = await ticket(page)
  const incident = await (await page.request.get(api + target + '/host-incidents')).json()
  const [past] = incident.items
  expect(past.incident_key).toBe('SYN-MEM-01')
  await expect.poll(async () => {
    const list = await (await page.request.get(api + run + '/sops')).json()
    return list.items.find((item: { title: string; status: string }) => item.title.includes('dco-basic 1.1') && item.status === 'COMPLETED')?.id
  }).toBeTruthy()
  const sopLink = page.locator('#knowledge').getByRole('link', { name: '합성 데이터센터 메모리 진단 · dco-basic 1.1', exact: true })
  const sopPath = (await sopLink.getAttribute('href'))!
  const sopId = sopPath.split('/').at(-1)!
  const pastPath = past.source_url.replace(api, '')
  await page.locator('#incidents').getByRole('link', { name: past.title }).click()
  await expect(page.getByRole('heading', { name: past.title, exact: true })).toBeVisible()
  await page.goto(sopPath)
  await expect(page.getByRole('link', { name: 'SOP 원본 열기' })).toBeVisible()
  await page.goto(target)
  const headers = await writes(page, baseURL!)
  for (const [kind, source_id] of [['HOST_HISTORY', past.id], ['SOP', sopId]]) {
    expect((await page.request.post(api + target + '/learning-evidence', { headers, data: { kind, source_id, activity: 'VIEWED' } })).status()).toBe(201)
  }
  await page.goto(run + '#outgoing')
  await finalize(page)
  await page.locator('#evaluation').getByRole('button', { name: memory, exact: true }).click()
  await expect(page.locator('#evaluation').getByRole('link', { name: '[과거 장애]', exact: true }).first()).toHaveAttribute('href', pastPath)
  control('approval', 'sop', sopId, 'no')
  try {
    control('approval', 'incident', past.id, 'no')
    try {
      for (const path of [sopPath, sopPath + '/original', pastPath]) expect((await page.request.get(api + path)).status()).toBe(404)
      const search = await page.request.post(api + run + '/knowledge/search', { headers,
        data: { ticket_id: target.split('/').at(-1), source_types: ['SOP', 'INCIDENT'], query: '메모리 ECC 오류 가상 교체 MEM_ECC_ERROR' } })
      expect(search.status()).toBe(200)
      expect((await search.json()).results.every((item: { source_id: string }) => ![sopId, past.id].includes(item.source_id))).toBeTruthy()
      await page.reload()
      await page.locator('#evaluation').getByRole('button', { name: memory, exact: true }).click()
      await expect(page.locator('#evaluation')).toContainText('규칙 평가 준비됨')
      await expect(page.locator('#evaluation').getByRole('link', { name: '[과거 장애]', exact: true })).toHaveCount(0)
      await expect(page.locator('#evaluation').getByRole('link', { name: '[SOP]', exact: true })).toHaveCount(0)
      await page.goto(pastPath)
      await expect(page.getByRole('alert')).toContainText('승인된 사고 근거를 찾을 수 없습니다')
    } finally { control('approval', 'incident', past.id, 'yes') }
  } finally { control('approval', 'sop', sopId, 'yes') }
})
