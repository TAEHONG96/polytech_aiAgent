import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ReportView } from './agent'
import type { Run as Analysis } from './agent'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, SimulationStatus, Status, Ticket } from './api'
import { TicketOperations } from './ticket-operations'
import { useCompanionPart } from './companion/companion-context'
import { tutorTabId } from './companion/companion-tutor'
import { TrainingEvaluation, TrainingHandover } from './training-evaluation'
import { trainingSourceHref } from './training-source'
import { Badge, Feedback, Priority, TicketStatus } from './ui'

const kst = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short',
})
const date = (value: string) => kst.format(new Date(value))
const origin = { INITIAL: '초기', CARRYOVER: '이월', USER_CREATED: '직접 등록' }
type TrainingTicket = Ticket & {
  training_origin: keyof typeof origin; scenario_host_id: string | null
  related_sops?: { id: string; title: string; source_url: string }[]
}
type RunHost = {
  id: string; hostname: string; host_key: string
  state: { asset_kind?: string; error_code?: string | null; health?: string }
}
const assetLabels: Record<string, string> = { memory: '메모리', storage: '스토리지' }
const healthLabels: Record<string, string> = { degraded: '성능 저하', verification_pending: '재진단 필요', healthy: '정상' }
type Run = { status: SimulationStatus; virtual_now: string; scenario: { key: string; version: string } }
type Slot = { id: string; crew_code: string; type: string; actor_type: string; starts_at: string; ends_at: string }
type Incoming = { summary: string; tickets: { ticket_id: string }[] }
type Incident = { id: string; ticket_key: string | null; summary: string; occurred_at: string; resolution: string; source_url: string }
type Hit = { source_type: 'SOP' | 'TICKET' | 'INCIDENT'; source_id: string; title: string; content: string; source_url: string; section: string | null; page_number: number | null }
type CatalogAction = {
  action_code: string; label: string; parameters: Record<string, string>; available: boolean
  blocked_reason: string | null; repeatable?: boolean; requires_note?: boolean
}
type Activity = {
  action_id: string; action_code: string; kind: 'ACTION' | 'NOTE'; occurred_at: string
  event_sequence: number; ticket_version: number; note: string | null; result: ActionResult | null
}

type ActionResult = {
  message: string; health: string; error_code: string | null
  observations?: { label: string; target?: string; window?: string; value: string | number | null; before?: string | number; unit?: string }[]
  unconfirmed?: string[]
}

function ActionResultDetails({ result }: { result: ActionResult }) {
  return <div className="training-action-result">
    <p>{result.message}</p>
    {result.observations?.length ? <dl>{result.observations.map((entry, index) => {
      const measure = (value: number | string) => `${value}${entry.unit ?? ''}`
      return <div key={index}>
        <dt>{entry.label}{entry.target && ` · ${entry.target}`}</dt>
        <dd>{entry.before !== undefined && `${measure(entry.before)} → `}{entry.value === null ? '미확인' : measure(entry.value)}
          {entry.window && <small> · {entry.window}</small>}</dd>
      </div>
    })}</dl> : null}
    {result.unconfirmed?.length ? <p><strong>미확인</strong> · {result.unconfirmed.join(', ')}</p> : null}
    {(result.error_code || !result.observations) && <small>장애 코드 {result.error_code ?? '오류 없음'}</small>}
  </div>
}

function useResource<T>(path: string, revision = 0) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api<T>(path, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) { setData(result); setError('') }
    })
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
      const disclosure = target.closest('details')
      if (disclosure) disclosure.open = true
      target.focus({ preventScroll: true })
      target.scrollIntoView({ block: 'start' })
    }
  }, [ready])
}

