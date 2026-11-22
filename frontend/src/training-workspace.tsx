import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ReportView } from './agent'
import type { Run as Analysis } from './agent'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Ticket } from './api'
import { TicketOperations } from './ticket-operations'

const kst = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short',
})
const date = (value: string) => kst.format(new Date(value))
const origin = { INITIAL: '초기', CARRYOVER: '이월', USER_CREATED: '직접 등록' }
type TrainingTicket = Ticket & { training_origin: keyof typeof origin }
type Run = { status: 'ACTIVE' | 'COMPLETED'; virtual_now: string; scenario: { key: string; version: string } }
type Slot = { id: string; crew_code: string; type: string; actor_type: string; starts_at: string; ends_at: string }
type Incoming = { summary: string; tickets: { ticket_id: string }[] }
type Incident = { id: string; summary: string; occurred_at: string; resolution: string; source_url: string }
type Sop = { id: string; title: string; original_url: string }
type Hit = { source_id: string; title: string; content: string; source_url: string; section: string | null; page_number: number | null }
type CatalogAction = { action_code: string; label: string; parameters: Record<string, string>; available: boolean; blocked_reason: string | null }
type Activity = {
  action_id: string; action_code: string; kind: 'ACTION' | 'NOTE'; occurred_at: string
  event_sequence: number; ticket_version: number; note: string | null; result: { message: string; health: string; error_code: string | null } | null
}

function useResource<T>(path: string) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api<T>(path, { signal: controller.signal }).then(setData)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path])
  return { data, error }
}

export function TrainingWorkspace({ runId, ticketId, session }: { runId: string; ticketId?: string; session: Session }) {
  const base = '/simulation-runs/' + runId
  const { data: run, error } = useResource<Run>(base)
  return <>
    <nav className="row" aria-label="실습 탐색"><a href="/simulation">내 실습</a>{ticketId && <a href={base}>근무·티켓 목록</a>}</nav>
    {error && <p role="alert">{error}</p>}
    {!run && !error && <p role="status">실습을 불러오는 중입니다.</p>}
    {run && (ticketId ? <TicketWorkspace base={base} ticketId={ticketId} run={run} session={session} />
      : <Overview base={base} run={run} session={session} />)}
  </>
}

function Overview({ base, run, session }: { base: string; run: Run; session: Session }) {
  const schedule = useResource<{ slots: Slot[] }>(base + '/schedule')
  const incoming = useResource<Incoming>(base + '/incoming-handover')
  const tickets = useResource<{ items: TrainingTicket[] }>(base + '/tickets')
  return <>
    <h1>{run.scenario.key} v{run.scenario.version}</h1>
    <p>{run.status === 'ACTIVE' ? '진행 중' : '완료'} · 고정 실습 시각 {date(run.virtual_now)} KST</p>
    <nav className="row" aria-label="실습 자료"><a href="#schedule">근무·인계</a><a href="#tickets">티켓</a>
      <a href="#knowledge">SOP·유사 장애</a></nav>
    {schedule.error || incoming.error || tickets.error ? <p role="alert">{schedule.error || incoming.error || tickets.error}</p> : null}
    <section id="schedule" className="surface">
      <h2>4조 2교대 근무</h2>
      <ol>{schedule.data?.slots.map((slot, index) => (
        <li key={slot.id}><strong>{['이전', '현재', '다음'][index]} · {slot.crew_code}조 · {slot.actor_type === 'BOT' ? 'Bot' : '교육생'}</strong>
          {' · '}{slot.type === 'DAY' ? '주간' : '야간'} · {date(slot.starts_at)}–{date(slot.ends_at)} KST</li>
      ))}</ol>
      <h3>이전 Bot 인계 보고서</h3>
      <p>{incoming.data?.summary}</p>
      <ul>{incoming.data?.tickets.map(link => (
        <li key={link.ticket_id}><a href={base + '/tickets/' + link.ticket_id}>
          {tickets.data?.items.find(ticket => ticket.id === link.ticket_id)?.title ?? '이월 티켓 열기'}</a></li>
      ))}</ul>
    </section>
    <section id="tickets" className="surface">
      <h2>실습 티켓</h2>
      <ul>{tickets.data?.items.map(ticket => (
        <li key={ticket.id}><a href={base + '/tickets/' + ticket.id}>{ticket.title}</a>
          {' · '}{origin[ticket.training_origin]} · {statuses[ticket.status]}</li>
      ))}</ul>
    </section>
    <Knowledge base={base} session={session} />
  </>
}

