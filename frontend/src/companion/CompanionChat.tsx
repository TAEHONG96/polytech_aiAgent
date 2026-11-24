import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { FormEvent } from 'react'
import { api, errorMessage } from '../api'
import type { Session } from '../api'
import { trainingSourceHref } from '../training-source'
import { companionAsset } from './companion-assets'
import { useCompanionContextStore } from './companion-context'
import type { CompanionPageContext } from './companion-context'
import { companionChatRequestedEvent } from './companion-events'
import { CompanionActionCard } from './CompanionActionCard'
import { actionLocation, executeCompanionAction } from './companion-actions'
import type { ActionFailure, Proposal } from './companion-actions'

type Phase = 'chatting' | 'thinking' | 'happy' | 'surprised' | null
type PageInput = { page_id: string; run_id?: string; ticket_id?: string; section?: string; sop_id?: string;
  incident_id?: string; handover_revision?: number; analysis_id?: string; search_state?: string }
type Body = { conversation_id: string; request_id: string; page_context: PageInput; message: string; active_hint_id?: string; action_view_id?: string }
type Turn = { message_id: string; request_id: string; status: 'queued' | 'running' | 'succeeded' | 'failed';
  message: string; answer?: string; context_info: PageInput & { label: string }; active_hint_id?: string;
  sources: { key: string; title: string; source_url: string; section?: string }[];
  execution_mode?: 'gemini' | 'mock' | 'workflow' | 'error'; provider?: string;
  action_proposal?: Proposal | null;
  retrieval?: 'found' | 'empty' | 'unavailable' | 'not_scoped' | 'not_required'; error?: { message: string; retryable: boolean } }
type Saved = { id: string; open: boolean; receipt?: { id: string; at: number } }

const noContext = () => undefined
const noSubscription = () => () => {}
const base = '/companion/chat'

function restore(key: string): Saved {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? 'null')
    if (value && /^[a-f0-9-]{36}$/i.test(value.id)) return value
  } catch { /* A tab without storage can still start a conversation. */ }
  return { id: crypto.randomUUID(), open: false }
}

function save(key: string, value: Saved) {
  try { sessionStorage.setItem(key, JSON.stringify(value)) } catch { /* Keep the current in-memory session. */ }
}

function requestContext(context?: CompanionPageContext): PageInput {
  return {
    page_id: context?.pageId ?? 'home', section: context?.section, run_id: context?.runId, ticket_id: context?.ticketId,
    sop_id: context?.entity?.type === 'sop' ? context.entity.id : undefined,
    incident_id: context?.entity?.type === 'incident' ? context.entity.id : undefined,
    handover_revision: context?.entity?.type === 'handover' ? Number(context.entity.id) : undefined,
    analysis_id: context?.resources.analysis?.phase === 'ready' ? context.resources.analysis.id : undefined,
    search_state: context?.resources.search?.phase,
  }
}

function inline(text: string) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).map((piece, index) => piece.startsWith('**')
    ? <strong key={index}>{piece.slice(2, -2)}</strong> : piece.startsWith('`') ? <code key={index}>{piece.slice(1, -1)}</code> : piece)
}

