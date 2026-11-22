import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ReportView } from './agent'
import type { Run as Analysis } from './agent'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Status, Ticket } from './api'
import { TicketOperations } from './ticket-operations'
import { TrainingEvaluation, TrainingHandover } from './training-evaluation'
import { trainingSourceHref } from './training-source'

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

function useResource<T>(path: string, revision = 0) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api<T>(path, { signal: controller.signal }).then(result => { setData(result); setError('') })
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path, revision])
  return { data, error }
}

function useSectionFocus(ready: boolean) {
  useEffect(() => {
    if (!ready) return
    const target = document.getElementById(window.location.hash.slice(1))
    if (target) {
      target.focus({ preventScroll: true })
      target.scrollIntoView({ block: 'start' })
    }
  }, [ready])
}

export function TrainingWorkspace({ runId, ticketId, session }: { runId: string; ticketId?: string; session: Session }) {
  const base = '/simulation-runs/' + runId
  const { data: run, error } = useResource<Run>(base)
  return <>
    <nav className="training-nav" aria-label="실습 탐색"><a href="/simulation">내 실습</a>{ticketId && <>
      <a href={base}>근무·티켓 목록</a><a href={base + '#outgoing'}>발신 인계</a>
      {run?.status === 'COMPLETED' && <a href={base + '#evaluation'}>평가 결과</a>}
    </>}</nav>
    {error && <p role="alert">{error}</p>}
    {!run && !error && <p role="status">실습을 불러오는 중입니다.</p>}
    {run && (ticketId ? <TicketWorkspace base={base} ticketId={ticketId} run={run} session={session} />
      : <Overview base={base} run={run} session={session} />)}
  </>
}

function Overview({ base, run, session }: { base: string; run: Run; session: Session }) {
  const schedule = useResource<{ slots: Slot[] }>(base + '/schedule')
  const incoming = useResource<Incoming>(base + '/incoming-handover')
  const [status, setStatus] = useState('')
  const tickets = useResource<{ items: TrainingTicket[]; next_cursor: string | null }>(base + '/tickets?limit=100')
  const matching = tickets.data?.items.filter(ticket => !status || status === ticket.status)
  const current = schedule.data?.slots[1]
  const completed = run.status === 'COMPLETED'
  useSectionFocus(true)
  return <>
    <div className="training-hero"><div>
      <p className="training-context">{run.scenario.key} · v{run.scenario.version}</p>
      <h1>{completed ? '완료된 데이터센터 근무' : '데이터센터 교대 근무'}</h1>
      <p>고정 실습 시각 {date(run.virtual_now)} KST{current && ` · ${current.crew_code}조 ${current.type === 'DAY' ? '주간' : '야간'}`}</p>
    </div><span className="training-badge">{completed ? '완료 · 읽기 전용' : '진행 중'}</span></div>
    <nav className="training-nav training-nav-sections" aria-label="실습 자료"><a href="#schedule">근무·인계</a><a href="#tickets">티켓</a>
      <a href="#knowledge">SOP·유사 장애</a><a href="#activity">행동 타임라인</a><a href="#outgoing">발신 인계</a>
      {completed && <a href="#evaluation">평가 결과</a>}</nav>
    <section id="schedule" className="surface" tabIndex={-1}>
      <h2>4조 2교대 근무</h2>
      {schedule.error && <p className="notice" role="alert">{schedule.error}</p>}
      {!schedule.data && !schedule.error && <p role="status">근무표를 불러오는 중입니다.</p>}
      <ol className="training-grid training-shifts">{schedule.data?.slots.map((slot, index) => (
        <li className="training-tile" key={slot.id} aria-current={index === 1 ? 'true' : undefined}>
          <span className="training-badge">{['이전', '현재', '다음'][index]}</span>
          <strong>{slot.crew_code}조 {slot.type === 'DAY' ? '주간' : '야간'} · {slot.actor_type === 'BOT' ? 'Bot' : '교육생'}</strong>
          <small>{date(slot.starts_at)} ~ {date(slot.ends_at)} KST</small></li>
      ))}</ol>
      <h3>이전 Bot 인계 보고서</h3>
      {incoming.error && <p className="notice" role="alert">{incoming.error}</p>}
      {!incoming.data && !incoming.error && <p role="status">이전 인계를 불러오는 중입니다.</p>}
      {incoming.data && <p className="knowledge-text">{incoming.data.summary || '인계 요약이 없습니다.'}</p>}
      {incoming.data?.tickets.length === 0 && <p>이월된 티켓이 없습니다.</p>}
      <ul className="training-list">{incoming.data?.tickets.map(link => (
        <li key={link.ticket_id}><a href={base + '/tickets/' + link.ticket_id}>
          {tickets.data?.items.find(ticket => ticket.id === link.ticket_id)?.title ?? '이월 티켓 열기'}</a></li>
      ))}</ul>
    </section>
    <section id="tickets" className="surface" tabIndex={-1}>
      <div className="training-heading"><h2>실습 티켓</h2><label>티켓 상태 필터<select value={status} onChange={event => setStatus(event.target.value)}>
        <option value="">전체</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label></div>
      {tickets.error && <p className="notice" role="alert">{tickets.error}</p>}
      {!tickets.data && !tickets.error && <p role="status">티켓 목록을 불러오는 중입니다.</p>}
      {matching?.length === 0 && <p>표시할 티켓이 없습니다.</p>}
      <ul className="training-list">{matching?.map(ticket => (
        <li key={ticket.id}><div><a href={base + '/tickets/' + ticket.id}>{ticket.title}</a>
          <p className="meta">{ticket.number} · {ticket.severity} · {origin[ticket.training_origin]}</p></div>
          <span className="training-badge" data-state={ticket.status}>{statuses[ticket.status]}</span></li>
      ))}</ul>
    </section>
    <Knowledge base={base} session={session} />
    <RunTimeline base={base} titles={Object.fromEntries(tickets.data?.items.map(ticket => [ticket.id, ticket.title]) ?? [])} />
    <TrainingHandover base={base} session={session} completed={completed} />
    {completed && <TrainingEvaluation base={base} />}
  </>
}

