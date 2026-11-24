import { expect, test } from '@playwright/test'
import type { Locator, Page } from '@playwright/test'
import type { Session } from '../src/api'

test.use({ viewport: { width: 1280, height: 800 } })

const states = ['idle', 'walking', 'hint', 'chatting', 'thinking', 'happy', 'sleeping', 'surprised']
const runId = 'a0000000-0000-4000-8000-000000000001'
const runPath = `/simulation-runs/${runId}`
const corpusSession = {
  user: { id: 'synthetic-trainee', display_name: '테스트 교육생' },
  roles: ['trainee'], csrf_token: 'synthetic', corpus_training: true,
}
const pet = (page: Page) => page.getByRole('button', { name: '키로 컴패니언' })
const animation = (page: Page) => pet(page).locator('.companion-animation')

async function center(locator: Locator) {
  const box = (await locator.boundingBox())!
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 }
}

async function position(locator: Locator) {
  const { x, y } = (await locator.boundingBox())!
  return { x, y }
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/session', route => route.fulfill({ json: corpusSession }))
  await page.route(url => url.pathname === '/api/v1/tickets', route => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route('**/api/v1/simulation', route => route.fulfill({ json: { active_run_id: runId, available_scenarios: [] } }))
  await page.route('**/api/v1/simulation-runs', route => route.fulfill({ json: { items: [] } }))
  await page.route('**/api/v1/companion/chat/**', route => route.fulfill({ json: { turns: [] } }))
  await page.addInitScript(() => { Math.random = () => 0.6 })
})

test('코퍼스 교육생 로그인 뒤에만 컴패니언을 로드한다', async ({ page }) => {
  const sessionPath = '**/api/v1/auth/session'
  const widget = page.locator('.companion-widget')
  let session: Session | null = { ...corpusSession, roles: ['operator'] }
  await page.route(sessionPath, route => session ? route.fulfill({ json: session }) : route.fulfill({ status: 401 }))
  await page.goto('/tickets')
  await expect(page.getByRole('heading', { name: '장애 티켓' })).toBeVisible()
  await expect(widget).toHaveCount(0)

  session = { ...corpusSession, corpus_training: undefined }
  await page.goto('/simulation')
  await expect(page.getByRole('heading', { name: '내 실습', exact: true })).toBeVisible()
  await expect(widget).toHaveCount(0)

  session = null
  await page.route('**/api/v1/auth/registration', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/v1/auth/login', route => route.fulfill({ json: corpusSession }))
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '로그인' })).toBeVisible()
  await expect(widget).toHaveCount(0)
  await page.getByLabel('사용자명').fill('synthetic-trainee')
  await page.getByLabel('비밀번호').fill('synthetic-password')
  await page.getByRole('button', { name: '로그인', exact: true }).click()
  await expect(page).toHaveURL(/\/simulation$/)
  await expect(pet(page)).toBeVisible()
})

test('개발 도구에서 배경이 투명하고 상태 기호가 포함된 이미지 8장을 전환한다', async ({ page }) => {
  await page.goto('/simulation')
  await page.getByText('컴패니언', { exact: true }).click()
  const preview = page.getByLabel('상태 미리보기')
  test.skip(await preview.count() === 0, '상태 미리보기는 Vite 개발 서버에서만 제공됩니다.')
  await expect(pet(page).locator('img')).toHaveCount(8)
  await expect.poll(() => pet(page).locator('img').evaluateAll(images =>
    images.every(image => image instanceof HTMLImageElement && image.complete && image.naturalWidth > 0),
  )).toBe(true)
  const cutouts = await pet(page).locator('img').evaluateAll(images => {
    const canvas = document.createElement('canvas')
    const context = canvas.getContext('2d', { willReadFrequently: true })!
    return images.map(element => {
      const image = element as HTMLImageElement
      const { naturalWidth: width, naturalHeight: height } = image
      canvas.width = width
      canvas.height = height
      context.drawImage(image, 0, 0)
      const sample = [[0, 0], [width - 1, 0], [0, height - 1], [width - 1, height - 1], [Math.floor(width / 2), Math.floor(height / 2)]]
        .map(([x, y]) => context.getImageData(x, y, 1, 1).data[3])
      const markLeft = Math.floor(width * 0.75)
      const marks = context.getImageData(markLeft, 0, width - markLeft, Math.floor(height * 0.4)).data
      return { sample, hasMark: marks.some((value, index) => index % 4 === 3 && value > 0) }
    })
  })
  const statesWithMarks = new Set(['hint', 'chatting', 'thinking', 'sleeping', 'surprised'])
  expect(cutouts).toEqual(states.map(state => ({ sample: [0, 0, 0, 0, 255], hasMark: statesWithMarks.has(state) })))
  const cursor = await center(pet(page))
  expect(await page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest('.companion-pet')), cursor)).toBe(true)

  const original = await pet(page).locator('[data-image-state=idle]').elementHandle()
  for (const state of states) {
    await preview.selectOption(state)
    await expect(pet(page)).toHaveAttribute('data-state', state)
    const image = pet(page).locator('img:visible')
    await expect(image).toHaveCount(1)
    await expect(image).toHaveAttribute('data-image-state', state)
    await expect(image).toHaveAttribute('src', new RegExp(`/(?:kiro_asset/cutouts|assets)/${state}(?:-[^/.]+)?\\.png$`))
  }
  expect(await original!.evaluate(image => image.isConnected)).toBe(true)
  await preview.selectOption('')
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
})

