import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

type Turn = {
  message_id: string; status: string; execution_mode: string; answer?: string; error?: { code: string };
  sources: { title: string; source_url: string }[];
  workflow: { status: string; model_calls: number; embedding_calls: number; current_step_index: number;
    steps: { type: string; status: string }[] } | null;
  action_proposal: { id: string; type: string; options?: { title: string; sop_id: string }[] } | null;
}
type Ticket = { id: string; title: string; description: string; status: string }
const review = '지금 티켓을 분석하고 관련 SOP를 찾아서 다음 작업을 알려줘.'
const open = '관련 SOP를 찾아서 열어줘.'
const guide = '티켓 검색 화면으로 이동한 다음 검색창을 찾아줘.'
const reviewAndOpen = '현재 티켓을 분석하고 관련 SOP를 찾아서 다음에 해야 할 작업을 알려줘. 그리고 그 SOP를 열어줘.'

test('runs bounded Kiro workflows and waits for each browser approval', async ({ page, baseURL }, info) => {
  const provider = process.env.QA_WORKFLOW_SMOKE
  test.skip(provider !== 'gemini' && provider !== 'mock', 'Requires an isolated synthetic corpus and explicit expected provider')
  test.setTimeout(480_000)
  const origin = baseURL!
  const credentials = { username: `kiro-flow-${randomUUID().slice(0, 8)}`, password: `Synthetic-${randomUUID()}` }
  const common = { Origin: origin }
  expect((await page.request.post('/api/v1/auth/register', { headers: common, data: credentials })).status()).toBe(201)
  const login = await page.request.post('/api/v1/auth/login', { headers: common, data: credentials })
  expect(login.ok()).toBeTruthy()
  const headers = { ...common, 'X-CSRF-Token': (await login.json()).csrf_token as string, 'Idempotency-Key': randomUUID() }
  const start = await page.request.post('/api/v1/simulation-runs', { headers,
    data: { scenario_key: 'synthetic-ops-memory', scenario_version: '1.0' } })
  expect(start.status()).toBe(201)
  const apiRun = start.headers().location
  const run = apiRun.slice('/api/v1'.length)
  const { items: tickets } = await (await page.request.get(`${apiRun}/tickets?scope=all`)).json() as { items: Ticket[] }
  const memory = tickets.find(ticket => /ECC/i.test(ticket.title) && ['waiting', 'in_progress', 'open'].includes(ticket.status))
    ?? tickets.find(ticket => /ECC/i.test(ticket.description))!
  expect(memory).toBeDefined()
  const other = tickets.find(ticket => ticket.id !== memory.id)!
  const detail = `${run}/tickets/${memory.id}#incidents`
  await page.goto(detail)
  await page.getByRole('button', { name: '키로 컴패니언' }).focus()
  await page.keyboard.press('Enter')
  const chat = page.getByRole('region', { name: '키로 채팅' })
  let historyUrl = ''

  async function read(id: string) {
    const response = await page.request.get(historyUrl)
    const { turns } = await response.json() as { turns: Turn[] }
    return turns.find(turn => turn.message_id === id)
  }

  async function ask(label: string, question: string, expected: string | null) {
    const posted = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/companion/chat')
    const input = chat.getByLabel('키로에게 질문')
    await expect(input).toBeEnabled()
    await input.fill(question)
    await expect(chat.getByRole('button', { name: '전송' })).toBeEnabled()
    await input.press('Enter')
    const response = await posted
    expect(response.status()).toBe(202)
    const receipt = await response.json() as { conversation_id: string; message_id: string }
    historyUrl = `/api/v1/companion/chat/${receipt.conversation_id}`
    const id = receipt.message_id
    let final: Turn | undefined
    await expect.poll(async () => {
      final = await read(id)
      return final?.status === 'succeeded' || final?.status === 'failed'
    }, { timeout: 105_000 }).toBeTruthy()
    const turn = final!
    await info.attach(label, { body: JSON.stringify(turn), contentType: 'application/json' })
    expect(turn.execution_mode).toBe(provider)
    expect(turn.workflow?.status ?? null).toBe(expected)
    const article = chat.locator('article').filter({ hasText: question }).last()
    return { turn, id, article, flow: article.getByRole('region', { name: '키로 워크플로' }),
      action: article.getByRole('group', { name: '키로 실행 제안' }) }
  }

  const a = await ask('A', review, 'COMPLETED')
  expect(a.turn.workflow?.steps.map(step => step.type)).toEqual(['read_ticket', 'search_sop', 'analyze', 'report'])
  expect(a.turn.workflow?.model_calls).toBe(2)
  expect(a.turn.sources.length).toBeGreaterThan(0)
  expect(a.turn.answer).toMatch(/확인|관측/)
  await expect(a.flow).toContainText('4/4단계 완료')

  const b = await ask('B', open, 'WAITING_APPROVAL')
  expect(b.turn.workflow?.model_calls).toBe(1)
  const options = b.turn.action_proposal!.options!
  const selected = options.find(item => item.title.includes('ECC')) ?? options[0]
  await expect(page).toHaveURL(`${origin}${detail}`)
  const openButton = b.action.getByRole('button', { name: '문서 열기' })
  if (options.length > 1) await expect(openButton).toBeDisabled()
  await b.action.getByRole('radio', { name: selected.title, exact: true }).check()
  await openButton.click()
  await expect(page).toHaveURL(`${origin}${run}/sops/${selected.sop_id}`)
  await expect(page.locator('[data-companion-destination="sop"]')).toHaveAttribute('data-companion-source-id', selected.sop_id)
  await expect(b.flow).toContainText('4/4단계 완료')
  expect((await page.request.get(`${apiRun}/sops/${selected.sop_id}`)).ok()).toBeTruthy()
  expect((await read(b.id))?.workflow?.model_calls).toBe(1)

  const c = await ask('C', guide, 'WAITING_APPROVAL')
  await c.action.getByRole('button', { name: '이동', exact: true }).click()
  await expect(page).toHaveURL(`${origin}${run}#tickets`)
  const search = page.getByRole('searchbox', { name: '티켓 검색' })
  const focus = c.action.getByRole('button', { name: '검색창으로 이동' })
  await expect(focus).toBeVisible()
  await expect(search).not.toBeFocused()
  const afterNav = await read(c.id)
  expect(afterNav?.action_proposal?.id).not.toBe(c.turn.action_proposal?.id)
  expect(afterNav?.workflow?.model_calls).toBe(1)
  await focus.click()
  await expect(search).toBeFocused()
  await expect(search).toHaveValue('')
  await expect(c.flow).toContainText('3/3단계 완료')

  const h = await ask('H', 'POST와 BMC의 차이점은?', null)
  expect(h.turn.action_proposal).toBeNull()
  if (provider === 'gemini') {
    expect(h.turn.answer).toMatch(/POST/i)
    expect(h.turn.answer).toMatch(/BMC/i)
  }

  await page.goto(detail)
  const combined = await ask('A+B', reviewAndOpen, 'WAITING_APPROVAL')
  expect(combined.turn.workflow?.steps.map(step => step.type)).toEqual(['read_ticket', 'search_sop', 'analyze', 'report', 'open_sop'])
  expect(combined.turn.workflow?.model_calls).toBe(2)
  expect(combined.turn.workflow?.embedding_calls).toBe(1)
  expect(combined.turn.answer).toMatch(/확인|관측/)
  await expect(combined.flow).toContainText('4/5단계 완료')
  const combinedSelection = combined.turn.action_proposal!.options![0]
  await combined.action.getByRole('radio', { name: combinedSelection.title, exact: true }).check()
  await combined.action.getByRole('button', { name: '문서 열기' }).click()
  await expect(page.locator('[data-companion-destination="sop"]')).toHaveAttribute('data-companion-source-id', combinedSelection.sop_id)
  await expect(combined.flow).toContainText('5/5단계 완료')

  if (provider === 'mock') {
    await page.goto(detail)
    const d = await ask('D', open, 'WAITING_APPROVAL')
    await d.flow.getByRole('button', { name: '작업 취소' }).click()
    await expect(d.flow).toContainText('취소됨')
    await expect(d.action.getByRole('button', { name: '문서 열기' })).toHaveCount(0)
    await expect(page).toHaveURL(`${origin}${detail}`)

    const f = await ask('F', guide, 'WAITING_APPROVAL')
    await page.goto(`${run}/tickets/${other.id}#incidents`)
    await expect(f.flow).toContainText('일시중지')
    await f.flow.getByRole('button', { name: '작업 재개' }).click()
    await expect(chat.getByRole('alert')).toContainText('기존 작업의 실습과 화면으로 돌아간 뒤 재개')
    await page.goto(detail)
    await f.flow.getByRole('button', { name: '작업 재개' }).click()
    await expect(f.action.getByRole('button', { name: '이동', exact: true })).toBeVisible()
    const resumed = await read(f.id)
    expect(resumed?.workflow?.model_calls).toBe(1)
    expect(resumed?.action_proposal?.id).not.toBe(f.turn.action_proposal?.id)
    await f.flow.getByRole('button', { name: '작업 취소' }).click()
    await expect(f.flow).toContainText('취소됨')
  }
})
