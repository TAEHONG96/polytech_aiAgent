import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { contextEvents, selectHints } from '../src/companion/companion-hints'
import type { CompanionPageContext } from '../src/companion/companion-context'

const run = '/simulation-runs/a0000000-0000-4000-8000-000000000010'
const firstId = 'a0000000-0000-4000-8000-000000000011'
const secondId = 'a0000000-0000-4000-8000-000000000012'
const analysisId = 'a0000000-0000-4000-8000-000000000013'
const first = `${run}/tickets/${firstId}`
const second = `${run}/tickets/${secondId}`
const ticket = (id: string) => ({ id, number: 'SIM-01', title: id === firstId ? '합성 메모리 티켓' : '합성 스토리지 티켓',
  description: '합성 장애 설명', service: 'training', severity: 'P2', status: 'open',
  detected_at: '2026-01-01T00:00:00Z', version: 1, training_origin: 'INITIAL', allowed_transitions: [] })
const pet = (page: Page) => page.getByRole('button', { name: '키로 컴패니언' })
const bubble = (page: Page) => page.getByRole('group', { name: '키로 힌트' })
const fire = (page: Page, type: 'hint-request' | 'hint-dismiss') => page.evaluate(type => {
  window.dispatchEvent(new CustomEvent('dco:companion-command', { detail: { type } }))
}, type)

async function openHint(page: Page) {
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  await expect(bubble(page)).toHaveCount(0)
  await pet(page).focus()
  await page.keyboard.press('Enter')
  await expect(bubble(page)).toBeVisible()
}

test.beforeEach(async ({ page }) => {
  const empty = { items: [], next_cursor: null }
  const resources = {
    '': { status: 'ACTIVE', scenario: { key: 'synthetic-ops-boot', version: '1.0' }, virtual_now: '2026-01-01T00:00:00Z' },
    '/tickets': { items: [ticket(firstId), ticket(secondId)], next_cursor: null },
    '/schedule': { slots: [] }, '/incoming-handover': { summary: '합성 이전 인계', tickets: [{ ticket_id: firstId }] },
    '/outgoing-handover': { revision: 0, status: 'DRAFT', warnings: [], items: [], content: { summary: null, open_items: [] } },
    '/outgoing-handover/revisions/1': { revision: 1, items: [], content: { summary: '합성 확정 인계', open_items: [] } },
  } as Record<string, object>
  await page.route('**/api/v1/auth/session', route => route.fulfill({ json: {
    user: { id: 'synthetic-trainee', display_name: '교육생' }, roles: ['trainee'], csrf_token: 'synthetic', corpus_training: true,
  } }))
  await page.route(url => url.pathname.startsWith('/api/v1' + run), route => {
    const suffix = new URL(route.request().url()).pathname.slice(7 + run.length)
    const id = /^\/tickets\/([^/]+)$/.exec(suffix)?.[1]
    if (id) return route.fulfill({ json: ticket(id) })
    if (suffix.endsWith('/operations')) return route.fulfill({ json: { version: 1, work: [], history: [] } })
    if (suffix.endsWith('/actions')) return route.fulfill({ json: { catalog: [], activity: [] } })
    if (suffix.endsWith('/host-incidents')) return route.fulfill({ json: { incidents: [], host: null } })
    if (suffix === '/knowledge/search') return route.fulfill({ json: { results: [] } })
    return route.fulfill({ json: resources[suffix] ?? empty })
  })
})

test('티켓 목록 힌트는 준비 신호만 표시하고 클릭으로 열리며 페이지를 바꾸면 정리된다', async ({ page }) => {
  await page.clock.install()
  await page.goto(run)
  const link = page.getByRole('region', { name: '실습 티켓 목록' }).getByRole('link', { name: '합성 메모리 티켓' })
  await expect(link).toBeVisible()
  const chatEvents = await page.evaluateHandle(() => {
    const state = { count: 0 }
    window.addEventListener('dco:companion-chat-requested', () => state.count++)
    return state
  })
  const before = await pet(page).boundingBox()
  await openHint(page)
  await expect(bubble(page)).toContainText('우선순위와 처리 상태')
  await page.clock.runFor(11_000)
  await expect(bubble(page)).toBeVisible()
  expect(await pet(page).boundingBox()).toEqual(before)
  expect(await chatEvents.evaluate(state => state.count)).toBe(0)
  await page.keyboard.press('Escape')
  await expect(bubble(page)).toHaveCount(0)
  await expect(pet(page)).toBeFocused()
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')

  await link.click()
  await expect(page.getByRole('heading', { name: '합성 메모리 티켓' })).toBeVisible()
  await openHint(page)
  await expect(bubble(page)).toContainText('장애 증상과 티켓에 기록된 영향 범위')
  await page.goto(second)
  await expect(bubble(page)).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '합성 스토리지 티켓' })).toBeVisible()
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
})