test('자율 이동은 호버 때 멈추고 다시 진행하며 유휴 시간이 길어지면 잠든다', async ({ page }) => {
  await page.clock.install()
  await page.goto('/simulation')
  await expect(pet(page)).toBeVisible()
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now() + 300)))
  const pause = page.getByLabel('애니메이션 정지')
  await pause.evaluate(element => (element as HTMLInputElement).click())
  await expect(pet(page)).toHaveAttribute('data-paused', 'true')
  await pause.evaluate(element => (element as HTMLInputElement).click())
  await expect(pet(page)).toHaveAttribute('data-paused', 'false')
  const initial = await position(pet(page))
  expect(initial.x).toBeGreaterThan(1100)
  expect(initial.y).toBeGreaterThan(600)

  await page.clock.runFor(12_900)
  await expect(pet(page)).toHaveAttribute('data-state', 'walking')
  await expect(pet(page)).toHaveAttribute('data-direction', 'left')
  const walking = await position(pet(page))
  expect(walking.x).toBeLessThan(initial.x)
  expect(walking.x).toBeGreaterThan(initial.x - 120)

  const cursor = await center(pet(page))
  await page.mouse.move(cursor.x, cursor.y)
  const hovered = await position(pet(page))
  await page.clock.runFor(3_000)
  expect(await position(pet(page))).toEqual(hovered)
  await page.mouse.move(500, 40)
  await page.clock.runFor(2_000)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  expect((await position(pet(page))).x).toBeLessThan(hovered.x)

  await page.clock.runFor(80_000)
  await expect(pet(page)).toHaveAttribute('data-state', 'sleeping')
  const sleeping = await center(pet(page))
  await page.evaluate(() => {
    window.addEventListener('dco:companion-chat-requested', () => { document.body.dataset.chatRequested = 'yes' }, { once: true })
  })
  await page.mouse.move(sleeping.x, sleeping.y)
  await page.mouse.down()
  await page.mouse.move(sleeping.x + 3, sleeping.y)
  await page.mouse.up()
  await expect(pet(page)).toHaveAttribute('data-state', 'chatting')
  await expect(page.locator('body')).toHaveAttribute('data-chat-requested', 'yes')
  await page.clock.runFor(2_000)
  const chat = page.getByRole('region', { name: '키로 채팅' })
  await expect(chat).toBeVisible()
  await expect(pet(page)).toHaveAttribute('data-state', 'chatting')
  await chat.getByRole('button', { name: '닫기', exact: true }).click()
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
})

