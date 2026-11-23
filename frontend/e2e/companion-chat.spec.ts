import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

const runId = 'b0000000-0000-4000-8000-000000000010'
const first = 'b0000000-0000-4000-8000-000000000011'
const second = 'b0000000-0000-4000-8000-000000000012'
const run = `/simulation-runs/${runId}`
const ticketPath = (id: string) => `${run}/tickets/${id}`
const chat = (page: Page) => page.getByRole('region', { name: '키로 채팅' })
const pet = (page: Page) => page.getByRole('button', { name: '키로 컴패니언' })
const ticket = (id: string) => ({ id, number: id === first ? 'SIM-101' : 'SIM-102', title: id === first ? '메모리 실습' : '스토리지 실습',
  description: '합성 장애', service: 'training', severity: 'P2', status: 'open', detected_at: '2026-01-01T00:00:00Z', version: 1,
  training_origin: 'INITIAL', allowed_transitions: [] })

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/session', route => route.fulfill({ json: {
    user: { id: 'trainee-for-chat', display_name: '교육생' }, roles: ['trainee'], csrf_token: 'synthetic', corpus_training: true,
  } }))
  await page.route(url => url.pathname.startsWith('/api/v1' + run), route => {
    const suffix = new URL(route.request().url()).pathname.slice(7 + run.length)
    const id = /^\/tickets\/([^/]+)$/.exec(suffix)?.[1]
    const data = id ? ticket(id) : suffix === '' ? { status: 'ACTIVE', scenario: { key: 'synthetic-ops-boot', version: '1.0' }, virtual_now: '2026-01-01T00:00:00Z' }
      : suffix === '/tickets' ? { items: [ticket(first), ticket(second)] }
      : suffix === '/schedule' ? { slots: [] }
      : suffix === '/incoming-handover' ? { summary: '', tickets: [] }
      : suffix === '/outgoing-handover' ? { revision: 0, status: 'DRAFT', warnings: [], items: [], content: { summary: '', open_items: [] } }
      : suffix.endsWith('/actions') ? { catalog: [], activity: [] }
      : suffix.endsWith('/operations') ? { version: 1, work: [], history: [] }
      : suffix.endsWith('/host-incidents') ? { incidents: [] } : { items: [] }
    return route.fulfill({ json: data })
  })
})

async function open(page: Page) {
  await pet(page).focus()
  await page.keyboard.press('Enter')
}

test('힌트가 우선 열리고 추가 질문으로만 채팅 컨텍스트를 전달한다', async ({ page }) => {
  await page.route('**/api/v1/companion/chat/**', route => route.fulfill({ json: { turns: [] } }))
  await page.route('**/api/v1/companion/chat', route => route.fulfill({ status: 429, json: { error: { code: 'CHAT_RATE_LIMIT', message: '잠시 뒤 다시 시도하세요.' } } }))
  await page.goto(run)
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  await open(page)
  await expect(page.getByRole('group', { name: '키로 힌트' })).toBeVisible()
  await expect(chat(page)).toHaveCount(0)
  await page.getByRole('button', { name: '키로에게 질문', exact: true }).click()
  const input = chat(page).getByLabel('키로에게 질문')
  await expect(input).toBeFocused()
  await expect(chat(page)).toContainText('이 질문에 현재 힌트를 연결합니다.')
  await input.fill('힌트 설명')
  await input.press('Shift+Enter')
  await input.pressSequentially('please')
  await expect(input).toHaveValue('힌트 설명\nplease')
  const request = page.waitForRequest('**/api/v1/companion/chat')
  await input.press('Enter')
  expect((await request).postDataJSON()).toMatchObject({ message: '힌트 설명\nplease', active_hint_id: 'TICKET_LIST_READY',
    page_context: { run_id: runId, page_id: 'workspace' } })
  await expect(chat(page).getByRole('alert')).toHaveText(/잠시 뒤 다시 시도하세요/)
  await expect(chat(page).getByRole('button', { name: '질문 다시 전송' })).toBeVisible()
})

test('응답 대기 중 티켓을 바꾸고 다음 질문부터 새 티켓을 사용하며 초기화한다', async ({ page }) => {
  type Payload = { conversation_id: string; request_id: string; message: string; page_context: { ticket_id: string } }
  const requests: Payload[] = []
  let complete = false
  const view = (body: Payload, index: number) => ({ ...body, message_id: body.request_id, sources: [], retrieval: 'empty',
    status: complete || index > 0 ? 'succeeded' : 'running', context_info: { ...body.page_context, label: body.page_context.ticket_id === first ? '메모리 실습' : '스토리지 실습' },
    answer: complete || index > 0 ? '**관측**\n\n```text\nobs\n```\n\n<img src=x onerror=alert(1)>' : null })
  await page.route('**/api/v1/companion/chat/**', route => {
    if (route.request().method() === 'DELETE') { requests.length = 0; return route.fulfill({ status: 204 }) }
    return route.fulfill({ json: { turns: requests.map(view) } })
  })
  await page.route('**/api/v1/companion/chat', route => {
    const body = route.request().postDataJSON() as Payload
    requests.push(body)
    return route.fulfill({ status: 202, json: view(body, requests.length - 1) })
  })
  await page.goto(ticketPath(first) + '#incidents')
  await expect(pet(page)).toBeVisible()
  await open(page)
  await chat(page).getByLabel('키로에게 질문').fill('첫 질문')
  await chat(page).getByRole('button', { name: '전송' }).click()
  await expect(pet(page)).toHaveAttribute('data-state', 'thinking')
  await page.goto(ticketPath(second) + '#incidents')
  await expect(chat(page)).toContainText('현재 화면 · 실습 티켓 SIM-102')
  complete = true
  await expect(chat(page).locator('article').first()).toContainText('메모리 실습')
  await expect(chat(page).locator('pre code')).toHaveText('obs')
  await expect(chat(page)).toContainText('관련 승인 SOP 검색 결과가 없습니다.')
  await expect(chat(page).locator('img[onerror]')).toHaveCount(0)
  await chat(page).getByLabel('키로에게 질문').fill('다음 질문')
  await chat(page).getByRole('button', { name: '전송' }).click()
  await expect(chat(page).locator('article').last()).toContainText('스토리지 실습')
  expect(requests.map(request => request.page_context.ticket_id)).toEqual([first, second])
  expect(requests[0].conversation_id).toBe(requests[1].conversation_id)
  await chat(page).getByRole('button', { name: '닫기', exact: true }).click()
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  await open(page)
  await expect(chat(page)).toContainText('첫 질문')
  await chat(page).getByRole('button', { name: '대화 초기화', exact: true }).click()
  await expect(chat(page)).not.toContainText('첫 질문')
})
