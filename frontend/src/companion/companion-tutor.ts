import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { ApiError, api, errorMessage } from '../api'
import type { Session } from '../api'
import { requestContext } from './companion-context'
import type { CompanionPageContext, useCompanionContextStore } from './companion-context'
import type { CompanionView, createCompanionController } from './companion-controller'
import { adaptiveHintPriority, hintScope } from './companion-hints'
import type { TutorFeedback, TutorHintResult } from './companion-hints'

const endpoint = '/companion/tutor'
const retentionMs = 10 * 60_000
const idleThreshold = 90
const hintRequestTimeout = 110_000
let fallbackTab: string | undefined

export function tutorTabId() {
  const key = 'dco.companion.tutor-tab'
  try {
    const saved = sessionStorage.getItem(key)
    if (saved && /^[\da-f-]{36}$/i.test(saved)) return saved
    const id = crypto.randomUUID()
    sessionStorage.setItem(key, id)
    return id
  } catch { return fallbackTab ??= crypto.randomUUID() }
}

export type TutorBehavior = 'TICKET_VIEWED' | 'SOP_SEARCH_COMPLETED' | 'SOP_OPENED' | 'ANALYSIS_COMPLETED' | 'ANALYSIS_FAILED'
  | 'WORKFLOW_STEP_COMPLETED' | 'WORKFLOW_CANCELLED' | 'TICKET_PROGRESS'
type LocalEvent = { kind: TutorBehavior; id: string; at: number; run: string; ticket: string; receipt?: string }
type TicketRef = { run: string; ticket: string; at: number }

function createCollector(userId: string) {
  const key = `dco.companion.tutor-events.${userId}`
  let events: LocalEvent[] = []
  let lastTicket: TicketRef | undefined
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? '{}')
    if (Array.isArray(saved.events)) events = saved.events.slice(-80)
    lastTicket = saved.lastTicket
  } catch { /* The current tab can still collect events in memory. */ }
  const prune = () => { events = events.filter(event => event.at >= Date.now() - retentionMs) }
  const save = () => {
    try { sessionStorage.setItem(key, JSON.stringify({ events, lastTicket })) } catch { /* Keep in memory. */ }
  }
  function record(kind: TutorBehavior, id: string, run: string, ticket: string, receipt?: string) {
    prune()
    if (events.some(event => event.kind === kind && event.id === id && event.run === run && event.ticket === ticket)) return
    events = [...events, { kind, id, run, ticket, at: Date.now(), receipt }].slice(-80)
    save()
  }
  function capture(current: CompanionPageContext) {
    const run = current.runId
    const ticket = current.ticketId
    const resources = current.resources
    if (current.pageId === 'ticket' && run && ticket && resources.ticket?.phase === 'ready') {
      if (lastTicket?.run !== run || lastTicket.ticket !== ticket) {
        lastTicket = { run, ticket, at: Date.now() }
        save()
      }
      record('TICKET_VIEWED', ticket, run, ticket)
      const search = resources.search
      if (search?.tutorReceipt && (search.phase === 'empty' || search.phase === 'ready')) {
        record('SOP_SEARCH_COMPLETED', search.tutorReceipt.split('.')[0], run, ticket, search.tutorReceipt)
      }
      const analysis = resources.analysis
      if (analysis?.id && (analysis.phase === 'ready' || analysis.phase === 'error')) {
        record(analysis.phase === 'ready' ? 'ANALYSIS_COMPLETED' : 'ANALYSIS_FAILED', analysis.id, run, ticket)
      }
    } else if (current.pageId === 'sop' && run && lastTicket?.run === run && resources.source?.phase === 'ready'
      && current.entity?.type === 'sop' && Date.now() - lastTicket.at < retentionMs) {
      record('SOP_OPENED', current.entity.id, run, lastTicket.ticket)
    } else if (current.pageId !== 'ticket' && current.pageId !== 'sop' && lastTicket) {
      lastTicket = undefined
      save()
    }
  }
  return {
    capture,
    record,
    recent(run: string, ticket: string) {
      prune()
      return events.filter(event => event.run === run && event.ticket === ticket).slice(-40)
    },
  }
}

export function sendTutorFeedback(session: Session, offerId: string, event: TutorFeedback) {
  void api(`${endpoint}/${offerId}/feedback`, { method: 'POST', keepalive: true,
    body: JSON.stringify({ tab_id: tutorTabId(), event }) }, session.csrf_token).catch(() => {})
}

function canOffer(view: CompanionView, busy: boolean, context?: CompanionPageContext) {
  return !document.hidden && view.tutorEnabled && !view.hidden && !view.paused && !view.chatOpen && !view.activeHint
    && !view.interaction && !view.preview && !busy && context?.pageId === 'ticket' && context.runId && context.ticketId
    && context.pathname === window.location.pathname && context.resources.run?.phase === 'ready'
    && context.resources.ticket?.phase === 'ready' && context.resources.search?.phase !== 'loading'
    && context.resources.analysis?.phase !== 'loading' && !context.resources.work?.editing
}