export function TrainingWorkspace({ runId, ticketId, session }: { runId: string; ticketId?: string; session: Session }) {
  const base = '/simulation-runs/' + runId
  const { data: run, error } = useResource<Run>(base)
  useCompanionPart('run', { phase: error ? 'error' : run ? 'ready' : 'loading', status: run?.status, scenario: run?.scenario.key })
  return <>
    <nav className="training-nav training-breadcrumbs" aria-label="실습 탐색">
      {run && <a href={base + '/sops'}>SOP 목록</a>}{ticketId && <>
      <a href={base}>근무·티켓 목록</a><a href={base + '#outgoing'}>발신 인계</a>
      {run?.status === 'COMPLETED' && <a href={base + '#evaluation'}>평가 결과</a>}
    </>}</nav>
    {error && <p role="alert">{error}</p>}
    {!run && !error && <p role="status">실습을 불러오는 중입니다.</p>}
    {run?.status === 'PAUSED' && <p className="notice">일시정지된 실습입니다. <a href="/simulation">내 실습에서 이어서 진행</a></p>}
    {run && (ticketId ? <TicketWorkspace base={base} ticketId={ticketId} run={run} session={session} />
      : <Overview base={base} run={run} session={session} />)}
  </>
}

function Overview({ base, run, session }: { base: string; run: Run; session: Session }) {
  const schedule = useResource<{ slots: Slot[] }>(base + '/schedule')
  const incoming = useResource<Incoming>(base + '/incoming-handover')
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const tickets = useResource<{ items: TrainingTicket[]; next_cursor: string | null }>(base + '/tickets?limit=100')
  const search = query.trim().toLocaleLowerCase()
  const matching = tickets.data?.items.filter(ticket => (!status || status === ticket.status) &&
    (!search || [ticket.number, ticket.title, ticket.service].some(value => value.toLocaleLowerCase().includes(search))))
  const current = schedule.data?.slots[1]
  const completed = run.status === 'COMPLETED'
  useCompanionPart('tickets', { phase: tickets.error ? 'error' : !tickets.data ? 'loading' : tickets.data.items.length ? 'ready' : 'empty' })
  useCompanionPart('incoming', { phase: incoming.error ? 'error' : !incoming.data ? 'loading' : incoming.data.tickets.length ? 'ready' : 'empty' })
  useSectionFocus(true)
  return <>
    <div className="training-hero"><div>
      <p className="training-context">{run.scenario.key} · v{run.scenario.version}</p>
      <h1>{completed ? '완료된 데이터센터 근무' : '데이터센터 교대 근무'}</h1>
      <p>고정 실습 시각 {date(run.virtual_now)} KST{current && ` · ${current.crew_code}조 ${current.type === 'DAY' ? '주간' : '야간'}`}</p>
    </div><span className="training-badge">{completed ? '완료 · 읽기 전용' : run.status === 'PAUSED' ? '일시정지 · 읽기 전용' : '진행 중'}</span></div>
    <nav className="training-nav training-nav-sections" aria-label="실습 자료"><a href="#schedule">근무·인계</a><a href="#tickets">티켓</a>
      <a href="#knowledge">SOP·유사 장애</a><a href="#activity">행동 타임라인</a><a href="#outgoing">발신 인계</a>
      {completed && <a href="#evaluation">평가 결과</a>}</nav>
    <div className="training-workgrid">
    <section id="tickets" className="surface" tabIndex={-1}>
      <div className="training-heading"><h2>실습 티켓</h2></div>
      <div className="training-filters">
        <label>티켓 검색<input type="search" data-companion-anchor="ticket-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="티켓 번호, 제목, 영향 서비스" /></label>
        <label>티켓 상태 필터<select value={status} onChange={event => setStatus(event.target.value)}>
          <option value="">전체</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      </div>
      {tickets.error && <p className="notice" role="alert">{tickets.error}</p>}
      {!tickets.data && !tickets.error && <Feedback>티켓 목록을 불러오는 중입니다.</Feedback>}
      {matching?.length === 0 && <Feedback kind="empty">조건에 맞는 실습 티켓이 없습니다.</Feedback>}
      {!!matching?.length && <div className="table-scroll training-table" role="region" aria-label="실습 티켓 목록" tabIndex={0}>
        <table><thead><tr><th scope="col">티켓</th><th scope="col">우선순위</th><th scope="col">상태</th></tr></thead>
          <tbody>{matching.map(ticket => <tr key={ticket.id}>
            <td><a href={base + '/tickets/' + ticket.id}>{ticket.title}</a>
              <div className="meta"><code>{ticket.number}</code> · {ticket.service} · {origin[ticket.training_origin]}</div></td>
            <td><Priority value={ticket.severity} /></td><td><TicketStatus ticket={ticket} /></td>
          </tr>)}</tbody>
        </table>
      </div>}
    </section>
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
    </div>
    <Knowledge base={base} session={session} />
    <RunTimeline base={base} titles={Object.fromEntries(tickets.data?.items.map(ticket => [ticket.id, ticket.title]) ?? [])} />
    <TrainingHandover base={base} session={session} status={run.status} />
    {completed && <TrainingEvaluation base={base} />}
  </>
}

