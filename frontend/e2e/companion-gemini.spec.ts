import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'

type Ticket = { id: string; title: string; description: string }
type Turn = { message_id: string; status: string; execution_mode: string; intent: string; retrieval: string;
  model: string; answer: string; context_info: { ticket_id: string }; error?: { code: string; message: string };
  sources: { title: string; kind: string; source_url: string }[] }

test('Gemini answers A–E using the active synthetic ticket and verified SOPs', async ({ page, baseURL }, info) => {
  test.skip(process.env.QA_GEMINI_SMOKE !== '1', 'Requires a dedicated Gemini-enabled synthetic corpus')
  test.setTimeout(300_000)
  const origin = baseURL!
  const credentials = { username: `kiro-gemini-${randomUUID().slice(0, 8)}`, password: `Synthetic-${randomUUID()}` }
  const sessionHeaders = { Origin: origin }
  expect((await page.request.post('/api/v1/auth/register', { headers: sessionHeaders, data: credentials })).status()).toBe(201)
  const login = await page.request.post('/api/v1/auth/login', { headers: sessionHeaders, data: credentials })
  expect(login.ok()).toBeTruthy()
  const headers = { ...sessionHeaders, 'X-CSRF-Token': (await login.json()).csrf_token as string }
  let activeRun: string | undefined

  async function selectTicket(scenario: string, symptom: RegExp) {
    const start = await page.request.post('/api/v1/simulation-runs', { headers: { ...headers, 'Idempotency-Key': randomUUID() },
      data: { scenario_key: `synthetic-ops-${scenario}`, scenario_version: '1.0', pause_active_run_id: activeRun } })
    expect(start.status()).toBe(201)
    const runLink = start.headers().location
    activeRun = runLink.split('/').at(-1)
    const data = await (await page.request.get(`${runLink}/tickets?scope=all`)).json() as { items: Ticket[] }
    const ticket = data.items.find(item => symptom.test(`${item.title} ${item.description}`))
    expect(ticket, `${scenario} synthetic ticket fixture`).toBeDefined()
    await page.goto(`${runLink.slice('/api/v1'.length)}/tickets/${ticket!.id}#incidents`)
    return ticket!
  }

  const chat = page.getByRole('region', { name: '키로 채팅' })
  async function ask(label: string, question: string, intent: string, ticket: Ticket) {
    const started = Date.now()
    const sent = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/companion/chat')
    const input = chat.getByLabel('키로에게 질문')
    await expect(input).toBeEnabled()
    await input.fill(question)
    await input.press('Enter')
    const queued = await sent
    expect(queued.status()).toBe(202)
    const receipt = await queued.json() as { conversation_id: string; message_id: string }
    let turn: Turn | undefined
    await expect.poll(async () => {
      const history = await (await page.request.get(`/api/v1/companion/chat/${receipt.conversation_id}`)).json() as { turns: Turn[] }
      turn = history.turns.find(item => item.message_id === receipt.message_id)
      return turn && ['succeeded', 'failed'].includes(turn.status)
    }, { timeout: 80_000 }).toBe(true)
    const completed = turn!
    await info.attach(label, { body: JSON.stringify({ question, elapsed_ms: Date.now() - started, ...completed }, null, 2),
      contentType: 'application/json' })
    expect(completed.error, `${label} provider error`).toBeNull()
    expect(completed).toMatchObject({ execution_mode: 'gemini', intent, context_info: { ticket_id: ticket.id } })
    expect(completed.model).toMatch(/^gemini-/)
    const article = chat.locator('article').filter({ hasText: question })
    await expect(article.getByText('Gemini 응답', { exact: true })).toBeVisible()
    await expect(chat.getByRole('button', { name: '전송' })).toBeDisabled()
    return completed
  }

  const boot = await selectTicket('boot', /POST/i)
  const pet = page.getByRole('button', { name: '키로 컴패니언' })
  await pet.focus()
  await page.keyboard.press('Enter')
  const a = await ask('A', '지금 보고 있는 티켓이 무슨 작업이야?', 'TICKET_SUMMARY', boot)
  expect(a.answer).toMatch(/POST|부팅/i)
  expect(a.retrieval).toBe('not_required')

  const memory = await selectTicket('memory', /ECC/i)
  const b = await ask('B', '이 티켓에서는 무엇부터 확인해야 해?', 'TROUBLESHOOTING', memory)
  expect(b.answer).toMatch(/ECC|메모리|DIMM/i)
  expect(b.answer).not.toContain(boot.title)
  const c = await ask('C', '이 장애와 관련된 SOP를 찾아줘.', 'SOP_LOOKUP', memory)
  expect(c.retrieval).toBe('found')
  expect(c.sources.length).toBeGreaterThan(0)
  for (const source of c.sources) {
    expect(source.kind).toBe('SOP')
    expect(source.source_url).toMatch(/^\/api\/v1\/simulation-runs\/[\da-f-]+\/sops\/[\da-f-]+$/)
    const document = await page.request.get(source.source_url)
    expect(document.ok()).toBeTruthy()
    expect((await document.json()).title).toBe(source.title)
  }
  const d = await ask('D', 'POST와 BMC의 차이점은 무엇이야?', 'GENERAL_QUESTION', memory)
  expect(d.answer).toMatch(/POST/i)
  expect(d.answer).toMatch(/BMC/i)
  expect(d.retrieval).toBe('not_required')
  expect(d.sources).toEqual([])

  const e = await ask('E', '그다음에는 무엇을 확인해야 해?', 'FOLLOW_UP', memory)
  expect(e.answer).toMatch(/ECC|메모리|DIMM|슬롯/i)
  expect(e.answer).not.toContain(boot.title)
  expect(e.answer).not.toMatch(/(?:모듈|DIMM)\s*(?:자체의?\s*)?불량이\s*아니므로/i)
  expect(e.answer).not.toBe(b.answer)
})