function TicketWorkspace({ base, ticketId, run, session }: { base: string; ticketId: string; run: Run; session: Session }) {
  const path = base + '/tickets/' + ticketId
  const [latestVersion, setVersion] = useState(0)
  const ticket = useResource<TrainingTicket>(path, latestVersion)
  const incidents = useResource<{ incidents: Incident[]; host: { hostname: string; host_key: string } | null }>(path + '/host-incidents')
  useSectionFocus(!!ticket.data)
  if (ticket.error) return <p role="alert">{ticket.error}</p>
  if (!ticket.data) return <p role="status">티켓을 불러오는 중입니다.</p>
  const current = { ...ticket.data, version: Math.max(ticket.data.version, latestVersion) }
  const readOnly = run.status === 'COMPLETED' || !!current.archived_at
  return <>
    <div className="training-hero"><div><p className="training-context">{current.number} · {current.severity} · {origin[current.training_origin]}</p>
      <h1>{current.title}</h1><p>발생 {date(current.detected_at)} KST{incidents.data?.host && ` · ${incidents.data.host.hostname}`}</p></div>
      <span className="training-badge" data-state={current.status}>{statuses[current.status]}{readOnly && ' · 읽기 전용'}</span></div>
    <nav className="training-nav training-nav-sections" aria-label="티켓 실습 도구">
      <a href="#ticket-summary">장애 개요</a>
      <a href="#incidents">동일 호스트 이력</a><a href="#knowledge">SOP·유사 장애</a>
      <a href="#analysis">AI 분석</a><a href="#actions">가상 조치</a><a href="#work">작업 기록</a>
    </nav>
    <section id="ticket-summary" className="surface" tabIndex={-1}>
      <h2>장애 개요</h2><p className="knowledge-text">{current.description}</p>
      {current.impact && <p>영향 {current.impact}</p>}
      {current.resolution && <p>해결 기록 {current.resolution}</p>}
      {!readOnly && <TrainingTransition path={path} ticket={current} session={session} onVersion={setVersion} />}
    </section>
    <section id="incidents" className="surface" tabIndex={-1}>
      <h2>동일 호스트의 과거 장애</h2>
      {incidents.error && <p role="alert">{incidents.error}</p>}
      {!incidents.data && !incidents.error && <p role="status">호스트 이력을 불러오는 중입니다.</p>}
      {incidents.data?.incidents.length === 0 && <p>승인된 과거 장애가 없습니다.</p>}
      <ul className="training-materials">{incidents.data?.incidents.map(item => <li key={item.id}><a href={trainingSourceHref(item.source_url)}>{item.summary}</a>
        {' · '}{date(item.occurred_at)} KST <p>{item.resolution}</p>
        {!readOnly && <LearningControl path={path} kind="HOST_HISTORY" sourceId={item.id} session={session} />}
      </li>)}</ul>
    </section>
    <Knowledge base={base} session={session} ticketId={ticketId} readOnly={readOnly} />
    <TrainingAnalysis base={base} path={path} session={session} readOnly={readOnly} />
    <VirtualActions path={path} version={current.version} session={session} readOnly={readOnly} onVersion={setVersion} />
    <div id="work" tabIndex={-1}><TicketOperations ticket={current} session={session} trainingPath={base} readOnly={readOnly} onVersion={setVersion} /></div>
  </>
}