function TicketWorkspace({ base, ticketId, run, session }: { base: string; ticketId: string; run: Run; session: Session }) {
  const path = base + '/tickets/' + ticketId
  const [latestVersion, setVersion] = useState(0)
  const ticket = useResource<TrainingTicket>(path, latestVersion)
  const hosts = useResource<{ items: RunHost[] }>(base + '/hosts', latestVersion)
  const incidents = useResource<{ incidents: Incident[]; host: { hostname: string; host_key: string } | null }>(path + '/host-incidents')
  const host = hosts.data?.items.find(item => item.id === ticket.data?.scenario_host_id)
  useCompanionPart('ticket', { phase: ticket.error ? 'error' : ticket.data ? 'ready' : 'loading', id: ticketId,
    number: ticket.data?.number, status: ticket.data?.status, severity: ticket.data?.severity,
    origin: ticket.data?.training_origin, kind: host?.state.asset_kind })
  useSectionFocus(!!ticket.data)
  if (ticket.error) return <p role="alert">{ticket.error}</p>
  if (!ticket.data) return <p role="status">티켓을 불러오는 중입니다.</p>
  const current = { ...ticket.data, version: Math.max(ticket.data.version, latestVersion) }
  const asset = host?.state.asset_kind
  const health = host?.state.health
  const errorCode = host?.state.error_code
  const readOnly = run.status !== 'ACTIVE' || !!current.archived_at
  return <>
    <div className="training-hero"><div><p className="training-context"><code>{current.number}</code> · {origin[current.training_origin]}</p>
      <h1>{current.title}</h1><p>발생 {date(current.detected_at)} KST{incidents.data?.host && ` · ${incidents.data.host.hostname}`}</p></div>
      <div className="row"><Priority value={current.severity} /><TicketStatus ticket={current} />
        {readOnly && <Badge>읽기 전용</Badge>}</div></div>
    <nav className="training-nav training-nav-sections" aria-label="티켓 실습 도구">
      <a href="#ticket-summary">장애 개요</a>
      <a href="#incidents">동일 호스트 이력</a><a href="#knowledge">SOP·유사 장애</a>
      <a href="#analysis">AI 분석</a><a href="#actions">가상 조치</a><a href="#work">작업 이력</a>
    </nav>
    <div className="training-workgrid"><section id="ticket-summary" className="surface" tabIndex={-1}>
      <h2>장애 개요</h2><p className="knowledge-text">{current.description}</p>
      {readOnly ? <p>영향 범위: {current.impact ?? '기록 없음'}</p>
        : <TrainingImpact key={current.id} path={path} ticket={current} session={session} onVersion={setVersion} />}
      {current.resolution && <p>해결 기록 {current.resolution}</p>}
      {!readOnly && <TrainingTransition path={path} ticket={current} session={session} onVersion={setVersion} />}
    </section><aside className="surface" aria-label="티켓 참고 정보">
      <h2>대상 시스템</h2>
      <dl className="details">
        <div><dt>티켓 키</dt><dd><code>{current.number}</code></dd></div>
        <div><dt>티켓 ID</dt><dd><code>{current.id}</code></dd></div>
        <div><dt>서비스</dt><dd>{current.service}</dd></div>
        {host && <>
          <div><dt>호스트명</dt><dd><code>{host.hostname}</code></dd></div>
          <div><dt>호스트 키</dt><dd><code>{host.host_key}</code></dd></div>
          <div><dt>장애 분류</dt><dd>{asset ? (assetLabels[asset] ?? asset) : '미등록'}</dd></div>
          <div><dt>현재 장애 코드</dt><dd>{errorCode ? <code>{errorCode}</code> : errorCode === null ? '오류 없음' : '미등록'}</dd></div>
          <div><dt>현재 호스트 상태</dt><dd>{health ? (healthLabels[health] ?? health) : '미등록'}</dd></div>
        </>}
      </dl>
      {hosts.error ? <p role="alert">호스트 정보 조회 실패. {hosts.error}</p>
        : !hosts.data ? <p role="status">호스트 정보를 불러오는 중입니다.</p>
          : !host && <p>연결된 가상 호스트가 없습니다.</p>}
      {!!current.related_sops?.length && <div>
        <h3>관련 SOP</h3>
        <ul>{current.related_sops.map(sop => <li key={sop.id}><a href={trainingSourceHref(sop.source_url)}>{sop.title}</a></li>)}</ul>
      </div>}
    </aside></div>
    <section id="incidents" className="surface" tabIndex={-1}>
      <h2>동일 호스트의 과거 장애</h2>
      <p className="meta">현재 티켓 발생 전에 같은 호스트에서 발생한 승인 사례입니다.</p>
      {incidents.error && <p role="alert">{incidents.error}</p>}
      {!incidents.data && !incidents.error && <p role="status">호스트 이력을 불러오는 중입니다.</p>}
      {incidents.data?.incidents.length === 0 && <p>승인된 과거 장애가 없습니다.</p>}
      <ul className="training-materials">{incidents.data?.incidents.map(item => <li key={item.id}><a href={trainingSourceHref(item.source_url)}>{item.summary}</a>
        {' · '}{date(item.occurred_at)} KST · <code>{item.ticket_key ?? '티켓 키 미등록'}</code>
        <p>{item.resolution}</p>
        {!readOnly && <LearningControl path={path} kind="HOST_HISTORY" sourceId={item.id} session={session} />}
      </li>)}</ul>
    </section>
    <Knowledge base={base} session={session} ticketId={ticketId} readOnly={readOnly} initialQuery={current.description} />
    <TrainingAnalysis base={base} path={path} session={session} readOnly={readOnly} />
    <VirtualActions path={path} version={current.version} session={session} readOnly={readOnly} onVersion={setVersion} />
    <div id="work" tabIndex={-1}><TicketOperations ticket={current} session={session} trainingPath={base} readOnly={readOnly} onVersion={setVersion} /></div>
  </>
}

