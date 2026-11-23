import type { CompanionPageContext } from './companion-context'

export const hintWaitMs = 10_000

export type CompanionEvent =
  | { type: 'PAGE_ENTERED'; pageId: CompanionPageContext['pageId'] }
  | { type: 'TICKET_LIST_READY' | 'SOP_SEARCH_EMPTY' | 'WORK_EDIT_STARTED' | 'INCOMING_OPENED' | 'HANDOVER_OPENED' | 'HANDOVER_EDIT_STARTED' }
  | { type: 'TICKET_SELECTED' | 'ANALYSIS_COMPLETED'; id: string }
  | { type: 'TICKET_STATUS_CHANGED'; status: string }
  | { type: 'ANALYSIS_FAILED'; id?: string }

export type CompanionHint = {
  id: string
  key: string
  priority: number
  message: string
  scope?: string
  targetId?: string
  cooldownMs?: number
}

export type ActiveCompanionHint = { hint: CompanionHint; mode: 'ready' | 'open' }

export function hintScope(context: CompanionPageContext) {
  return context.pathname + '#' + (context.section ?? '')
}

export function contextEvents(current: CompanionPageContext, previous?: CompanionPageContext) {
  const events: CompanionEvent[] = []
  const entered = !previous || hintScope(current) !== hintScope(previous)
  const before = previous?.resources ?? {}
  const { ticket, tickets, search, analysis, incoming, work, handover, source } = current.resources
  if (entered) events.push({ type: 'PAGE_ENTERED', pageId: current.pageId })

  if (tickets?.phase === 'ready' && (!current.section || current.section === 'tickets')
    && (entered || before.tickets?.phase !== 'ready')) events.push({ type: 'TICKET_LIST_READY' })
  if (ticket?.phase === 'ready' && ticket.id) {
    if ((entered || before.ticket?.id !== ticket.id || before.ticket?.phase !== 'ready')
      && (!current.section || current.section === 'ticket-summary')) events.push({ type: 'TICKET_SELECTED', id: ticket.id })
    if (before.ticket?.id === ticket.id && before.ticket.status && ticket.status !== before.ticket.status && ticket.status) {
      events.push({ type: 'TICKET_STATUS_CHANGED', status: ticket.status })
    }
  }
  if (search?.phase === 'empty' && search.kind === 'sop' && (before.search?.request !== search.request || before.search.phase !== 'empty')) {
    events.push({ type: 'SOP_SEARCH_EMPTY' })
  }
  if (analysis && (analysis.id !== before.analysis?.id || analysis.phase !== before.analysis?.phase)) {
    if (analysis.phase === 'ready' && analysis.id) events.push({ type: 'ANALYSIS_COMPLETED', id: analysis.id })
    if (analysis.phase === 'error') events.push({ type: 'ANALYSIS_FAILED', id: analysis.id })
  }
  if (work?.editing && !before.work?.editing) events.push({ type: 'WORK_EDIT_STARTED' })
  if (current.section === 'schedule' && incoming?.phase === 'ready' && (entered || before.incoming?.phase !== 'ready')) {
    events.push({ type: 'INCOMING_OPENED' })
  }
  if ((current.section === 'outgoing' && handover?.phase === 'ready' && (entered || before.handover?.phase !== 'ready'))
    || (current.pageId === 'handover' && source?.phase === 'ready' && before.source?.phase !== 'ready')) {
    events.push({ type: 'HANDOVER_OPENED' })
  }
  if (handover?.dirty && !before.handover?.dirty) events.push({ type: 'HANDOVER_EDIT_STARTED' })
  return events
}

type Rule = Pick<CompanionHint, 'priority' | 'message' | 'targetId' | 'cooldownMs'>

const rules: Partial<Record<CompanionEvent['type'], Rule>> = {
  TICKET_LIST_READY: { priority: 1, targetId: 'tickets', message: '티켓의 우선순위와 처리 상태를 살펴보고 먼저 조사할 티켓을 골라 보세요.' },
  TICKET_SELECTED: { priority: 1, targetId: 'ticket-summary', message: '장애 증상과 티켓에 기록된 영향 범위를 먼저 확인해 보세요.' },
  TICKET_STATUS_CHANGED: { priority: 2, targetId: 'ticket-summary', message: '바뀐 상태에 맞춰 확인한 근거와 아직 필요한 후속 작업을 점검해 보세요.' },
  SOP_SEARCH_EMPTY: { priority: 3, targetId: 'search-sop', cooldownMs: 60_000, message: '승인 SOP를 찾지 못했어요. 장애 코드나 장비 이름처럼 짧고 구체적인 검색어로 다시 찾아보세요.' },
  ANALYSIS_COMPLETED: { priority: 2, targetId: 'analysis', message: '분석에 제시된 근거와 참고 문서를 실제 장애 증상과 비교해 보세요.' },
  INCOMING_OPENED: { priority: 1, targetId: 'schedule', message: '이전 근무조에서 해결하지 못한 티켓과 남겨 둔 다음 조치를 확인해 보세요.' },
  HANDOVER_OPENED: { priority: 1, targetId: 'outgoing', message: '인계에 포함된 티켓과 다음 근무조가 이어서 확인할 항목을 살펴보세요.' },
  WORK_EDIT_STARTED: { priority: 2, targetId: 'work', message: '이미 수행한 작업과 다음 근무자가 이어서 할 작업을 구분해서 기록해 보세요.' },
  HANDOVER_EDIT_STARTED: { priority: 2, targetId: 'outgoing', message: '확인한 사실과 남은 위험, 다음 근무조의 확인 항목을 구분해 작성해 보세요.' },
}

export function isContextHintId(id: string) {
  return Object.hasOwn(rules, id)
}

export function selectHints(context: CompanionPageContext, events: CompanionEvent[]) {
  return events.flatMap((event): CompanionHint[] => {
    const rule = rules[event.type]
    if (!rule || context.userRole !== 'trainee') return []
    const step = event.type === 'TICKET_STATUS_CHANGED' ? event.status : 'id' in event ? event.id : ''
    return [{ ...rule, id: event.type, key: [event.type, context.pageId, context.pathname, step].join('|'), scope: hintScope(context) }]
  }).sort((a, b) => b.priority - a.priority)
}

const historyKey = 'dco.companion.hints'

export function createHintHistory() {
  const seen = new Map<string, number>()
  try {
    const entries: unknown = JSON.parse(sessionStorage.getItem(historyKey) ?? '[]')
    if (Array.isArray(entries)) for (const entry of entries) {
      if (Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1] === 'number') seen.set(entry[0], entry[1])
    }
  } catch { /* The current page still deduplicates when storage is unavailable. */ }
  return {
    available(hint: CompanionHint) {
      const expiration = seen.get(hint.key)
      return expiration === undefined || (expiration !== 0 && expiration <= Date.now())
    },
    remember(hint: CompanionHint) {
      seen.delete(hint.key)
      seen.set(hint.key, hint.cooldownMs ? Date.now() + hint.cooldownMs : 0)
      if (seen.size > 200) seen.delete(seen.keys().next().value!)
      try { sessionStorage.setItem(historyKey, JSON.stringify([...seen])) } catch { /* Keep the in-memory history. */ }
    },
  }
}