function Answer({ text }: { text: string }) {
  return text.split(/(```[\s\S]*?```)/).flatMap((segment, block) => {
    if (segment.startsWith('```') && segment.endsWith('```')) {
      return <pre key={block}><code>{segment.slice(3, -3).replace(/^[\w+-]*\r?\n/, '')}</code></pre>
    }
    return segment.trim().split(/\n\s*\n/).filter(Boolean).map((paragraph, index) => {
      const lines = paragraph.split('\n')
      const ordered = lines.every(line => /^\d+\. /.test(line))
      const bullets = lines.every(line => /^[-*] /.test(line))
      if (ordered || bullets) {
        const List = ordered ? 'ol' : 'ul'
        return <List key={`${block}-${index}`}>{lines.map((line, i) => <li key={i}>{inline(line.replace(/^(?:\d+\.|[-*]) /, ''))}</li>)}</List>
      }
      return <p key={`${block}-${index}`}>{inline(paragraph.replace(/^#{1,6} /gm, ''))}</p>
    })
  })
}

const sourcePath = /^\/api\/v1\/simulation-runs\/[\da-f-]{36}\/(?:sops|tickets)\/[\da-f-]{36}(?:\/incident-sources\/[\da-f-]{36})?$/i
const isWaiting = (turn: Turn) => turn.status === 'queued' || turn.status === 'running'

export function CompanionChat({ session, hidden, setPhase, returnFocus }: {
  session: Session; hidden: boolean; setPhase: (phase: Phase) => void; returnFocus: () => void
}) {
  const key = `dco.companion.chat.${session.user.id}`
  const [saved, setSaved] = useState(() => restore(key))
  const [turns, setTurns] = useState<Turn[]>([])
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState<{ body: Body; label: string }>()
  const [retry, setRetry] = useState<Body>()
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [resetting, setResetting] = useState(false)
  const [runningAction, setRunningAction] = useState<string>()
  const [actionReaction, setActionReaction] = useState<'happy' | 'surprised'>()
  const [actionClock, setActionClock] = useState(Date.now)
  const [browserLocation, setBrowserLocation] = useState(actionLocation)
  const [viewEpoch, advanceView] = useState(0)
  const [instruction, setInstruction] = useState<{ id: string; path: string; section?: string }>()
  const textarea = useRef<HTMLTextAreaElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const live = useRef(true)
  const previous = useRef<{ id: string; status: Turn['status'] }>(undefined)
  const contextStore = useCompanionContextStore()
  const context = useSyncExternalStore(contextStore?.subscribe ?? noSubscription, contextStore?.getCurrentPageContext ?? noContext)
  const label = context?.resources.ticket?.number ? `실습 티켓 ${context.resources.ticket.number}` : context?.pageTitle ?? '일반 DCO 질문'
  const relatedHint = instruction?.path === context?.pathname && instruction?.section === context?.section ? instruction?.id : undefined
  const visible = saved.open && !hidden
  const waiting = !!sending || !!saved.receipt || turns.some(isWaiting)
  const actionView = useMemo(() => ({ id: crypto.randomUUID(), location: browserLocation }),
    [visible, saved.id, browserLocation, context?.pathname, context?.section, viewEpoch])
  const activeView = useRef<string>(undefined)
  const usedView = useRef<string>(undefined)
  const actionLock = useRef(false)

  useEffect(() => {
    const sync = () => setBrowserLocation(actionLocation())
    const restore = (event: PageTransitionEvent) => { if (event.persisted) advanceView(value => value + 1) }
    window.addEventListener('hashchange', sync)
    window.addEventListener('popstate', sync)
    window.addEventListener('pageshow', restore)
    return () => {
      window.removeEventListener('hashchange', sync)
      window.removeEventListener('popstate', sync)
      window.removeEventListener('pageshow', restore)
    }
  }, [])

  useLayoutEffect(() => {
    if (!visible) return
    activeView.current = actionView.id
    const invalidate = () => {
      if (activeView.current === actionView.id) activeView.current = undefined
      if (usedView.current !== actionView.id) return
      usedView.current = undefined
      void api(`${base}/${saved.id}/actions/invalidate`, { method: 'POST', keepalive: true,
        body: JSON.stringify({ view_id: actionView.id }) }, session.csrf_token).catch(() => {})
    }
    window.addEventListener('pagehide', invalidate)
    return () => { window.removeEventListener('pagehide', invalidate); invalidate() }
  }, [visible, actionView, saved.id, session.csrf_token])

  useEffect(() => {
    if (!visible) return
    const pending = turns.flatMap(turn => turn.action_proposal?.status === 'pending' && turn.action_proposal.view_id === actionView.id
      ? [Date.parse(turn.action_proposal.expires_at)] : []).filter(expires => expires > actionClock)
    if (!pending.length) return
    const timer = window.setTimeout(() => setActionClock(Date.now()), Math.max(1, Math.min(...pending) - Date.now() + 10))
    return () => window.clearTimeout(timer)
  }, [visible, turns, actionView, actionClock])

  useEffect(() => { live.current = true; return () => { live.current = false } }, [])
  useLayoutEffect(() => {
    const open = (event: WindowEventMap[typeof companionChatRequestedEvent]) => {
      const current = contextStore?.getCurrentPageContext()
      if (event.detail?.hintId && current) setInstruction({ id: event.detail.hintId, path: current.pathname, section: current.section })
      setSaved(value => ({ ...value, open: true }))
      textarea.current?.focus({ preventScroll: true })
    }
    window.addEventListener(companionChatRequestedEvent, open)
    return () => window.removeEventListener(companionChatRequestedEvent, open)
  }, [contextStore])
  useLayoutEffect(() => { save(key, saved) }, [key, saved])
  useLayoutEffect(() => {
    if (visible) { follow.current = true; textarea.current?.focus({ preventScroll: true }) }
  }, [visible])
  useLayoutEffect(() => {
    if (visible && list.current && follow.current) list.current.scrollTop = list.current.scrollHeight
  }, [visible, turns, sending])

  useEffect(() => {
    if (!visible) return
    const abort = new AbortController()
    let timer: number | undefined
    async function read() {
      try {
        const history = await api<{ turns: Turn[] }>(`${base}/${saved.id}`, { signal: abort.signal })
        if (abort.signal.aborted) return
        setTurns(history.turns)
        setLoadError('')
        const reconnect = saved.receipt && !history.turns.some(turn => turn.request_id === saved.receipt?.id)
          && Date.now() - saved.receipt.at < 12_000
        if (saved.receipt && !reconnect) setSaved(value => value.receipt?.id === saved.receipt?.id ? { ...value, receipt: undefined } : value)
        if (reconnect || history.turns.some(isWaiting)) timer = window.setTimeout(read, 1000)
      } catch (failure) { if (!abort.signal.aborted) setLoadError(errorMessage(failure)) }
    }
    void read()
    return () => { abort.abort(); window.clearTimeout(timer) }
  }, [saved.id, saved.receipt, visible, refresh])

  const last = turns.at(-1)
  useLayoutEffect(() => {
    let phase: Phase = null
    if (visible) {
      const justFinished = last && last.message_id === previous.current?.id
        && (previous.current.status === 'queued' || previous.current.status === 'running') && !isWaiting(last)
      phase = waiting || runningAction ? 'thinking'
        : actionReaction ?? (justFinished && !last.action_proposal ? (last.status === 'succeeded' ? 'happy' : 'surprised') : 'chatting')
    }
    if (last) previous.current = { id: last.message_id, status: last.status }
    setPhase(phase)
  }, [visible, waiting, runningAction, actionReaction, last?.message_id, last?.status, setPhase])

  async function send(body: Body, snapshotLabel: string) {
    follow.current = true
    setActionReaction(undefined)
    if (body.action_view_id === activeView.current) usedView.current = body.action_view_id
    setSending({ body, label: snapshotLabel })
    setError('')
    setRetry(undefined)
    setSaved(value => ({ ...value, receipt: { id: body.request_id, at: Date.now() } }))
    try {
      const turn = await api<Turn>(base, { method: 'POST', body: JSON.stringify(body), keepalive: true }, session.csrf_token)
      if (!live.current) return
      setTurns(old => [...old.filter(item => item.message_id !== turn.message_id), turn])
      setRefresh(value => value + 1)
    } catch (failure) {
      if (!live.current) return
      setSaved(value => ({ ...value, receipt: undefined }))
      setError(errorMessage(failure))
      setRetry(body)
    } finally { if (live.current) setSending(undefined) }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!message.trim() || waiting || resetting || actionLock.current) return
    const current = contextStore?.getCurrentPageContext()
    const validHint = instruction?.path === current?.pathname && instruction?.section === current?.section ? instruction?.id : undefined
    const body = { conversation_id: saved.id, request_id: crypto.randomUUID(), page_context: requestContext(current),
      message: message.trim(), active_hint_id: validHint, action_view_id: actionView.id }
    setMessage('')
    setInstruction(undefined)
    void send(body, label)
  }

  async function decideAction(turn: Turn, proposal: Proposal, decision: 'approve' | 'cancel', selected?: string) {
    if (actionLock.current || waiting || resetting || !canDecide(proposal)) return
    actionLock.current = true
    setRunningAction(proposal.id)
    setActionReaction(undefined)
    setError('')
    const endpoint = `${base}/${saved.id}/actions/${turn.message_id}`
    const identity = { proposal_id: proposal.id, view_id: proposal.view_id }
    const update = (status: Proposal['status'], error_code?: ActionFailure) => setTurns(current => current.map(item =>
      item.message_id === turn.message_id ? { ...item, action_proposal: { ...proposal, status, error_code } } : item))
    try {
      const response = await api<{ status: 'approved' | 'cancelled'; command: unknown }>(endpoint, { method: 'POST',
        body: JSON.stringify({ ...identity, decision, sop_id: selected, page_context: requestContext(contextStore?.getCurrentPageContext()) }) }, session.csrf_token)
      update(response.status)
      if (response.status === 'approved') {
        let failure: ActionFailure | undefined = 'context_changed'
        if (activeView.current === proposal.view_id) {
          try { failure = await executeCompanionAction(response.command, actionView.location, proposal, selected) }
          catch { failure = 'target_unavailable' }
        }
        const outcome = failure ? 'failed' : 'succeeded'
        update(outcome, failure)
        setActionReaction(failure ? 'surprised' : 'happy')
        await api(`${endpoint}/feedback`, { method: 'POST', keepalive: true,
          body: JSON.stringify({ ...identity, outcome, error_code: failure }) }, session.csrf_token)
      }
    } catch (failure) {
      setError(errorMessage(failure))
      setActionReaction('surprised')
    } finally {
      actionLock.current = false
      setRunningAction(undefined)
      setRefresh(value => value + 1)
    }
  }

  function canDecide(proposal: Proposal) {
    return proposal.view_id === actionView.id && actionView.location === actionLocation()
      && proposal.status === 'pending' && Date.parse(proposal.expires_at) > Math.max(Date.now(), actionClock)
  }

  async function reset() {
    setResetting(true)
    setError('')
    try {
      await api(`${base}/${saved.id}`, { method: 'DELETE' }, session.csrf_token)
      setSaved({ id: crypto.randomUUID(), open: true })
      setTurns([])
      setMessage('')
      setInstruction(undefined)
      setRetry(undefined)
      setLoadError('')
      textarea.current?.focus({ preventScroll: true })
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setResetting(false) }
  }

  function close() {
    activeView.current = undefined
    setSaved(value => ({ ...value, open: false }))
    returnFocus()
  }

  if (!visible) return null
  return <section className="companion-chat" aria-label="키로 채팅" onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); close() }
  }}>
    <div className="companion-chat-header">
      <img src={companionAsset('chatting')} alt="" />
      <div><h2>키로 채팅</h2><p aria-live="polite">현재 화면 · {label}</p></div>
      <button type="button" className="secondary" onClick={close}>닫기</button>
    </div>
    <div ref={list} className="companion-chat-log" role="log" aria-label="대화 기록" onScroll={event => {
      const area = event.currentTarget
      follow.current = area.scrollHeight - area.scrollTop - area.clientHeight < 48
    }}>
      {!turns.length && !sending && <p className="meta">현재 실습에서 궁금한 점을 물어보세요.</p>}
      {turns.map(turn => <article key={turn.message_id} className="companion-chat-turn">
        <p className="meta">{turn.context_info.label}{turn.active_hint_id && ' · 힌트 질문'}</p>
        <p className="companion-chat-question">{turn.message}</p>
        {turn.answer && <div className="companion-chat-answer"><Answer text={turn.answer} />
          {(turn.execution_mode === 'mock' || turn.provider === 'fake') && <p className="meta">모의 응답</p>}
          {(turn.execution_mode === 'gemini' || turn.provider === 'gemini') && <p className="meta">Gemini 응답</p>}
          {(turn.execution_mode === 'workflow' || turn.provider === 'workflow') && <p className="meta">실습 업무 규칙·기록 기반</p>}
          {turn.retrieval === 'empty' && <p className="meta">관련 승인 SOP 검색 결과가 없습니다.</p>}
          {turn.retrieval === 'unavailable' && <p className="meta">승인 SOP 검색을 완료하지 못했습니다.</p>}
          {!!turn.sources.length && turn.action_proposal?.type !== 'open_sop' && <ul aria-label="근거 출처">{turn.sources.filter(source => sourcePath.test(source.source_url)).map(source => (
            <li key={source.key}><a href={trainingSourceHref(source.source_url)}>{source.title}{source.section && ` · ${source.section}`}</a></li>
          ))}</ul>}
        </div>}
        {turn.action_proposal && <CompanionActionCard key={turn.action_proposal.id} proposal={turn.action_proposal}
          active={canDecide(turn.action_proposal)} busy={!!runningAction || waiting || resetting}
          decide={(decision, selected) => void decideAction(turn, turn.action_proposal!, decision, selected)} />}
        {turn.error && <p className="notice" role="alert">{turn.error.message}{turn.error.retryable && !waiting && <button type="button" className="secondary"
          onClick={() => {
            const { label: snapshot, ...page_context } = turn.context_info
            void send({ conversation_id: saved.id, request_id: crypto.randomUUID(), page_context,
              message: turn.message, active_hint_id: turn.active_hint_id }, snapshot)
          }}>다시 시도</button>}</p>}
      </article>)}
      {sending && !turns.some(turn => turn.request_id === sending.body.request_id) && <article className="companion-chat-turn">
        <p className="meta">{sending.label}</p><p className="companion-chat-question">{sending.body.message}</p>
      </article>}
      {waiting && <p role="status">키로가 확인하는 중입니다.</p>}
    </div>
    <div>
      {loadError && <div role="alert" className="notice">{loadError} <button type="button" className="secondary" onClick={() => setRefresh(value => value + 1)}>기록 다시 불러오기</button></div>}
      {error && <div role="alert" className="notice">{error} {retry && <button type="button" className="secondary" onClick={() => void send(retry, label)}>질문 다시 전송</button>}</div>}
      <form className="companion-chat-form" onSubmit={submit}>
      {relatedHint && <p className="meta">이 질문에 현재 힌트를 연결합니다.</p>}
      <label>키로에게 질문<textarea ref={textarea} rows={2} maxLength={2000} value={message} onChange={event => setMessage(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault(); event.currentTarget.form?.requestSubmit()
          }
        }} /></label>
      <div><button type="button" className="secondary" disabled={!!sending || !!runningAction || resetting || (!turns.length && !retry)} onClick={() => void reset()}>대화 초기화</button>
        <button type="submit" disabled={waiting || !!runningAction || resetting || !message.trim()}>전송</button></div>
      </form>
    </div>
  </section>
}