function TicketWorkspace({ base, ticketId, run, session }: { base: string; ticketId: string; run: Run; session: Session }) {
  const path = base + '/tickets/' + ticketId
  const ticket = useResource<TrainingTicket>(path)
  const [latestVersion, setVersion] = useState(0)
  const incidents = useResource<{ incidents: Incident[] }>(path + '/host-incidents')
  if (ticket.error) return <p role="alert">{ticket.error}</p>
  if (!ticket.data) return <p role="status">티켓을 불러오는 중입니다.</p>
  const current = { ...ticket.data, version: Math.max(ticket.data.version, latestVersion) }
  const readOnly = run.status === 'COMPLETED'
  return <>
    <h1>{current.title}</h1>
    <p>{current.number} · {origin[current.training_origin]} · {statuses[current.status]} · 발생 {date(current.detected_at)} KST</p>
    <p className="knowledge-text">{current.description}</p>
    <nav className="row" aria-label="티켓 실습 도구">
      <a href="#incidents">동일 호스트 이력</a><a href="#knowledge">SOP·유사 장애</a>
      <a href="#analysis">AI 분석</a><a href="#actions">가상 조치</a><a href="#work">작업 기록</a>
    </nav>
    <section id="incidents" className="surface">
      <h2>동일 호스트의 과거 장애</h2>
      {incidents.error && <p role="alert">{incidents.error}</p>}
      {incidents.data?.incidents.length === 0 && <p>승인된 과거 장애가 없습니다.</p>}
      <ul>{incidents.data?.incidents.map(item => <li key={item.id}><a href={item.source_url}>{item.summary}</a>
        {' · '}{date(item.occurred_at)} KST <p>{item.resolution}</p></li>)}</ul>
    </section>
    <Knowledge base={base} session={session} ticketId={ticketId} />
    <TrainingAnalysis base={base} path={path} session={session} readOnly={readOnly} />
    <VirtualActions path={path} version={current.version} session={session} readOnly={readOnly} onVersion={setVersion} />
    <div id="work"><TicketOperations ticket={current} session={session} trainingPath={base} readOnly={readOnly} onVersion={setVersion} /></div>
  </>
}

function Knowledge({ base, ticketId, session }: { base: string; ticketId?: string; session: Session }) {
  const sops = useResource<{ items: Sop[] }>(base + '/sops')
  const [hits, setHits] = useState<Hit[]>()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const input = new FormData(event.currentTarget)
    const kind = input.get('kind')
    setError('')
    setHits(undefined)
    setPending(true)
    try {
      const response = await api<{ results: Hit[] }>(base + '/knowledge/search', {
        method: 'POST', body: JSON.stringify({
          query: input.get('query'), source_types: kind === 'SOP' ? ['SOP'] : ticketId ? ['TICKET', 'INCIDENT'] : ['TICKET'],
          ticket_id: ticketId,
        }),
      }, session.csrf_token)
      setHits(response.results)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setPending(false) }
  }
  return <section id="knowledge" className="surface">
    <h2>SOP·유사 장애 검색</h2>
    {(error || sops.error) && <p role="alert">{error || sops.error}</p>}
    <form onSubmit={search} className="row">
      <div><label htmlFor="training-source-kind">자료 유형</label>
        <select id="training-source-kind" name="kind"><option value="SOP">승인 SOP</option><option value="CASE">유사 장애</option></select></div>
      <label>검색어<input name="query" maxLength={300} required /></label>
      <button disabled={pending}>검색</button>
    </form>
    {pending && <p role="status">검색 중입니다.</p>}
    {hits && (hits.length ? <ul>{hits.map(hit => (<li key={hit.source_id}>
      <a href={hit.source_url}>{hit.title}</a>
      {hit.section && <span> · {hit.section}</span>}
      {hit.page_number && <span> · {hit.page_number}쪽</span>}
      <p className="knowledge-text">{hit.content}</p>
    </li>))}</ul> : <p>검색 결과가 없습니다.</p>)}
    <h3>승인 SOP</h3>
    {sops.data?.items.length === 0 && <p>현재 승인 SOP가 없습니다.</p>}
    <ul>{sops.data?.items.map(sop => <li key={sop.id}><a href={'/api/v1' + base + '/sops/' + sop.id}>{sop.title}</a>
      {' · '}<a href={sop.original_url}>원본</a></li>)}</ul>
  </section>
}