test('컴패니언이 고정 내비게이션 위에 보이고 기존 링크와 입력을 사용할 수 있다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route(`**/api/v1${runPath}`, route => route.fulfill({ json: {
    status: 'ACTIVE', virtual_now: '2026-01-01T00:00:00Z', scenario: { key: 'synthetic-ops-boot', version: '1.0' },
  } }))
  const resources = {
    schedule: { slots: [] }, 'incoming-handover': { tickets: [], summary: '' },
    tickets: { items: [], next_cursor: null }, events: { items: [], next_after_sequence: null },
  }
  await page.route(`**/api/v1${runPath}/**`, route => {
    const resource = new URL(route.request().url()).pathname.split('/').at(-1) as keyof typeof resources
    return resources[resource] ? route.fulfill({ json: resources[resource] }) : route.fulfill({ status: 404 })
  })
  await page.goto('/simulation')
  await expect(pet(page)).toBeVisible()
  await page.evaluate(() => {
    window.addEventListener('dco:companion-chat-requested', () => { document.body.dataset.chatRequested = 'yes' })
  })
  const start = await center(pet(page))
  const existingLink = page.getByRole('link', { name: '진행 중인 실습 계속하기' })
  await expect(existingLink).toBeVisible()
  const target = await center(existingLink)
  await page.mouse.move(start.x, start.y)
  await page.mouse.down()
  await page.mouse.move(start.x - 40, start.y - 30)
  await expect(pet(page)).toHaveAttribute('data-state', 'surprised')
  await page.mouse.move(target.x, target.y)
  await page.mouse.up()
  expect(await page.locator('body').getAttribute('data-chat-requested')).toBeNull()
  const dropped = await position(pet(page))
  expect(dropped.x).toBeCloseTo(target.x - 48, 0)
  const headerHeight = (await page.locator('header').boundingBox())!.height
  expect(dropped.y).toBeCloseTo(Math.max(headerHeight + 12, target.y - 48), 0)
  expect(await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('a')?.getAttribute('href'), target)).toBe(runPath)
  await page.mouse.click(target.x, target.y)
  const input = page.getByRole('searchbox', { name: '티켓 검색' })
  await expect(input).toBeVisible()
  await expect(pet(page)).toBeVisible()
  expect(await position(pet(page))).toEqual(dropped)

  const nav = (await page.getByRole('navigation', { name: '실습 자료' }).boundingBox())!
  const overNav = { x: nav.x + nav.width - 65, y: nav.y + nav.height / 2 }
  const dragStart = await center(pet(page))
  await page.mouse.move(dragStart.x, dragStart.y)
  await page.mouse.down()
  await page.mouse.move(overNav.x, overNav.y)
  await page.mouse.up()
  const order = await page.evaluate(({ x, y }) => document.elementsFromPoint(x, y).map(element => element.className), overNav)
  expect(order[0]).toBe('companion-pet')
  expect(order.some(className => String(className).includes('training-nav-sections'))).toBe(true)

  const field = await center(input)
  await page.mouse.down()
  await page.mouse.move(field.x, field.y)
  await page.mouse.up()
  await page.mouse.click(field.x, field.y)
  await expect(input).toBeFocused()
  await page.keyboard.type('합성 장애 제목')
  await expect(input).toHaveValue('합성 장애 제목')

  await page.setViewportSize({ width: 800, height: 520 })
  const resized = (await pet(page).boundingBox())!
  expect(resized.x + resized.width).toBeLessThanOrEqual(800)
  expect(resized.y + resized.height).toBeLessThanOrEqual(520)
})

async function startDragTest(page: Page, origin: { x: number; y: number }) {
  await page.addInitScript(position => {
    sessionStorage.setItem('dco.companion', JSON.stringify({ position, anchor: position }))
  }, origin)
  await page.clock.install()
  await page.goto('/simulation')
  await expect(pet(page)).toBeVisible()
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now() + 300)))
  const cursor = await center(pet(page))
  await page.mouse.move(cursor.x, cursor.y)
  await page.mouse.down()
  return cursor
}

