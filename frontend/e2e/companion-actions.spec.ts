import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

type Ticket = { id: string; title: string; description: string }
type Proposal = { id: string; view_id: string; type: string; status: string; options?: { sop_id: string; title: string }[] }
type Turn = { message_id: string; status: string; action_proposal: Proposal | null; execution_mode: string;
  answer?: string; error?: { code: string }; context_info: Record<string, unknown> }

test('approves only scoped Kiro actions and confirms the visible result', async ({ page, baseURL }, info) => {
  const mode = process.env.QA_ACTION_SMOKE
  test.skip(mode !== 'gemini' && mode !== 'mock', 'Requires an isolated synthetic corpus and explicit expected provider')
  test.setTimeout(360_000)
  const origin = baseURL!
  const credentials = { username: `kiro-action-${randomUUID().slice(0, 8)}`, password: `Synthetic-${randomUUID()}` }
  const common = { Origin: origin }
  expect((await page.request.post('/api/v1/auth/register', { headers: common, data: credentials })).status()).toBe(201)
  const login = await page.request.post('/api/v1/auth/login', { headers: common, data: credentials })
  expect(login.ok()).toBeTruthy()
  const headers = { ...common, 'X-CSRF-Token': (await login.json()).csrf_token as string }
  const start = await page.request.post('/api/v1/simulation-runs', { headers: { ...headers, 'Idempotency-Key': randomUUID() },
    data: { scenario_key: 'synthetic-ops-memory', scenario_version: '1.0' } })
  expect(start.status()).toBe(201)
  const apiRun = start.headers().location
  const run = apiRun.slice('/api/v1'.length)
  const { items: tickets } = await (await page.request.get(`${apiRun}/tickets?scope=all`)).json() as { items: Ticket[] }
  const memory = tickets.find(ticket => /ECC/i.test(ticket.title)) ?? tickets.find(ticket => /ECC/i.test(ticket.description))!
  expect(memory).toBeDefined()
  const other = tickets.find(ticket => ticket.id !== memory.id)!
  await page.goto(`${run}#knowledge`)
  const pet = page.getByRole('button', { name: '키로 컴패니언' })
  await pet.focus()
  await page.keyboard.press('Enter')
  const chat = page.getByRole('region', { name: '키로 채팅' })
  let conversation = ''

  async function ask(label: string, question: string, kind: string | null) {
    const response = page.waitForResponse(item => item.request().method() === 'POST'
      && new URL(item.url()).pathname === '/api/v1/companion/chat')
    const input = chat.getByLabel('키로에게 질문')
    await expect(input).toBeEnabled()
    await input.fill(question)
    await expect(chat.getByRole('button', { name: '전송' })).toBeEnabled()
    await input.press('Enter')
    const posted = await response
    expect(posted.status()).toBe(202)
    const receipt = await posted.json() as { conversation_id: string; message_id: string }
    conversation = receipt.conversation_id
    const historyUrl = `/api/v1/companion/chat/${conversation}`
    let turn: Turn | undefined
    await expect.poll(async () => {
      const history = await (await page.request.get(historyUrl)).json() as { turns: Turn[] }
      turn = history.turns.find(item => item.message_id === receipt.message_id)
      return turn && (turn.status === 'succeeded' || turn.status === 'failed')
    }, { timeout: 85_000 }).toBe(true)
    const result = turn!
    await info.attach(label, { body: JSON.stringify(result), contentType: 'application/json' })
    expect(result.error).toBeNull()
    expect(result.execution_mode).toBe(mode)
    expect(result.action_proposal?.type ?? null).toBe(kind)
    const article = chat.locator('article').filter({ hasText: question }).last()
    if (kind) await expect(article.getByRole('group', { name: '키로 실행 제안' })).toBeVisible()
    return { result, article, card: article.getByRole('group', { name: '키로 실행 제안' }), historyUrl }
  }

  const a = await ask('A', '인수인계 페이지로 이동해 줘.', 'navigate_to_page')
  await expect(page).toHaveURL(`${origin}${run}#knowledge`)
  await a.card.getByRole('button', { name: '이동', exact: true }).click()
  await expect(page).toHaveURL(`${origin}${run}#outgoing`)
  await expect(a.card).toContainText('요청하신 화면으로 이동했어요.')

  const c = await ask('C', 'SOP 검색 버튼이 어디 있어?', 'highlight_element')
  const search = page.locator('[data-companion-anchor="search-sop"]')
  await expect(search).not.toHaveClass(/companion-action-highlight/)
  await c.card.getByRole('button', { name: '강조' }).click()
  await expect(search).toHaveClass(/companion-action-highlight/)
  await expect(c.card).toContainText('해당 위치를 강조했어요.')
  await expect(search).not.toHaveClass(/companion-action-highlight/)

  const d = await ask('D', '티켓 검색창으로 이동해 줘.', 'focus_input')
  const ticketSearch = page.getByRole('searchbox', { name: '티켓 검색' })
  await expect(ticketSearch).not.toBeFocused()
  await d.card.getByRole('button', { name: '검색창으로 이동' }).click()
  await expect(ticketSearch).toBeFocused()
  await expect(ticketSearch).toHaveValue('')
  await expect(d.card).toContainText('검색창에 포커스를 옮겼어요.')

  const ticketLocation = `${run}/tickets/${memory.id}#incidents`
  await page.goto(ticketLocation)
  const b = await ask('B', '지금 티켓과 관련된 SOP를 열어줘.', 'open_sop')
  const options = b.result.action_proposal!.options!
  const chosen = options.find(option => /ECC/i.test(option.title)) ?? options[0]
  expect(chosen).toBeDefined()
  await expect(page).toHaveURL(`${origin}${ticketLocation}`)
  const open = b.card.getByRole('button', { name: '문서 열기' })
  if (options.length > 1) await expect(open).toBeDisabled()
  await b.card.getByRole('radio', { name: chosen.title, exact: true }).check()
  await open.click()
  await expect(page).toHaveURL(`${origin}${run}/sops/${chosen.sop_id}`)
  await expect(page.getByRole('heading', { name: chosen.title, exact: true })).toBeVisible()
  await expect(page.locator('[data-companion-destination="sop"]')).toHaveAttribute('data-companion-source-id', chosen.sop_id)
  await expect(b.card).toContainText('승인 SOP 문서를 열었어요.')

  const e = await ask('E', 'POST와 BMC의 차이점은?', null)
  if (mode === 'gemini') {
    expect(e.result.answer).toMatch(/POST/i)
    expect(e.result.answer).toMatch(/BMC/i)
  }
  await expect(e.card).toHaveCount(0)

  await page.goto(ticketLocation)
  const f = await ask('F', '이 티켓에 필요한 SOP 문서를 열어줘.', 'open_sop')
  const pending = f.result.action_proposal!
  await page.goto(`${run}/tickets/${other.id}#incidents`)
  await expect(f.card.getByRole('button', { name: '문서 열기' })).toHaveCount(0)
  await expect.poll(async () => {
    const history = await (await page.request.get(f.historyUrl)).json() as { turns: Turn[] }
    return history.turns.find(turn => turn.message_id === f.result.message_id)?.action_proposal?.status
  }).toBe('stale')
  const approval = await page.request.post(`${f.historyUrl}/actions/${f.result.message_id}`, { headers,
    data: { proposal_id: pending.id, view_id: pending.view_id, decision: 'approve',
      sop_id: pending.options![0].sop_id, page_context: { page_id: 'ticket', run_id: run.split('/').at(-1),
        ticket_id: memory.id, section: 'incidents' } } })
  expect(approval.status()).toBe(409)
  expect((await approval.json()).error.code).toBe('ACTION_STALE')
  await expect(page).toHaveURL(`${origin}${run}/tickets/${other.id}#incidents`)
  expect(conversation).toBeTruthy()

  if (mode === 'mock') {
    const missing = await ask('G-target', 'SOP 검색 버튼이 어디 있어?', 'highlight_element')
    await search.evaluate(element => { element.setAttribute('hidden', '') })
    await missing.card.getByRole('button', { name: '강조' }).click()
    await expect(missing.card).toContainText('현재 화면에서 대상 요소를 찾을 수 없어요.')
    await expect.poll(async () => {
      const history = await (await page.request.get(missing.historyUrl)).json() as { turns: Turn[] }
      return history.turns.find(turn => turn.message_id === missing.result.message_id)?.action_proposal?.status
    }).toBe('failed')
    const home = await ask('G-home', '내 실습으로 이동해 줘.', 'navigate_to_page')
    await home.card.getByRole('button', { name: '이동', exact: true }).click()
    await expect(page).toHaveURL(`${origin}/simulation`)
    await expect(home.card).toContainText('요청하신 화면으로 이동했어요.')
  }
})
