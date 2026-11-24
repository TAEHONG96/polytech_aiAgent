import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

const compare = '현재 티켓에 맞는 SOP를 찾아서 어떤 문서가 가장 관련성이 높은지 설명해 줘.'
const compareOpen = '현재 티켓에 맞는 SOP를 찾아서 어떤 문서가 가장 관련성이 높은지 설명해 주고, 내가 선택한 문서를 열어줘.'
const explain = '인수인계 페이지로 이동해서 어떤 항목을 확인해야 하는지 설명해 줘.'
const highlight = '인수인계 페이지로 이동해서 중요한 섹션을 강조하고 어떤 항목을 확인해야 하는지 설명해 줘.'

type Turn = {
  message_id: string; execution_mode: string; answer: string | null;
  workflow: { status: string; model_calls: number; input_kind: string | null;
    outcome?: { incomplete: string[] }; goal_requirements: { id: string; status: string }[] };
  sop_comparison: { reason: string; candidates: { sop_id: string; title: string; scope?: string }[] } | null;
  action_proposal: { id: string; options?: { sop_id: string; title: string }[] } | null;
}

test('preserves SOP comparisons and continues handover goals after confirmed navigation', async ({ page, baseURL }, info) => {
  const provider = process.env.QA_WORKFLOW_SMOKE
  test.skip(provider !== 'gemini' && provider !== 'mock', 'Requires a seeded synthetic corpus')
  test.setTimeout(360_000)
  const origin = baseURL!
  const credentials = { username: `kiro-cont-${randomUUID().slice(0, 8)}`, password: `Synthetic-${randomUUID()}` }
  const common = { Origin: origin }
  expect((await page.request.post('/api/v1/auth/register', { headers: common, data: credentials })).status()).toBe(201)
  const login = await page.request.post('/api/v1/auth/login', { headers: common, data: credentials })
  expect(login.ok()).toBeTruthy()
  const headers = { ...common, 'X-CSRF-Token': (await login.json()).csrf_token, 'Idempotency-Key': randomUUID() }
  const start = await page.request.post('/api/v1/simulation-runs', { headers,
    data: { scenario_key: 'synthetic-ops-memory', scenario_version: '1.0' } })
  expect(start.status()).toBe(201)
  const apiRun = start.headers().location
  const run = apiRun.slice('/api/v1'.length)
  const { items } = await (await page.request.get(`${apiRun}/tickets?scope=all`)).json() as {
    items: { id: string; title: string; status: string }[]
  }
  const memory = items.find(ticket => /ECC/i.test(ticket.title) && ticket.status !== 'resolved')!
  expect(memory).toBeDefined()
  const detail = `${run}/tickets/${memory.id}#incidents`
  const other = `${run}/tickets/${items.find(ticket => ticket.id !== memory.id)!.id}#incidents`
  await page.goto(detail)
  await page.getByRole('button', { name: '키로 컴패니언' }).focus()
  await page.keyboard.press('Enter')
  const chat = page.getByRole('region', { name: '키로 채팅' })
  const section = page.locator('[data-companion-anchor="outgoing-handover"]')

  async function ask(question: string) {
    const posted = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/companion/chat')
    const input = chat.getByLabel('키로에게 질문')
    await expect(input).toBeEnabled()
    await input.fill(question)
    await input.press('Enter')
    const response = await posted
    expect(response.status()).toBe(202)
    const { conversation_id, message_id } = await response.json()
    const article = chat.locator('article').filter({ hasText: question }).last()
    const flow = article.getByRole('region', { name: '키로 워크플로' })
    const action = article.getByRole('group', { name: '키로 실행 제안' })
    async function expectState(status: string, name: string) {
      let turn!: Turn
      await expect.poll(async () => {
        const { turns } = await (await page.request.get(`/api/v1/companion/chat/${conversation_id}`)).json() as { turns: Turn[] }
        turn = turns.find(item => item.message_id === message_id)!
        const current = turn?.workflow?.status ?? ''
        return current === status || ['COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(current)
      }, { timeout: 65_000 }).toBeTruthy()
      await info.attach(name, { body: JSON.stringify(turn), contentType: 'application/json' })
      expect(turn.workflow.status).toBe(status)
      expect(turn.execution_mode).toBe(provider)
      return turn
    }
    return { article, flow, action, expectState }
  }

  const b1 = await ask(compare)
  const summary = await b1.expectState('COMPLETED', 'B-1')
  expect(summary.workflow.model_calls).toBe(2)
  expect(summary.sop_comparison!.candidates[0].scope).toBeTruthy()
  expect(summary.sop_comparison!.candidates.length).toBeGreaterThan(1)
  expect(summary.workflow.outcome?.incomplete).toEqual([])
  await expect(b1.article.getByRole('region', { name: '승인 SOP 관련성 비교' })).toBeVisible()
  await expect(b1.action).toHaveCount(0)
  await expect(page).toHaveURL(origin + detail)

  const b2 = await ask(compareOpen)
  const choosing = await b2.expectState('WAITING_USER_INPUT', 'B-2 비교')
  expect(choosing.workflow.input_kind).toBe('sop_selection')
  await expect(b2.article.getByRole('region', { name: '승인 SOP 관련성 비교' })).toContainText(choosing.sop_comparison!.reason)
  await expect(b2.action.getByRole('button', { name: '문서 열기' })).toHaveCount(0)
  const selected = choosing.action_proposal!.options![1]
  await b2.action.getByRole('radio', { name: selected.title, exact: true }).check()
  await b2.action.getByRole('button', { name: '이 문서 선택' }).click()
  const approval = await b2.expectState('WAITING_APPROVAL', 'B-2 승인')
  expect(approval.workflow.goal_requirements.find(goal => goal.id === 'USER_SELECT_SOP')?.status).toBe('COMPLETED')
  await expect(page).toHaveURL(origin + detail)
  await b2.action.getByRole('button', { name: '문서 열기' }).click()
  await expect(page.locator('[data-companion-destination="sop"]')).toHaveAttribute('data-companion-source-id', selected.sop_id)
  const opened = await b2.expectState('COMPLETED', 'B-2 완료')
  expect(opened.answer).toContain(selected.title)
  expect(opened.sop_comparison).toEqual(choosing.sop_comparison)

  const e1 = await ask(explain)
  const before = await e1.expectState('WAITING_APPROVAL', 'E-1 이동 대기')
  expect(before.workflow.model_calls).toBe(1)
  expect(before.answer).toBeNull()
  let release!: () => void
  let requested!: () => void
  const draftRequested = new Promise<void>(resolve => { requested = resolve })
  const waitForDraft = new Promise<void>(resolve => { release = resolve })
  const draftUrl = `**${apiRun}/outgoing-handover`
  await page.route(draftUrl, async route => { requested(); await waitForDraft; await route.continue() }, { times: 1 })
  try {
    await e1.action.getByRole('button', { name: '이동', exact: true }).click()
    await draftRequested
    await expect(page).toHaveURL(`${origin}${run}#outgoing`)
    await expect(section).toHaveCount(0)
    await expect(e1.article).not.toContainText('이전 기록과 현재 기록')
  } finally { release() }
  const continued = await e1.expectState('COMPLETED', 'E-1 완료')
  expect(continued.workflow.model_calls).toBe(2)
  expect(continued.answer).toMatch(/이전 Bot/)
  expect(continued.answer).toMatch(/인계 요약|티켓별 다음 조치/)

  await page.goto(detail)
  const e2 = await ask(highlight)
  const navigate = await e2.expectState('WAITING_APPROVAL', 'E-2 이동 대기')
  await e2.action.getByRole('button', { name: '이동', exact: true }).click()
  await expect(e2.action.getByRole('button', { name: '강조', exact: true })).toBeVisible({ timeout: 65_000 })
  const next = await e2.expectState('WAITING_APPROVAL', 'E-2 강조 대기')
  expect(next.action_proposal!.id).not.toBe(navigate.action_proposal!.id)
  expect(next.workflow.model_calls).toBe(2)
  expect(next.answer).toContain('미완료 업무와 다음 조치')
  await expect(section).not.toHaveClass(/companion-action-highlight/)
  await e2.action.getByRole('button', { name: '강조', exact: true }).click()
  await expect(section).toHaveClass(/companion-action-highlight/)
  const emphasized = await e2.expectState('COMPLETED', 'E-2 완료')
  expect(emphasized.workflow.outcome?.incomplete).toEqual([])
  expect(emphasized.answer).toBe(next.answer)

  if (provider === 'mock') {
    const g = await ask(highlight)
    await g.expectState('WAITING_APPROVAL', 'G 강조 대기')
    await section.evaluate(node => node.removeAttribute('data-companion-anchor'))
    await g.action.getByRole('button', { name: '강조', exact: true }).click()
    const partial = await g.expectState('FAILED', 'G 부분 실패')
    expect(partial.answer).toContain('미완료 업무와 다음 조치')
    expect(partial.workflow.outcome?.incomplete).toEqual(['발신 인계 섹션 강조'])
    await expect(g.article.getByRole('region', { name: '키로 작업 결과' })).toContainText('미완료 작업')

    await page.goto(detail)
    const f = await ask(highlight)
    await f.expectState('WAITING_APPROVAL', 'F 이동 대기')
    await page.goto(other)
    await f.expectState('PAUSED', 'F 다른 티켓')
    await expect(f.action.getByRole('button', { name: '이동', exact: true })).toHaveCount(0)
    await page.goto(detail)
    await f.flow.getByRole('button', { name: '작업 재개' }).click()
    await expect(f.action.getByRole('button', { name: '이동', exact: true })).toBeVisible()
    await page.reload()
    await f.expectState('PAUSED', 'F 새로고침')
    await expect(f.flow.getByRole('button', { name: '작업 취소' })).toBeVisible()
  }
})