function TrainingTransition({ path, ticket, session, onVersion }: {
  path: string; ticket: TrainingTicket; session: Session; onVersion: (version: number) => void
}) {
  const [target, setTarget] = useState<Status | ''>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!ticket.allowed_transitions.length) return null

  async function transition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    setBusy(true)
    setError('')
    try {
      const saved = await api<TrainingTicket>(path + '/transitions', {
        method: 'POST', body: JSON.stringify({ expected_version: ticket.version, to_status: target,
          reason: data.get('reason'), resolution: target === 'resolved' ? data.get('resolution') : undefined }),
      }, session.csrf_token)
      form.reset()
      setTarget('')
      onVersion(saved.version)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }

  return <details className="training-details">
    <summary>티켓 상태 변경</summary>
    <form onSubmit={transition}>
      {error && <p className="notice" role="alert">{error}</p>}
      <label>다음 상태<select required value={target} onChange={event => setTarget(event.target.value as Status | '')}>
        <option value="">선택</option>{ticket.allowed_transitions.map(state => <option key={state} value={state}>{statuses[state]}</option>)}
      </select></label>
      <label>변경 사유<textarea name="reason" rows={2} maxLength={4000} required /></label>
      {target === 'resolved' && <label>해결 요약<textarea name="resolution" rows={2} maxLength={4000} required /></label>}
      <button disabled={busy || !target}>상태 저장</button>
    </form>
  </details>
}

type RunEvent = { id: string; ticket_id: string; event_type: string; event_sequence: number; occurred_at: string }
const eventNames: Record<string, string> = { scenario_seeded: '시나리오 티켓 준비', created: '티켓 등록', updated: '티켓 수정',
  transitioned: '티켓 상태 변경', archived: '티켓 보관', operations_updated: '업무 정보 수정', work_created: '후속 작업 등록',
  work_updated: '후속 작업 변경', learning_evidence: '학습 자료 기록', virtual_action_performed: '가상 조치 수행', training_note: '자유 메모' }