export function useAdaptiveTutor({ store, session, view, busy, controller }: {
  store: ReturnType<typeof useCompanionContextStore>; session: Session; view: CompanionView; busy: boolean;
  controller: RefObject<ReturnType<typeof createCompanionController> | null>;
}) {
  const [collector] = useState(() => createCollector(session.user.id))
  const latest = useRef({ view, busy, store })
  useLayoutEffect(() => { latest.current = { view, busy, store } }, [view, busy, store])
  const pending = useRef(false)
  const sample = useRef({ scope: '', active: 0, at: Date.now(), key: '', next: 0, lastInput: 0 })

  useEffect(() => {
    if (!store) return
    let live = true
    const onInput = () => { sample.current.lastInput = Date.now() }
    const request = async (current: CompanionPageContext, scope: string, active: number, events: LocalEvent[]) => {
      pending.current = true
      try {
        const offer = await api<{ id: string } | null>(`${endpoint}/offers`, { method: 'POST', body: JSON.stringify({
          tab_id: tutorTabId(), page_context: requestContext(current), active_seconds: Math.floor(active),
          frequency: latest.current.view.tutorFrequency,
          events: events.map(({ kind, id, at, receipt }) => ({ kind, id, at: new Date(at).toISOString(), receipt })),
        }) }, session.csrf_token)
        if (!offer) return
        const next = latest.current.store?.getCurrentPageContext()
        if (!live || !next || hintScope(next) !== scope || !canOffer(latest.current.view, latest.current.busy, next)
            || !controller.current?.offerHint({ id: 'ADAPTIVE_TUTOR', key: offer.id, priority: adaptiveHintPriority,
              message: '', scope, tutor: { offerId: offer.id } })) sendTutorFeedback(session, offer.id, 'INVALIDATED')
      } catch { /* The existing companion functions without an adaptive offer. */ }
      finally { pending.current = false }
    }
    const evaluate = () => {
      const current = store.getCurrentPageContext()
      collector.capture(current)
      const scope = hintScope(current)
      const state = sample.current
      const now = Date.now()
      if (state.scope !== scope) Object.assign(state, { scope, active: 0, at: now, key: '', next: 0, lastInput: 0 })
      const allowed = canOffer(latest.current.view, latest.current.busy, current) && now - state.lastInput >= 900
      if (allowed) state.active = Math.min(600, state.active + Math.min(5, Math.max(0, now - state.at) / 1000))
      state.at = now
      if (!allowed || pending.current || !current.runId || !current.ticketId) return
      const events = collector.recent(current.runId, current.ticketId)
      if (events.filter(event => event.kind === 'SOP_SEARCH_COMPLETED').length < 2
          && !events.some(event => event.kind.startsWith('ANALYSIS_'))) return
      const key = [latest.current.view.tutorFrequency, state.active >= idleThreshold,
        ...events.map(event => `${event.kind}:${event.id}`)].join('|')
      if (key === state.key && now < state.next) return
      state.key = key
      state.next = now + 30_000
      void request(current, scope, state.active, events)
    }
    const unsubscribe = store.subscribe(evaluate)
    const timer = window.setInterval(evaluate, 2500)
    const visibility = () => { sample.current.at = Date.now() }
    document.addEventListener('visibilitychange', visibility)
    document.addEventListener('input', onInput, true)
    evaluate()
    return () => {
      live = false
      unsubscribe()
      window.clearInterval(timer)
      document.removeEventListener('input', onInput, true)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [store, session, collector, controller])

  const active = view.activeHint
  const loading = active?.mode === 'loading' ? active.hint.tutor : undefined
  const command = loading?.request
  const offerId = loading?.offerId
  const scope = active?.hint.scope
  useEffect(() => {
    if (!store || !offerId || !command) return
    const snapshot = store.getCurrentPageContext()
    if (hintScope(snapshot) !== scope) return
    const abort = new AbortController()
    let timer: number | undefined
    const fail = (failure: unknown) => {
      if (abort.signal.aborted || hintScope(store.getCurrentPageContext()) !== scope) return
      const retryable = !(failure instanceof ApiError) || failure.status >= 500 || failure.status === 429
        || failure.code === 'AGENT_BUSY' || failure.code === 'TUTOR_BUSY'
      controller.current?.finishTutorRequest(command.id, { id: command.id, level: 0, status: 'failed', sources: [],
        error: { message: errorMessage(failure) }, can_retry: retryable, model_calls: 0, embedding_calls: 0 }, retryable)
    }
    const deadline = window.setTimeout(() => {
      fail(new ApiError('맞춤 힌트 응답을 기다리는 시간이 지났어요.', 504))
      abort.abort()
      window.clearTimeout(timer)
    }, hintRequestTimeout)
    const handle = (result: TutorHintResult) => {
      if (abort.signal.aborted || hintScope(store.getCurrentPageContext()) !== scope) return
      if (result.status === 'queued' || result.status === 'running') {
        timer = window.setTimeout(() => void api<TutorHintResult>(`${endpoint}/${offerId}/hints/${result.id}?tab_id=${tutorTabId()}`,
          { signal: abort.signal }).then(handle, fail), 800)
      } else controller.current?.finishTutorRequest(command.id, result)
    }
    void api<TutorHintResult>(`${endpoint}/${offerId}/hints`, { method: 'POST', signal: abort.signal,
      body: JSON.stringify({ tab_id: tutorTabId(), page_context: requestContext(snapshot), command: command.command, request_id: command.id }),
    }, session.csrf_token).then(handle, fail)
    return () => { abort.abort(); window.clearTimeout(timer); window.clearTimeout(deadline) }
  }, [store, session, controller, command, offerId, scope])

  return collector.record
}
