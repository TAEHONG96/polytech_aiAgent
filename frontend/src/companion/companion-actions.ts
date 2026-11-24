export type ActionType = 'navigate_to_page' | 'open_sop' | 'highlight_element' | 'focus_input'
type RouteId = 'practice_home' | 'workspace' | 'ticket_list' | 'outgoing_handover' | 'sop_library'
type AnchorId = 'search-sop' | 'ticket-search' | 'sop-query' | 'outgoing-handover'
export type ActionDecision = 'approve' | 'cancel' | 'select'
export type ActionFailure = 'target_unavailable' | 'context_changed' | 'navigation_failed'
export type Proposal = {
  id: string; type: ActionType; view_id: string; label: string; expires_at: string;
  status: 'pending' | 'approved' | 'cancelled' | 'stale' | 'expired' | 'succeeded' | 'failed';
  anchor_id?: AnchorId; route_id?: RouteId;
  options?: { sop_id: string; title: string }[]; error_code?: ActionFailure;
  requires_selection?: boolean; chosen_sop_id?: string;
}

const routes: Record<RouteId, { suffix: string; target: string }> = {
  practice_home: { suffix: '/simulation', target: '[data-companion-destination="practice-home"]' },
  workspace: { suffix: '', target: '#tickets' },
  ticket_list: { suffix: '#tickets', target: '#tickets' },
  outgoing_handover: { suffix: '#outgoing', target: '#outgoing' },
  sop_library: { suffix: '/sops', target: '[data-companion-destination="sop-library"]' },
}
const anchors: Record<AnchorId, string> = {
  'search-sop': '[data-companion-anchor="search-sop"]',
  'ticket-search': '[data-companion-anchor="ticket-search"]',
  'sop-query': '[data-companion-anchor="sop-query"]',
  'outgoing-handover': '[data-companion-anchor="outgoing-handover"]',
}
export const actionLabels: Record<ActionType, string> = {
  navigate_to_page: '이동', open_sop: '문서 열기', highlight_element: '강조', focus_input: '검색창으로 이동',
}
export const actionSuccess: Record<ActionType, string> = {
  navigate_to_page: '요청하신 화면으로 이동했어요.', open_sop: '승인 SOP 문서를 열었어요.',
  highlight_element: '화면에서 해당 위치를 강조했어요.', focus_input: '검색창에 포커스를 옮겼어요.',
}
export const actionErrors: Record<ActionFailure, string> = {
  target_unavailable: '현재 화면에서 대상 요소를 찾을 수 없어요.',
  context_changed: '화면이 바뀌었어요. 현재 화면에서 다시 요청해 주세요.',
  navigation_failed: '대상 화면이 정상적으로 열리지 않았어요. 접근 권한이나 화면 상태를 확인해 주세요.',
}
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i
const runLocation = /^\/simulation-runs\/([a-f0-9-]{36})(?:\/|$)/i
let highlight: { target: HTMLElement; timer: number } | undefined

function displayable(target: HTMLElement) {
  return target.getClientRects().length > 0 && !target.closest('[hidden], [inert], [aria-hidden="true"]')
    && getComputedStyle(target).visibility === 'visible' && !target.matches(':disabled, [aria-disabled="true"]')
}

function clearHighlight() {
  if (!highlight) return
  window.clearTimeout(highlight.timer)
  highlight.target.classList.remove('companion-action-highlight')
  highlight = undefined
}

export function actionLocation() {
  return window.location.pathname + window.location.search + window.location.hash
}

type ConfirmDestination = (path: string, deadline: number) => Promise<ActionFailure | undefined>

async function navigate(path: string, selector: string, sourceId?: string, confirm?: ConfirmDestination): Promise<ActionFailure | undefined> {
  if (actionLocation() !== path) {
    const oldURL = window.location.href
    window.history.pushState(null, '', path)
    window.dispatchEvent(new PopStateEvent('popstate'))
    window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }))
  }
  const deadline = performance.now() + 6000
  while (performance.now() < deadline) {
    if (actionLocation() !== path) return 'context_changed'
    const target = document.querySelector<HTMLElement>(selector)
    if (target && displayable(target) && (!sourceId || target.dataset.companionSourceId === sourceId)) {
      target.scrollIntoView({ block: 'start' })
      return confirm?.(path, deadline)
    }
    await new Promise(resolve => window.setTimeout(resolve, 50))
  }
  return 'navigation_failed'
}

export async function executeCompanionAction(command: unknown, location: string, proposal: Proposal,
  selectedSop?: string, confirm?: ConfirmDestination): Promise<ActionFailure | undefined> {
  if (location !== actionLocation()) return 'context_changed'
  if (typeof command !== 'object' || !command) return 'target_unavailable'
  const data = command as { type?: unknown; run_id?: unknown; route_id?: unknown; anchor_id?: unknown; sop_id?: unknown }
  if (proposal.type !== data.type) return 'target_unavailable'
  clearHighlight()
  const runId = typeof data.run_id === 'string' && uuid.test(data.run_id) && runLocation.exec(window.location.pathname)?.[1] === data.run_id
    ? data.run_id : undefined
  if (data.type === 'navigate_to_page' && typeof data.route_id === 'string' && data.route_id === proposal.route_id
      && Object.hasOwn(routes, data.route_id)) {
    const route = routes[data.route_id as RouteId]
    if (data.route_id === 'practice_home') return navigate(route.suffix, route.target, undefined, confirm)
    if (runId) return navigate(`/simulation-runs/${runId}${route.suffix}`, route.target, undefined, confirm)
  }
  if (data.type === 'open_sop' && runId && typeof data.sop_id === 'string' && uuid.test(data.sop_id) && data.sop_id === selectedSop) {
    return navigate(`/simulation-runs/${runId}/sops/${data.sop_id}`, '[data-companion-destination="sop"]', data.sop_id, confirm)
  }
  if ((data.type === 'highlight_element' || data.type === 'focus_input') && typeof data.anchor_id === 'string'
      && data.anchor_id === proposal.anchor_id && Object.hasOwn(anchors, data.anchor_id)) {
    const target = document.querySelector<HTMLElement>(anchors[data.anchor_id as AnchorId])
    if (!target || !displayable(target)) return 'target_unavailable'
    if (data.type === 'focus_input') {
      if (!(target instanceof HTMLInputElement) || target.type === 'hidden' || target.readOnly) return 'target_unavailable'
      target.focus()
      if (document.activeElement !== target) return 'target_unavailable'
    } else {
      target.scrollIntoView({ block: 'center' })
      target.classList.add('companion-action-highlight')
      highlight = { target, timer: window.setTimeout(clearHighlight, 2400) }
    }
    return
  }
  return 'target_unavailable'
}