function TrainingAnalysis({ base, path, session, readOnly }: { base: string; path: string; session: Session; readOnly: boolean }) {
  const [analysisId, setId] = useState(() => new URLSearchParams(window.location.search).get('analysis'))
  const [analysis, setAnalysis] = useState<Analysis>()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const active = !!analysisId && (!analysis || ['queued', 'running'].includes(analysis.status))
  useEffect(() => {
    if (!analysisId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const next = await api<Analysis>(base + '/analyses/' + encodeURIComponent(analysisId!), { signal: controller.signal })
        if (controller.signal.aborted) return
        if (next.ticket_id !== path.split('/').at(-1)) { setId(null); setError('이 티켓의 분석 결과를 찾을 수 없습니다.'); return }
        setAnalysis(next)
        if (['queued', 'running'].includes(next.status)) timer = setTimeout(poll, 1000)
      } catch (failure) {
        if (!controller.signal.aborted) { setId(null); setError(errorMessage(failure)) }
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [analysisId, base, path])
  async function analyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = new FormData(event.currentTarget).get('question')
    setPending(true)
    setError('')
    try {
      const queued = await api<Analysis & { analysis_id: string }>(path + '/analyses', {
        method: 'POST', body: JSON.stringify({ message }),
      }, session.csrf_token)
      const url = new URL(window.location.href)
      url.searchParams.set('analysis', queued.analysis_id)
      window.history.replaceState(null, '', url)
      setAnalysis(queued)
      setId(queued.analysis_id)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setPending(false) }
  }
  return <section id="analysis" className="surface">
    <h2>AI 분석과 권고</h2>
    <p>권고는 참고 자료로 표시됩니다. 수행한 가상 조치는 별도로 기록됩니다.</p>
    {error && <p role="alert">{error}</p>}
    {!readOnly && <form onSubmit={analyze} className="row">
      <label>분석 질문<textarea name="question" rows={2} maxLength={2000} required /></label>
      <button disabled={pending || active}>분석 요청</button>
    </form>}
    {active && <p role="status">분석 중입니다.</p>}
    {analysis?.error && <p role="alert">{analysis.error.message}</p>}
    {analysis?.result && <ReportView report={analysis.result} />}
  </section>
}

function VirtualActions({ path, version, readOnly, session, onVersion }: {
  path: string; version: number; readOnly: boolean; session: Session; onVersion: (version: number) => void
}) {
  const [state, setState] = useState<{ catalog: CatalogAction[]; activity: Activity[] }>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const request = useRef<{ body: string; key: string } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    api<{ catalog: CatalogAction[]; activity: Activity[] }>(path + '/actions', { signal: controller.signal })
      .then(setState).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path, version])
  async function perform(code: string, parameters: Record<string, string>, note?: string) {
    const body = JSON.stringify({ action_code: code, parameters, note, expected_version: version })
    if (request.current?.body !== body) request.current = { body, key: crypto.randomUUID() }
    setBusy(true)
    setError('')
    try {
      const saved = await api<Activity>(path + '/actions', {
        method: 'POST', headers: { 'Idempotency-Key': request.current.key }, body,
      }, session.csrf_token)
      request.current = null
      onVersion(saved.ticket_version)
      return true
    } catch (failure) {
      setError(errorMessage(failure))
      if (failure instanceof ApiError) request.current = null
      return false
    } finally { setBusy(false) }
  }
  async function note(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    if (await perform('CUSTOM_NOTE', {}, String(new FormData(form).get('note')))) form.reset()
  }
  return <section id="actions" className="surface">
    <h2>가상 조치와 자유 메모</h2>
    {error && <p role="alert">{error}</p>}
    {state?.catalog.length === 0 && <p>이 시나리오 버전에는 가상 조치 카탈로그가 없습니다.</p>}
    <ul>{state?.catalog.map(action => <li key={action.action_code}>
      <button type="button" className="secondary" disabled={readOnly || busy || !action.available}
        onClick={() => void perform(action.action_code, action.parameters)}>{action.label}</button>
      {action.blocked_reason && <span> · {action.blocked_reason}</span>}
    </li>)}</ul>
    {!readOnly && <form onSubmit={note} className="row">
      <label>자유 메모<textarea name="note" required maxLength={4000} rows={2} /></label>
      <button disabled={busy}>메모 저장</button>
    </form>}
    <h3>조치·메모 기록</h3>
    <ol>{state?.activity.map(event => <li key={event.action_id}>
      <strong>{event.kind === 'NOTE' ? '자유 메모' : state.catalog.find(action => action.action_code === event.action_code)?.label ?? event.action_code}</strong>
      {' · '}{date(event.occurred_at)} KST · 기록 {event.event_sequence}
      {event.result && <p>{event.result.message} · {event.result.error_code ?? '오류 없음'}</p>}
      {event.note && <p className="knowledge-text">{event.note}</p>}
    </li>)}</ol>
  </section>
}