function RunTimeline({ base, titles }: { base: string; titles: Record<string, string> }) {
  const [cursor, setCursor] = useState(0)
  const timeline = useResource<{ items: RunEvent[]; next_after_sequence: number | null }>(`${base}/events?after_sequence=${cursor}`)
  return <section id="activity" className="surface" tabIndex={-1}>
    <h2>실습 행동 타임라인</h2>
    {timeline.error && <p role="alert">{timeline.error}</p>}
    {!timeline.data && !timeline.error && <p role="status">행동 기록을 불러오는 중입니다.</p>}
    {timeline.data?.items.length === 0 && <p>이 구간에 표시할 행동이 없습니다.</p>}
    <ol className="training-list">{timeline.data?.items.map(event => <li key={event.id}>
      <div><strong>{event.event_sequence} · {eventNames[event.event_type] ?? '티켓 기록'}</strong>
        <p className="meta">{date(event.occurred_at)} KST</p></div>
      <a href={`${base}/tickets/${event.ticket_id}#work`}>{titles[event.ticket_id] ?? '티켓 작업 기록'}</a></li>)}</ol>
    <div className="row">
      {cursor > 0 && <button className="secondary" onClick={() => setCursor(0)}>처음 기록</button>}
      {timeline.data?.next_after_sequence != null && <button className="secondary" onClick={() => setCursor(timeline.data!.next_after_sequence!)}>다음 기록</button>}
    </div>
  </section>
}

function Knowledge({ base, ticketId, session, readOnly = false }: {
  base: string; ticketId?: string; session: Session; readOnly?: boolean
}) {
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
  return <section id="knowledge" className="surface" tabIndex={-1}>
    <h2>SOP·유사 장애 검색</h2>
    {(error || sops.error) && <p role="alert">{error || sops.error}</p>}
    <form onSubmit={search} className="row">
      <div><label htmlFor="training-source-kind">자료 유형</label>
        <select id="training-source-kind" name="kind"><option value="SOP">승인 SOP</option><option value="CASE">유사 장애</option></select></div>
      <label>검색어<input name="query" maxLength={300} required /></label>
      <button disabled={pending}>검색</button>
    </form>
    {pending && <p role="status">검색 중입니다.</p>}
    {hits && (hits.length ? <><p role="status">검색 후보 {hits.length}건입니다. 판단에 사용하기 전에 실제 증상과 출처를 비교하세요.</p>
      <ul className="training-materials">{hits.map((hit, index) => (<li key={`${hit.source_id}-${index}`}>
      <a href={trainingSourceHref(hit.source_url)}>{hit.title}</a>
      {hit.section && <span> · {hit.section}</span>}
      {hit.page_number && <span> · {hit.page_number}쪽</span>}
      <p className="knowledge-text">{hit.content}</p>
    </li>))}</ul></> : <p role="status">검색 결과가 없습니다. 오류 코드나 장비 이름으로 다시 찾아보세요.</p>)}
    <h3>승인 SOP</h3>
    {!sops.data && !sops.error && <p role="status">승인 자료를 불러오는 중입니다.</p>}
    {sops.data?.items.length === 0 && <p>현재 승인 SOP가 없습니다.</p>}
    <ul className="training-materials">{sops.data?.items.map(sop => <li key={sop.id}><a href={base + '/sops/' + sop.id}>{sop.title}</a>
      {' · '}<a href={sop.original_url}>원본</a>
      {ticketId && !readOnly && <LearningControl path={base + '/tickets/' + ticketId} kind="SOP" sourceId={sop.id} session={session} />}
    </li>)}</ul>
  </section>
}