test('빈 SOP 검색은 낮은 우선순위 힌트를 교체하고 타임아웃과 쿨다운을 지킨다', async ({ page }) => {
  await page.clock.install()
  await page.goto(run)
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  async function search() {
    await page.getByLabel('검색어').fill('합성 장애 코드')
    await page.getByRole('button', { name: '검색', exact: true }).click()
    await expect(page.getByText('검색 결과가 없습니다.', { exact: false })).toBeVisible()
  }
  await search()
  await openHint(page)
  await expect(bubble(page)).toContainText('승인 SOP를 찾지 못했어요')
  await expect(page.getByRole('button', { name: '검색', exact: true })).toHaveAttribute('data-companion-highlight', '')
  await page.getByRole('button', { name: '힌트 닫기' }).click()
  await search()
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
  await page.clock.runFor(61_000)
  await search()
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.runFor(10_050)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  await search()
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
})

test('실제 분석 응답과 작업·인계 입력에만 안내가 반응한다', async ({ page }) => {
  await page.route('**/api/v1' + first + '/analyses', route => route.fulfill({ json: {
    analysis_id: analysisId, run_id: analysisId, ticket_id: firstId, status: 'queued', result: null, error: null, tool_calls: [],
  } }))
  await page.route('**/api/v1' + run + '/analyses/' + analysisId, route => route.fulfill({ json: {
    analysis_id: analysisId, run_id: analysisId, ticket_id: firstId, status: 'succeeded', error: null, tool_calls: [],
    result: { summary: '합성 분석', evidence: [], confirmed_facts: [], hypotheses: [], diagnostic_steps: [],
      additional_information: [], limitations: [] },
  } }))
  await page.goto(first + '#analysis')
  await expect(page.getByRole('heading', { name: '합성 메모리 티켓' })).toBeVisible()
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
  await page.getByLabel('분석 질문').fill('합성 질문')
  await page.getByRole('button', { name: '분석 요청', exact: true }).click()
  await openHint(page)
  await expect(bubble(page)).toContainText('근거와 참고 문서')

  await page.getByRole('link', { name: '작업 이력', exact: true }).click()
  await expect(bubble(page)).toHaveCount(0)
  await page.getByRole('button', { name: '후속 작업 추가' }).click()
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
  await page.getByLabel('작업명').fill('합성 작업 입력')
  await openHint(page)
  await expect(bubble(page)).toContainText('이어서 할 작업')

  await page.goto(run + '#schedule')
  await openHint(page)
  await expect(bubble(page)).toContainText('이전 근무조에서 해결하지 못한 티켓')
  await page.goto(run + '#outgoing')
  await openHint(page)
  await expect(bubble(page)).toContainText('인계에 포함된 티켓')
  await fire(page, 'hint-dismiss')
  await page.getByLabel('인계 요약').fill('합성 인계 입력')
  await openHint(page)
  await expect(bubble(page)).toContainText('확인한 사실과 남은 위험')

  await page.goto(run + '/outgoing-handover/revisions/1')
  await expect(page.getByRole('heading', { name: '인계 리비전 1' })).toBeVisible()
  await openHint(page)
  await expect(bubble(page)).toContainText('인계에 포함된 티켓')
  await expect(page.locator('[data-companion-highlight]')).toHaveCount(0)
})

test('개발 버튼의 우선순위, 중복 방지와 드래그 취소를 확인한다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/about')
  await page.getByText('컴패니언', { exact: true }).click()
  const normal = page.getByRole('button', { name: '힌트 테스트', exact: true })
  test.skip(await normal.count() === 0, '개발 전용 테스트 버튼입니다.')
  await normal.click()
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  await page.getByRole('button', { name: '우선 힌트 테스트', exact: true }).click()
  await page.getByText('컴패니언', { exact: true }).click()
  const bounds = (await pet(page).boundingBox())!
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
  await page.mouse.down()
  await page.mouse.move(bounds.x + 20, bounds.y - 20)
  await page.mouse.up()
  await expect(bubble(page)).toHaveCount(0)
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
  await page.getByText('컴패니언', { exact: true }).click()
  await normal.click()
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
})

test('작업 단계 변경은 새 이벤트를 만들고 같은 티켓 상태는 중복 발행하지 않는다', () => {
  const before: CompanionPageContext = { pageId: 'ticket', pathname: first, pageTitle: '실습 티켓', userRole: 'trainee',
    entity: { type: 'ticket', id: firstId }, resources: { ticket: { phase: 'ready', id: firstId, status: 'open', severity: 'P2' } } }
  const current: CompanionPageContext = { ...before, resources: { ticket: { ...before.resources.ticket!, status: 'in_progress' } } }
  expect(contextEvents(current, current)).toEqual([])
  const hints = selectHints(current, contextEvents(current, before))
  expect(hints).toHaveLength(1)
  expect(hints[0].key).toBe(`TICKET_STATUS_CHANGED|ticket|${first}|in_progress`)
})