test('붙잡으면 매달리고 빠르게 놓은 방향으로 날아간 뒤 감속한다', async ({ page }) => {
  const cursor = await startDragTest(page, { x: 600, y: 390 })
  const original = await position(pet(page))
  await expect(pet(page)).toHaveAttribute('data-interaction', 'holding')
  await expect(animation(page)).toHaveCSS('animation-name', 'companion-dangle')
  await page.clock.runFor(420)
  expect(await position(pet(page))).toEqual(original)

  await page.mouse.move(cursor.x + 18, cursor.y + 5)
  await page.clock.runFor(16)
  await page.mouse.move(cursor.x + 43, cursor.y + 11)
  await page.clock.runFor(16)
  await page.mouse.move(cursor.x + 68, cursor.y + 17)
  await expect(pet(page)).toHaveAttribute('data-interaction', 'dragging')
  const dropped = await position(pet(page))
  await page.mouse.up()
  await expect(pet(page)).toHaveAttribute('data-interaction', 'flying')
  await expect(animation(page)).toHaveCSS('animation-name', 'companion-glide')
  await page.clock.runFor(160)
  const flying = await position(pet(page))
  expect(flying.x).toBeGreaterThan(dropped.x + 55)
  expect(flying.y).toBeGreaterThan(dropped.y)
  await page.clock.runFor(160)
  const slowing = await position(pet(page))
  expect(slowing.x - flying.x).toBeLessThan(flying.x - dropped.x)
  await page.clock.runFor(500)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')

  const settled = await center(pet(page))
  await page.mouse.move(settled.x, settled.y)
  await page.mouse.down()
  await page.mouse.move(settled.x - 22, settled.y - 18)
  const parked = await position(pet(page))
  await page.clock.runFor(120)
  await page.mouse.up()
  await expect(pet(page)).not.toHaveAttribute('data-interaction', 'flying')
  await page.clock.runFor(500)
  expect(await position(pet(page))).toEqual(parked)
})

for (const { side, origin, delta } of [
  { side: 'right', origin: { x: 1090, y: 320 }, delta: { x: 22, y: 0 } },
  { side: 'top', origin: { x: 670, y: 150 }, delta: { x: 0, y: -22 } },
]) {
  test(`${side} 경계에서 눌림 효과가 실행되고 범위 안에서 멈춘다`, async ({ page }) => {
    const cursor = await startDragTest(page, origin)
    await page.clock.runFor(16)
    await page.mouse.move(cursor.x + delta.x, cursor.y + delta.y)
    await page.clock.runFor(16)
    await page.mouse.move(cursor.x + delta.x * 2, cursor.y + delta.y * 2)
    await page.mouse.up()
    await expect(pet(page)).toHaveAttribute('data-interaction', 'flying')
    await page.clock.runFor(120)
    await expect(pet(page)).toHaveAttribute('data-impact', side)
    await expect(animation(page)).toHaveCSS('animation-name', 'companion-wall-impact')
    const squash = await animation(page).evaluate(element => {
      const effect = element.getAnimations()[0]
      effect.pause()
      effect.currentTime = effect.effect!.getComputedTiming().activeDuration * 0.23
      const matrix = new DOMMatrix(getComputedStyle(element).transform)
      return { x: matrix.a, y: matrix.d }
    })
    const contact = await position(pet(page))
    if (side === 'right') {
      expect(squash.x).toBeLessThan(0.6)
      expect(squash.y).toBeGreaterThan(1.05)
      const width = await page.evaluate(() => document.documentElement.clientWidth)
      expect(contact.x).toBe(width - 108)
      expect(contact.y).toBe(origin.y)
    } else {
      expect(squash.x).toBeGreaterThan(1.05)
      expect(squash.y).toBeLessThan(0.6)
      const header = (await page.locator('.training-topbar').boundingBox())!
      expect(contact.y).toBe(header.height + 12)
      expect(contact.x).toBe(origin.x)
    }
    await page.clock.runFor(500)
    await expect(pet(page)).toHaveAttribute('data-state', 'idle')
    await expect(pet(page)).not.toHaveAttribute('data-impact', side)
    expect(await position(pet(page))).toEqual(contact)
  })
}

test('교육 화면에서 백그라운드 이동을 멈추고 진행 중인 이동을 드래그로 덮어쓴다', async ({ page }) => {
  await page.clock.install()
  await page.goto('/about')
  await expect(page.getByRole('heading', { name: '장애 대응 실습 소개' })).toBeVisible()
  await expect(pet(page)).toBeVisible()
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now() + 300)))
  const initial = await position(pet(page))

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(20_000)
  expect(await position(pet(page))).toEqual(initial)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')

  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(300)
  await expect(pet(page)).toHaveAttribute('data-state', 'walking')
  const cursor = await center(pet(page))
  await page.mouse.move(cursor.x, cursor.y)
  await page.mouse.down()
  await page.mouse.move(2, 2)
  await expect(pet(page)).toHaveAttribute('data-state', 'surprised')
  await page.mouse.up()
  const dropped = await position(pet(page))
  const sidebar = (await page.getByRole('complementary').boundingBox())!
  expect(dropped.x).toBeGreaterThanOrEqual(sidebar.x + sidebar.width)
  await page.clock.runFor(2_000)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  expect(await position(pet(page))).toEqual(dropped)

  const nextPage = await center(page.getByRole('link', { name: '내 실습', exact: true }))
  await page.clock.resume()
  await page.mouse.click(nextPage.x, nextPage.y)
  await expect(page).toHaveURL(/\/simulation$/)
  await expect(pet(page)).toBeVisible()
  expect(await position(pet(page))).toEqual(dropped)
})