function TrainingImpact({ path, ticket, session, onVersion }: {
  path: string; ticket: TrainingTicket; session: Session; onVersion: (version: number) => void
}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const impact = String(new FormData(event.currentTarget).get('impact') ?? '').trim() || null
    setBusy(true)
    setMessage('')
    setError('')
    try {
      const updated = await api<TrainingTicket>(path, {
        method: 'PATCH', body: JSON.stringify({ expected_version: ticket.version, impact }),
      }, session.csrf_token)
      onVersion(updated.version)
      setMessage('영향 범위를 저장했습니다. 인계 초안이 있으면 인계 자료를 다시 수집하세요.')
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return <form onSubmit={save}>
    <label>영향 범위<textarea name="impact" maxLength={4000} rows={2}
      placeholder="예: 컴퓨팅 노드 1대에 오류 발생, 사용자 영향 확인 중" defaultValue={ticket.impact ?? ''} /></label>
    <button className="secondary" disabled={busy}>영향 범위 저장</button>
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </form>
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
      <label>변경 사유<textarea name="reason" rows={2} maxLength={4000} required placeholder="예: 재진단에서 오류가 사라져 해결로 변경" /></label>
      {target === 'resolved' && <label>해결 요약<textarea name="resolution" rows={2} maxLength={4000} required
        placeholder="예: 가상 메모리 교체 후 재진단에서 정상 확인" /></label>}
      <button disabled={busy || !target}>상태 저장</button>
    </form>
  </details>
}

type RunEvent = { id: string; ticket_id: string; event_type: string; event_sequence: number; occurred_at: string }
const eventNames: Record<string, string> = { scenario_seeded: '시나리오 티켓 준비', scenario_observation: '실습 시작 전 관측·조치',
  created: '티켓 등록', updated: '티켓 수정',
  transitioned: '티켓 상태 변경', archived: '티켓 보관', operations_updated: '업무 정보 수정', work_created: '후속 작업 등록',
  work_updated: '후속 작업 변경', learning_evidence: '학습 자료 기록', virtual_action_performed: '가상 조치 수행', training_note: '자유 메모' }

function RunTimeline({ base, titles }: { base: string; titles: Record<string, string> }) {
  const [cursor, setCursor] = useState(0)
  const timeline = useResource<{ items: RunEvent[]; next_after_sequence: number | null }>(`${base}/events?after_sequence=${cursor}`)
  return <details className="surface training-timeline">
    <summary>실습 행동 타임라인</summary>
    <section id="activity" aria-label="실습 행동 타임라인 기록" tabIndex={-1}>
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
  </details>
}

function Knowledge({ base, ticketId, session, readOnly = false, initialQuery = '' }: {
  base: string; ticketId?: string; session: Session; readOnly?: boolean; initialQuery?: string
}) {
  const [hits, setHits] = useState<Hit[]>()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [submitted, setSubmitted] = useState<{ kind: 'sop' | 'case'; request: number }>()
  const [tutorReceipt, setTutorReceipt] = useState<string>()
  const [keywordSearch, setKeywordSearch] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  useCompanionPart('search', submitted && { ...submitted, tutorReceipt,
    phase: error ? 'error' : pending || !hits ? 'loading' : hits.length ? 'ready' : 'empty' })
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const input = new FormData(event.currentTarget)
    const kind = input.get('kind')
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setSubmitted(previous => ({ kind: kind === 'SOP' ? 'sop' : 'case', request: (previous?.request ?? 0) + 1 }))
    setError('')
    setHits(undefined)
    setTutorReceipt(undefined)
    setKeywordSearch(false)
    setPending(true)
    try {
      const response = await api<{ results: Hit[]; tutor_receipt?: string; search_mode?: 'semantic' | 'keyword' }>(base + '/knowledge/search', {
        method: 'POST', signal: controller.signal, body: JSON.stringify({
          query: input.get('query'), source_types: kind === 'SOP' ? ['SOP'] : ticketId ? ['TICKET', 'INCIDENT'] : ['TICKET'],
          ticket_id: ticketId,
          tutor_tab_id: session.corpus_training && ticketId && kind === 'SOP' ? tutorTabId() : undefined,
        }),
      }, session.csrf_token)
      if (!controller.signal.aborted) {
        setHits(response.results)
        setTutorReceipt(response.tutor_receipt ?? undefined)
        setKeywordSearch(response.search_mode === 'keyword')
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure))
    } finally {
      if (!controller.signal.aborted) setPending(false)
    }
  }
  return <section id="knowledge" className="surface" tabIndex={-1}>
    <h2>SOP·유사 장애 검색</h2>
    {error && <p role="alert">{error}</p>}
    <form onSubmit={search} className="training-search">
      <div><label htmlFor="training-source-kind">자료 유형</label>
        <select id="training-source-kind" name="kind"><option value="SOP">승인 SOP</option><option value="CASE">유사 장애</option></select></div>
      <label>검색어<input name="query" data-companion-anchor="sop-query" maxLength={300} required defaultValue={initialQuery.replace(/\s+/g, ' ').trim().slice(0, 300)}
        placeholder="예: 메모리 ECC 오류 재진단" /></label>
      <button disabled={pending} data-companion-anchor="search-sop">{pending ? '검색 중…' : '검색'}</button>
    </form>
    {pending && <p role="status">검색 중입니다.</p>}
    {keywordSearch && <p className="meta">Gemini 임베딩 연결이 지연되어 기존 자료에서 키워드로 검색했어요.</p>}
    {hits && (hits.length ? <><p role="status">검색 후보 {hits.length}건입니다. 판단에 사용하기 전에 실제 증상과 출처를 비교하세요.</p>
      <ul className="training-materials">{hits.map((hit, index) => (<li key={`${hit.source_id}-${index}`}>
      <a href={trainingSourceHref(hit.source_url)}>{hit.title}</a>
      {hit.section && <span> · {hit.section}</span>}
      {hit.page_number && <span> · {hit.page_number}쪽</span>}
      <details className="training-excerpt">
        <summary><span aria-hidden="true">{hit.content}</span>검색 구절 펼치기 / 접기</summary>
        <p className="knowledge-text">{hit.content}</p>
      </details>
      {hit.source_type === 'SOP' && ticketId && !readOnly &&
        <LearningControl path={base + '/tickets/' + ticketId} kind="SOP" sourceId={hit.source_id} session={session} />}
    </li>))}</ul></> : <p role="status">검색 결과가 없습니다. 오류 코드나 장비 이름으로 다시 찾아보세요.</p>)}
  </section>
}

