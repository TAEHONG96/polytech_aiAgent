import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

const password = 'synthetic-phase4-browser-password'

async function registerAndLogin(page: Page, baseURL: string) {
  const username = 'phase4-browser-' + randomUUID()
  const registration = await page.request.post('/api/v1/auth/register', {
    headers: { Origin: baseURL }, data: { username, password },
  })
  expect(registration.status(), 'Use loopback QA with APP_TRAINING_REGISTRATION=1').toBe(201)
  await page.goto('/login', { waitUntil: 'commit' })
  await page.locator('input[name=username]').fill(username)
  await page.locator('input[name=password]').fill(password)
  await page.getByRole('button', { name: '로그인', exact: true }).click()
  await page.waitForURL('**/simulation', { waitUntil: 'commit' })
}

test('Fake 평가와 완료 이력, 재실습 및 사용자 격리', async ({ page, browser, baseURL }) => {
  await registerAndLogin(page, baseURL!)
  await page.getByRole('button', { name: 'dco-basic v1.1 시작', exact: true }).click()
  await page.waitForURL(/\/simulation-runs\/[\da-f-]+$/, { waitUntil: 'commit' })
  const firstRun = new URL(page.url()).pathname
  await page.locator('#schedule').getByRole('link', { name: '스토리지 응답 지연', exact: true }).click()
  await page.getByRole('heading', { name: '스토리지 응답 지연', exact: true }).waitFor()
  await page.getByRole('button', { name: 'BMC 진단', exact: true }).click()
  await page.getByText('가상 스토리지 진단을 실행했습니다. · STORAGE_LATENCY').waitFor()
  await page.getByRole('button', { name: '스토리지 점검', exact: true }).click()
  await page.getByText('가상 스토리지의 지연을 확인했습니다. · STORAGE_LATENCY').waitFor()
  await page.getByRole('button', { name: '재진단', exact: true }).click()
  await page.getByText('가상 재진단에서 지연이 계속되었습니다. · STORAGE_LATENCY').waitFor()
  await page.getByRole('link', { name: '근무·티켓 목록', exact: true }).click()
  await page.getByRole('link', { name: '발신 인계' }).click()
  await page.getByRole('button', { name: '인계 자료 다시 수집' }).click()
  await page.getByLabel('스토리지 응답 지연 · 다음 조치', { exact: true }).waitFor()
  await page.getByLabel('인계 요약', { exact: true }).fill('가상 스토리지 지연을 다시 확인했습니다.')
  await page.getByLabel('스토리지 응답 지연 · 다음 조치', { exact: true }).fill('대기열과 지연 추이를 다시 확인합니다.')
  const saved = page.waitForResponse(response => response.url().endsWith('/outgoing-handover') && response.request().method() === 'PUT'
    && !!response.request().postDataJSON()?.content)
  await page.getByRole('button', { name: '인계 초안 저장' }).click()
  expect((await saved).status()).toBe(200)
  await page.getByRole('checkbox', { name: '저장된 보고서를 최종 확정하고 실습을 종료합니다.' }).check()
  const finalized = page.waitForResponse(response => response.url().endsWith('/outgoing-handover/finalize'))
  await page.getByRole('button', { name: '인계 최종 확정' }).click()
  expect((await finalized).status()).toBe(200)
  await page.getByRole('heading', { name: '실습 평가', exact: true }).waitFor()
  await expect(page.locator('#outgoing').getByRole('heading', { name: '확정 경고', exact: true })).toBeVisible()
  await expect(page.locator('#evaluation').getByRole('status')).toContainText('AI 설명 준비됨', { timeout: 20_000 })
  await page.locator('#evaluation').getByRole('button', { name: '스토리지 응답 지연', exact: true }).click()
  await expect(page.locator('#evaluation').getByText('조치 후 재검증 · 충족')).toBeVisible()
  await expect(page.locator('#evaluation').getByText('미해결 업무 인계 · 충족')).toBeVisible()
  await expect(page.locator('#evaluation').getByRole('heading', { name: 'AI 피드백' })).toBeVisible()
  await expect(page.locator('#evaluation').getByText(/수행한 가상 조치 · RERUN_DIAGNOSTICS/)).toBeVisible()
  await expect(page.locator('#outgoing').getByRole('button', { name: '인계 최종 확정' })).toHaveCount(0)

  const other = await browser.newPage({ baseURL })
  try {
    await registerAndLogin(other, baseURL!)
    expect((await other.request.get('/api/v1' + firstRun + '/evaluation')).status()).toBe(404)
    await other.goto(firstRun + '#evaluation', { waitUntil: 'commit' })
    await other.getByRole('alert').waitFor()
    await expect(other.getByRole('heading', { name: '실습 평가' })).toHaveCount(0)
  } finally { await other.close() }

  await page.getByRole('link', { name: '내 실습', exact: true }).click()
  await page.getByRole('button', { name: 'dco-basic v1.1 시작', exact: true }).click()
  await page.waitForURL(url => /\/simulation-runs\/[\da-f-]+$/.test(url.pathname) && url.pathname !== firstRun,
    { waitUntil: 'commit' })
  await page.goto(firstRun + '#evaluation', { waitUntil: 'commit' })
  await expect(page.locator('#evaluation').getByRole('status')).toContainText('AI 설명 준비됨')
})