function LearningControl({ path, kind, sourceId, session }: {
  path: string; kind: 'HOST_HISTORY' | 'SOP'; sourceId: string; session: Session
}) {
  const [options, setOptions] = useState<Activity[]>()
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function save(activity: 'VIEWED' | 'USED', action?: string) {
    setError('')
    setBusy(true)
    try {
      await api(path + '/learning-evidence', {
        method: 'POST', body: JSON.stringify({ kind, source_id: sourceId, activity, action_event_id: action }),
      }, session.csrf_token)
      setStatus(activity === 'VIEWED' ? '자료 확인 기록 저장됨' : '자료 활용 기록 저장됨')
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }
  async function loadActions() {
    setError('')
    try {
      const data = await api<{ activity: Activity[] }>(path + '/actions')
      setOptions(data.activity.filter(entry => entry.kind === 'ACTION'))
    } catch (failure) { setError(errorMessage(failure)) }
  }
  return <div className="row">
    <button className="secondary" type="button" disabled={busy} onClick={() => void save('VIEWED')}>자료 확인 기록</button>
    <button className="secondary" type="button" disabled={busy} onClick={() => void loadActions()}>수행한 조치에 연결</button>
    {status && <span role="status">{status}</span>}
    {error && <span role="alert">{error}</span>}
    {options && (options.length === 0 ? <p>연결할 수행 조치가 없습니다.</p> :
      <form className="row" onSubmit={event => {
        event.preventDefault()
        const action = new FormData(event.currentTarget).get('action')
        if (typeof action === 'string' && action) void save('USED', action)
      }}>
        <label aria-label="수행 조치">수행 조치<select name="action" required defaultValue=""><option value="">선택</option>
          {options.map(item => <option key={item.action_id} value={item.action_id}>
            {item.event_sequence} · {item.result?.message ?? item.action_code}</option>)}
        </select></label>
        <button disabled={busy}>자료 활용 저장</button>
      </form>)}
  </div>
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
  return <section id="analysis" className="surface" tabIndex={-1}>
    <h2>AI 분석과 권고</h2>
    <p>권고는 참고 자료로 표시됩니다. 수행한 가상 조치는 별도로 기록됩니다.</p>
    {error && <p role="alert">{error}</p>}
    {!readOnly && <form onSubmit={analyze} className="row">
      <label>분석 질문<textarea name="question" rows={2} maxLength={2000} required /></label>
      <button disabled={pending || active}>분석 요청</button>
    </form>}
    {active && <p role="status">분석 중입니다.</p>}
    {analysis?.error && <div className="notice" role="alert"><strong>AI 분석 실패</strong><p>{analysis.error.message}</p>
      <p>호스트 이력과 SOP, 가상 조치 및 인계는 계속 사용할 수 있습니다.</p></div>}
    {readOnly && !analysisId && <p>이 주소에 연결된 분석 기록이 없습니다.</p>}
    {analysis?.result && <ReportView report={analysis.result} sourceHref={trainingSourceHref} />}
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
  return <section id="actions" className="surface" tabIndex={-1}>
    <h2>가상 조치와 자유 메모</h2>
    {error && <p role="alert">{error}</p>}
    {!state && !error && <p role="status">조치 카탈로그를 불러오는 중입니다.</p>}
    {busy && <p role="status">실습 행동을 저장하고 있습니다.</p>}
    {state?.catalog.length === 0 && <p>이 시나리오 버전에는 가상 조치 카탈로그가 없습니다.</p>}
    <ul className="training-grid training-actions">{state?.catalog.map(action => <li className="training-tile" key={action.action_code}>
      <button type="button" className="secondary" disabled={readOnly || busy || !action.available}
        onClick={() => void perform(action.action_code, action.parameters)}>{action.label}</button>
      {action.blocked_reason && <small>{action.blocked_reason}</small>}
    </li>)}</ul>
    {!readOnly && <form onSubmit={note} className="row">
      <label>자유 메모<textarea name="note" required maxLength={4000} rows={2} /></label>
      <button disabled={busy}>메모 저장</button>
    </form>}
    <h3>조치·메모 기록</h3>
    {state?.activity.length === 0 && <p>아직 저장된 조치나 메모가 없습니다.</p>}
    <ol>{state?.activity.map(event => <li key={event.action_id}>
      <strong>{event.kind === 'NOTE' ? '자유 메모' : state.catalog.find(action => action.action_code === event.action_code)?.label ?? event.action_code}</strong>
      {' · '}{date(event.occurred_at)} KST · 기록 {event.event_sequence}
      {event.result && <p>{event.result.message} · {event.result.error_code ?? '오류 없음'}</p>}
      {event.note && <p className="knowledge-text">{event.note}</p>}
    </li>)}</ol>
  </section>
}