function LearningControl({ path, kind, sourceId, session }: {
  path: string; kind: 'HOST_HISTORY' | 'SOP'; sourceId: string; session: Session
}) {
  type LearningActions = { viewed: boolean; linked: boolean; items: Pick<Activity, 'action_id' | 'action_code' | 'event_sequence' | 'result'>[] }
  const [options, setOptions] = useState<LearningActions>()
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
      setOptions(undefined)
      setStatus(activity === 'VIEWED' ? '자료 확인 기록 저장됨' : '자료 활용 기록 저장됨')
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }
  async function loadActions() {
    setError('')
    setBusy(true)
    try {
      const query = new URLSearchParams({ kind, source_id: sourceId })
      setOptions(await api<LearningActions>(path + '/learning-evidence/actions?' + query))
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }
  return <div className="row">
    <p className="meta">자료를 읽었다고 기록한 뒤, 현재 티켓에서 수행한 가상 조치를 연결하세요.</p>
    <button className="secondary" type="button" disabled={busy} onClick={() => void save('VIEWED')}>이 자료를 읽었어요</button>
    <button className="secondary" type="button" disabled={busy} onClick={() => void loadActions()}>이 자료를 참고해 조치했어요</button>
    {status && <span role="status">{status}</span>}
    {error && <span role="alert">{error}</span>}
    {options?.linked && <p>이 자료에 수행 조치를 이미 연결했습니다.</p>}
    {options && !options.viewed && <p>먼저 ‘이 자료를 읽었어요’를 눌러 확인을 기록하세요.</p>}
    {options?.viewed && !options.linked && options.items.length === 0 &&
      <p>자료 확인 후 이 티켓에서 수행한 <a href="#actions">가상 조치</a>가 없습니다.</p>}
    {options && options.items.length > 0 &&
      <form className="row" onSubmit={event => {
        event.preventDefault()
        const action = new FormData(event.currentTarget).get('action')
        if (typeof action === 'string' && action) void save('USED', action)
      }}>
        <label>이 티켓에서 수행한 조치<select name="action" required defaultValue=""><option value="">선택</option>
          {options.items.map(item => <option key={item.action_id} value={item.action_id}>
            {item.event_sequence} · {item.result?.message ?? item.action_code}</option>)}
        </select></label>
        <button disabled={busy}>자료 활용 저장</button>
      </form>}
  </div>
}