test('페이지를 떠난 뒤 도착한 SOP 검색 응답은 다음 티켓에 힌트를 만들지 않는다', async ({ page }) => {
  let finish!: () => void
  const wait = new Promise<void>(resolve => { finish = resolve })
  await page.route('**/api/v1' + run + '/knowledge/search', async route => {
    await wait
    await route.fulfill({ json: { results: [] } }).catch(() => undefined)
  })
  try {
    await page.goto(run + '#knowledge')
    await expect(pet(page)).toBeVisible()
    const requested = page.waitForRequest('**/api/v1' + run + '/knowledge/search')
    await page.getByLabel('검색어').fill('합성 장애 검색')
    await page.getByRole('button', { name: '검색', exact: true }).click()
    await requested
    await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
    await page.goto(second + '#incidents')
    await expect(page.getByRole('heading', { name: '합성 스토리지 티켓' })).toBeVisible()
  } finally {
    finish()
  }
  await fire(page, 'hint-request')
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
  await expect(bubble(page)).toHaveCount(0)
})

test('읽는 중에는 높은 우선순위 이벤트가 덮어쓰지 않고 정지 설정을 지킨다', async ({ page }) => {
  await page.goto(run)
  await openHint(page)
  await page.getByLabel('검색어').fill('합성 장비')
  await page.getByRole('button', { name: '검색', exact: true }).click()
  await expect(page.getByText('검색 결과가 없습니다.', { exact: false })).toBeVisible()
  await expect(bubble(page)).toContainText('우선순위와 처리 상태')
  await fire(page, 'hint-dismiss')
  await fire(page, 'hint-request')
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  await page.getByText('컴패니언', { exact: true }).click()
  await page.getByLabel('애니메이션 정지').check()
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', {
    detail: { type: 'hint', text: '합성 외부 힌트' },
  })))
  await expect(pet(page)).not.toHaveAttribute('data-hint', 'ready')
})

test('긴 힌트 본문을 마우스 휠과 키보드로 스크롤할 수 있다', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 600 })
  await page.goto(run + '#knowledge')
  await expect(pet(page)).toBeVisible()
  await page.evaluate(text => window.dispatchEvent(new CustomEvent('dco:companion-command', {
    detail: { type: 'hint', text },
  })), '관측 시각과 현재 장비 상태를 기록하고, 승인 문서의 점검 항목과 비교해 보세요. '.repeat(20))
  await openHint(page)

  const answer = bubble(page).getByRole('status')
  expect(await answer.evaluate(node => node.scrollHeight)).toBeGreaterThan(await answer.evaluate(node => node.clientHeight))
  const bounds = (await answer.boundingBox())!
  const initialPageScroll = await page.evaluate(() => window.scrollY)
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
  await page.mouse.wheel(0, 120)
  await expect.poll(() => answer.evaluate(node => node.scrollTop), { timeout: 2000 }).toBeGreaterThan(0)
  expect(await page.evaluate(() => window.scrollY)).toBe(initialPageScroll)

  await answer.press('Home')
  await expect.poll(() => answer.evaluate(node => node.scrollTop)).toBe(0)
  await expect(bubble(page).getByRole('button', { name: '힌트 닫기' })).toBeInViewport()
})

test('걷는 도중 힌트를 준비하고 누른 사이 만료되면 채팅으로 이어지지 않는다', async ({ page }) => {
  await page.clock.install()
  await page.goto('/about')
  await expect(pet(page)).toBeVisible()
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now() + 300)))
  const initial = (await pet(page).boundingBox())!
  await page.evaluate(({ x, y }) => window.dispatchEvent(new CustomEvent('dco:companion-command', {
    detail: { type: 'move', position: { x: x - 100, y } },
  })), initial)
  await expect(pet(page)).toHaveAttribute('data-state', 'walking')
  const chatEvents = await page.evaluateHandle(() => {
    const state = { count: 0 }
    window.addEventListener('dco:companion-chat-requested', () => state.count++)
    return state
  })
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', {
    detail: { type: 'hint', text: '합성 힌트' },
  })))
  await expect(pet(page)).toHaveAttribute('data-hint', 'ready')
  const stopped = (await pet(page).boundingBox())!
  await page.clock.runFor(9_950)
  expect(await pet(page).boundingBox()).toEqual(stopped)
  await page.mouse.move(stopped.x + stopped.width / 2, stopped.y + stopped.height / 2)
  await page.mouse.down()
  await page.clock.runFor(100)
  await page.mouse.up()
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  await expect(bubble(page)).toHaveCount(0)
  expect(await chatEvents.evaluate(state => state.count)).toBe(0)
})