test('연동 이벤트, 수동 정지, 숨김 설정과 정리된 타이머가 페이지 변경 후에도 동작한다', async ({ page }) => {
  await page.clock.install()
  await page.goto('/simulation')
  await expect(pet(page)).toBeVisible()
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', { detail: { type: 'hint', text: '합성 SOP 메모' } })))
  await expect(pet(page)).toHaveAttribute('data-state', 'hint')
  await expect(page.getByText('합성 SOP 메모')).toHaveCount(0)
  await pet(page).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('합성 SOP 메모')).toBeVisible()
  await page.clock.runFor(11_000)
  await expect(page.getByText('합성 SOP 메모')).toBeVisible()
  await page.getByRole('button', { name: '힌트 닫기' }).click()
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', { detail: { type: 'state', state: 'thinking' } })))
  await page.clock.runFor(6_000)
  await expect(pet(page)).toHaveAttribute('data-state', 'thinking')
  await expect(page.getByText('합성 SOP 메모')).toHaveCount(0)

  await page.getByText('컴패니언', { exact: true }).click()
  await page.getByLabel('애니메이션 정지').check()
  await expect(animation(page)).toHaveCSS('animation-name', 'none')
  await page.getByLabel('캐릭터 표시').uncheck()
  await expect(pet(page)).toBeHidden()
  await page.getByRole('link', { name: '서비스 소개', exact: true }).click()
  await expect(page.getByRole('heading', { name: '장애 대응 실습 소개' })).toBeVisible()
  await expect(pet(page)).toBeHidden()
  await page.getByText('컴패니언', { exact: true }).click()
  await expect(page.getByLabel('애니메이션 정지')).toBeChecked()
  await page.getByLabel('캐릭터 표시').check()
  await page.getByText('컴패니언', { exact: true }).click()
  await expect(pet(page)).toBeVisible()
  const before = await position(pet(page))
  await page.clock.runFor(30_000)
  expect(await position(pet(page))).toEqual(before)

  await page.evaluate(() => {
    document.body.dataset.chatCount = '0'
    window.addEventListener('dco:companion-chat-requested', () => {
      document.body.dataset.chatCount = String(Number(document.body.dataset.chatCount) + 1)
    })
  })
  await pet(page).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('body')).toHaveAttribute('data-chat-count', '1')

  await page.goBack()
  await expect(page).toHaveURL(/\/simulation$/)
  await expect(pet(page)).toBeVisible()
  await expect(pet(page)).toHaveAttribute('data-paused', 'true')
})

test('모션 축소와 누락된 이미지에 대응한다', async ({ page }) => {
  await page.clock.install()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route(/\/(?:kiro_asset\/cutouts|assets)\/thinking(?:-[^/.]+)?\.png$/, route => route.abort())
  await page.goto('/simulation')
  await expect(pet(page)).toBeVisible()
  await expect(animation(page)).toHaveCSS('animation-name', 'none')
  const initial = await position(pet(page))
  await page.clock.runFor(19_000)
  expect(await position(pet(page))).toEqual(initial)

  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', { detail: { type: 'state', state: 'thinking' } })))
  await expect(pet(page)).toHaveAttribute('data-state', 'thinking')
  await expect(pet(page).locator('img:visible')).toHaveAttribute('data-image-state', 'idle')
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('dco:companion-command', { detail: { type: 'move', position: { x: -300, y: 9000 } } })))
  const sidebar = (await page.getByRole('complementary').boundingBox())!
  expect((await position(pet(page))).x).toBe(sidebar.width + 12)
  await expect(pet(page)).toHaveAttribute('data-state', 'idle')
  await expect(page.getByRole('heading', { name: '내 실습', exact: true })).toBeVisible()
})