function TrainingAnalysis({ base, path, session, readOnly }: { base: string; path: string; session: Session; readOnly: boolean }) {
  const [analysisId, setId] = useState(() => new URLSearchParams(window.location.search).get('analysis'))
  const [analysis, setAnalysis] = useState<Analysis>()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const active = !!analysisId && (!analysis || ['queued', 'running'].includes(analysis.status))
  const status = analysis?.run_id === analysisId ? analysis?.status : undefined
  useCompanionPart('analysis', (analysisId || pending || error) ? {
    id: analysisId ?? undefined,
    phase: error || status === 'failed' ? 'error' : status === 'succeeded' && analysis?.result ? 'ready' : 'loading',
  } : undefined)
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
  return <section id="analysis" className="surface training-analysis" tabIndex={-1}>
    <div className="training-heading"><h2>AI 분석과 권고</h2><Badge tone="info">AI 참고 자료</Badge></div>
    <p className="meta">호스트 이력과 SOP를 대조한 뒤 판단하세요. 조치 결과는 가상 조치 기록에서 확인할 수 있습니다.</p>
    {error && <p role="alert">{error}</p>}
    {!readOnly && <form onSubmit={analyze} className="row">
      <label>분석 질문<textarea name="question" rows={2} maxLength={2000} required
        placeholder="예: 메모리 오류의 가능한 원인과 다음 점검 순서를 알려줘" /></label>
      <button disabled={pending || active}>{pending ? '요청 중…' : active ? '분석 진행 중' : '분석 요청'}</button>
    </form>}
    {active && <Feedback>분석 중입니다.</Feedback>}
    {analysis?.error && <div className="notice" role="alert"><strong>AI 분석 실패</strong><p>{analysis.error.message}</p>
      <p>호스트 이력과 SOP, 가상 조치 및 인계는 계속 사용할 수 있습니다.</p></div>}
    {readOnly && !analysisId && <p>이 주소에 연결된 분석 기록이 없습니다.</p>}
    {analysis?.result && <>
      <Feedback kind="success">AI 분석이 완료되었습니다.</Feedback>
      <div className="training-workgrid training-analysis-results"><ReportView report={analysis.result} sourceHref={trainingSourceHref} /></div>
    </>}
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
      setState(previous => previous && !previous.activity.some(event => event.action_id === saved.action_id)
        ? { ...previous, activity: [...previous.activity, saved] } : previous)
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
  const latestActions = new Map(state?.activity.filter(event => event.kind === 'ACTION')
    .map(event => [event.action_code, event]))
  return <section id="actions" className="surface" tabIndex={-1}>
    <h2>가상 조치와 자유 메모</h2>
    {error && <p role="alert">{error}</p>}
    {!state && !error && <p role="status">조치 카탈로그를 불러오는 중입니다.</p>}
    {busy && <p role="status">실습 행동을 저장하고 있습니다.</p>}
    {state?.catalog.length === 0 && <p>이 시나리오 버전에는 가상 조치 카탈로그가 없습니다.</p>}
    <ul className="training-grid training-actions">{state?.catalog.map(action => {
      const completed = latestActions.get(action.action_code)
      const disabled = readOnly || busy || (Boolean(completed) && !action.repeatable) || !action.available
      return <li className="training-tile" key={action.action_code}>
        {action.requires_note && !completed && !readOnly ? <form onSubmit={event => {
          event.preventDefault()
          const note = String(new FormData(event.currentTarget).get('observation') ?? '')
          void perform(action.action_code, action.parameters, note)
        }}>
          <label>관측·인계 내용<textarea name="observation" required disabled={disabled} maxLength={4000} rows={2}
            placeholder="예: 08:12에 응답이 회복됐지만 오류 재발 여부는 확인 중" /></label>
          <button className="secondary" disabled={disabled}>{action.label}</button>
        </form> : <button type="button" className="secondary" disabled={disabled}
          onClick={() => void perform(action.action_code, action.parameters)}>{action.label}</button>}
        {completed ? <div role="status">
          <strong>{action.repeatable ? '최근 수행 결과' : '수행 완료'}</strong>
          {completed.result && <ActionResultDetails result={completed.result} />}
          {completed.note && <p className="knowledge-text">{completed.note}</p>}
        </div> : action.blocked_reason && <small>{action.blocked_reason}</small>}
      </li>
    })}</ul>
    {!readOnly && <form onSubmit={note} className="row">
      <label>자유 메모<textarea name="note" required maxLength={4000} rows={2}
        placeholder="예: BMC 진단에서 확인한 오류 코드를 티켓에 기록함" /></label>
      <button disabled={busy}>메모 저장</button>
    </form>}
    <h3>조치·메모 기록</h3>
    {state?.activity.length === 0 && <p>아직 저장된 조치나 메모가 없습니다.</p>}
    <ol>{state?.activity.map(event => <li key={event.action_id}>
      <strong>{event.kind === 'NOTE' ? '자유 메모' : state.catalog.find(action => action.action_code === event.action_code)?.label ?? event.action_code}</strong>
      {' · '}{date(event.occurred_at)} KST · 기록 {event.event_sequence}
      {event.result && <ActionResultDetails result={event.result} />}
      {event.note && <p className="knowledge-text">{event.note}</p>}
    </li>)}</ol>
  </section>
}
