import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Ticket, TicketPage } from './api'
import { ErrorNotice } from './ui'

export interface Shift {
  id: string
  team_id: string
  team_name: string
  starts_at: string
  ends_at: string
  timezone: string
  is_member: boolean
  is_lead: boolean
}

export interface Work {
  id: string
  title: string
  status: 'todo' | 'in_progress' | 'blocked' | 'done'
  owner_id: string | null
  owner_name?: string | null
  due_at: string | null
  note: string | null
  overdue?: boolean
}

export interface Event {
  id: string
  version: number
  type: string
  created_at: string
  details: Record<string, unknown>
}

interface Item extends Pick<Ticket, 'id' | 'number' | 'title' | 'status' | 'severity' | 'impact' | 'description' | 'next_action' | 'recheck_at' | 'resolution' | 'version'> {
  owner_name?: string | null
  warnings: string | null
  approval_required: boolean | null
  reasons: string[]
  work: Work[]
  history: Event[]
  gaps: { id: string; question: string; requested_from_name?: string | null }[]
}

type ReportStatus = 'DRAFT' | 'READY' | 'CONFIRMED' | 'ACKNOWLEDGED'
interface Selection { include_ticket_ids: string[]; exclude_ticket_ids: string[] }
interface Body {
  summary: string | null
  gap_responses: { gap_id: string; answer: string }[]
  item_notes: { ticket_id: string; summary: string | null; followup: string | null; warnings: string | null }[]
}
interface Summary {
  id: string
  status: ReportStatus
  version: number
  outgoing_shift: Shift
  incoming_shift: Shift
  created_at: string
  confirmed_at: string | null
  acknowledged_at: string | null
  supersedes_id: string | null
}
interface Preview { items: Item[]; outgoing_shift: Shift; incoming_shift: Shift }
interface SavedContent { items: Item[]; body: Body; captured_at: string; selection: Selection; status: ReportStatus }
interface Evidence {
  key: string
  text: string
  source_url: string
  title?: string
  number?: string
  ticket_id?: string
  ticket_version?: number
  event_id?: string
  work_id?: string
}
interface Draft {
  highlights: Evidence[]
  completed_work: Evidence[]
  followups: Evidence[]
  warnings: Evidence[]
  needs_review: Evidence[]
  references: Evidence[]
  possible_related_groups: Evidence[][]
  limited_coverage: boolean
}
interface Analysis {
  run_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  result: Draft | null
  error: { message: string } | null
  tool_calls: { sequence: number; name: string; status: string }[]
}
interface Report extends Summary, SavedContent {
  analysis: Analysis | null
  counts: { total: number; unresolved: number; completed_work: number }
  revisions: { version: number; kind: string; updated_at: string; updated_by_name: string }[]
  permissions: { edit: boolean; confirm: boolean; acknowledge: boolean; revise: boolean }
  superseded_by: string | null
}

const reportStatuses: Record<ReportStatus, string> = {
  DRAFT: '작성 중', READY: '인계 준비', CONFIRMED: '최종 확정', ACKNOWLEDGED: '인수 확인',
}
const reasons: Record<string, string> = {
  unresolved: '미해결', pending_work: '미완료 작업', scheduled: '재확인 예정', approval: '추가 승인',
  shift_activity: '근무 중 변경', carryover: '이월', assigned: '회차 배정', manual: '수동 포함', unassigned: '미배정',
}
export const workStatuses: Record<Work['status'], string> = {
  todo: '대기', in_progress: '진행 중', blocked: '보류', done: '완료',
}
const revisionKinds: Record<string, string> = {
  created: '초안 생성', edited: '편집 및 상태 저장', refreshed: '원본 재수집',
  confirmed: '최종 확정', acknowledged: '인수 확인',
}
const emptySelection = (): Selection => ({ include_ticket_ids: [], exclude_ticket_ids: [] })
const note = (value: FormDataEntryValue | null) => typeof value === 'string' ? value.trim() || null : null

export function displayDate(value: string, timeZone = 'Asia/Seoul') {
  return new Intl.DateTimeFormat('ko-KR', { timeZone, dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

export function shiftLabel(shift: Shift) {
  return shift.team_name + ' · ' + displayDate(shift.starts_at, shift.timezone) + ' ~ ' +
    displayDate(shift.ends_at, shift.timezone) + ' (' + shift.timezone + ')'
}

function TicketPicker({ items, selected, onChange }: {
  items: Item[]; selected: Selection; onChange: (selection: Selection) => void
}) {
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<Ticket[]>([])
  const [error, setError] = useState('')
  const [searched, setSearched] = useState(false)
  const remove = (list: string[], id: string) => list.filter(value => value !== id)
  const known = new Set(items.map(item => item.id))
  const toggle = (id: string) => onChange({
    include_ticket_ids: remove(selected.include_ticket_ids, id),
    exclude_ticket_ids: selected.exclude_ticket_ids.includes(id) ? remove(selected.exclude_ticket_ids, id) :
      [...selected.exclude_ticket_ids, id],
  })
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    try {
      const page = await api<TicketPage>('/tickets?q=' + encodeURIComponent(query.trim()) + '&limit=20')
      setFound(page.items)
      setSearched(true)
    } catch (failure) { setError(errorMessage(failure)) }
  }
  return <>
    {items.length === 0 && <p>자동으로 선정된 티켓이 없습니다.</p>}
    {items.map(item => <label className="row checkbox-line" key={item.id}>
      <input type="checkbox" checked={!selected.exclude_ticket_ids.includes(item.id)} onChange={() => toggle(item.id)} />
      <span>{item.number} · {item.title} · {item.severity}</span>
      <small className="meta">{item.reasons.map(reason => reasons[reason]).join(' · ')}</small>
    </label>)}
    {selected.exclude_ticket_ids.filter(id => !known.has(id)).map(id => <div className="row" key={id}>
      <a href={'/tickets/' + id}>제외한 티켓</a><button className="secondary" onClick={() => toggle(id)}>제외 취소</button>
    </div>)}
    {selected.include_ticket_ids.filter(id => !known.has(id)).map(id => <div className="row" key={id}>
      <a href={'/tickets/' + id}>{found.find(ticket => ticket.id === id)?.number || '수동 포함한 티켓'}</a>
      <button className="secondary" onClick={() => onChange({ ...selected, include_ticket_ids: remove(selected.include_ticket_ids, id) })}>포함 취소</button>
    </div>)}
    <ErrorNotice message={error} />
    <form onSubmit={search} className="row handover-actions">
      <label>추가할 티켓 검색<input value={query} onChange={event => setQuery(event.target.value)} maxLength={200} required /></label>
      <button className="secondary">검색</button>
    </form>
    {searched && !found.length && <p>검색 결과가 없습니다.</p>}
    {found.map(ticket => <div className="row work-line" key={ticket.id}>
      <a href={'/tickets/' + ticket.id}>{ticket.number} · {ticket.title}</a>
      <button className="secondary" disabled={selected.include_ticket_ids.includes(ticket.id) ||
        (known.has(ticket.id) && !selected.exclude_ticket_ids.includes(ticket.id))}
      onClick={() => onChange({
        include_ticket_ids: known.has(ticket.id) ? selected.include_ticket_ids : [...selected.include_ticket_ids, ticket.id],
        exclude_ticket_ids: remove(selected.exclude_ticket_ids, ticket.id),
      })}>포함</button>
    </div>)}
  </>
}

export function HandoverDashboard({ session }: { session: Session }) {
  const [shifts, setShifts] = useState<Shift[]>()
  const [shiftId, setShiftId] = useState('')
  const [summary, setSummary] = useState<{ items: Summary[]; next_before: string | null }>()
  const [preview, setPreview] = useState<Preview>()
  const [selection, setSelection] = useState<Selection>(emptySelection)
  const [error, setError] = useState('')
  const [previewError, setPreviewError] = useState('')
  const [existingId, setExistingId] = useState('')
  const [busy, setBusy] = useState(false)
  const params = new URLSearchParams(window.location.search)
  const status = params.get('status') || ''
  const before = params.get('before') || ''
  const current = shifts?.find(shift => shift.id === shiftId)
  useEffect(() => {
    let cancelled = false
    api<{ items: Shift[] }>('/shift-instances').then(({ items }) => {
      if (cancelled) return
      setShifts(items)
      setShiftId(items.find(item => item.is_member && Date.parse(item.starts_at) <= Date.now())?.id || '')
    }).catch(failure => { if (!cancelled) setError(errorMessage(failure)) })
    return () => { cancelled = true }
  }, [])
  useEffect(() => {
    const filters = new URLSearchParams()
    if (status) filters.set('status', status)
    if (before) filters.set('before', before)
    api<{ items: Summary[]; next_before: string | null }>('/handovers?' + filters)
      .then(setSummary).catch(failure => setError(errorMessage(failure)))
  }, [status, before])
  useEffect(() => {
    let cancelled = false
    if (shiftId) {
      api<Preview>('/shift-instances/' + shiftId + '/handover-preview')
        .then(data => { if (!cancelled) setPreview(data) })
        .catch(failure => { if (!cancelled) setPreviewError(errorMessage(failure)) })
    }
    return () => { cancelled = true }
  }, [shiftId])
  async function create() {
    setBusy(true)
    setError('')
    try {
      const result = await api<Report>('/handovers', {
        method: 'POST', body: JSON.stringify({ outgoing_shift_id: shiftId, ...selection }),
      }, session.csrf_token)
      window.location.assign('/handovers/' + result.id)
    } catch (failure) {
      setError(errorMessage(failure))
      if (failure instanceof ApiError && typeof failure.details?.handover_id === 'string') setExistingId(failure.details.handover_id)
      setBusy(false)
    }
  }
  return <>
    <h1>교대 인수인계</h1>
    <ErrorNotice message={error} />
    {existingId && <p><a href={'/handovers/' + existingId}>기존 보고서 열기</a></p>}
    <section className="surface">
      <h2>인수인계 대상</h2>
      <label>인계 근무 회차<select value={shiftId} disabled={busy} onChange={event => {
        setShiftId(event.target.value); setPreview(undefined); setPreviewError(''); setSelection(emptySelection())
        setExistingId(''); setError('')
      }}>
        <option value="">근무 회차 선택</option>
        {shifts?.map(shift => <option key={shift.id} value={shift.id}>{shiftLabel(shift)}</option>)}
      </select></label>
      <ErrorNotice message={previewError} />
      {!shifts && !error && <p role="status">근무 회차를 불러오는 중입니다.</p>}
      {shifts?.length === 0 && <p className="meta">등록된 근무 회차가 없습니다.</p>}
      {shiftId && !preview && !previewError && <p role="status">대상 티켓을 조회하고 있습니다.</p>}
      {preview && <>
        <p>인수조 {shiftLabel(preview.incoming_shift)}</p>
        {current?.is_member ? <>
          <TicketPicker key={shiftId} items={preview.items} selected={selection} onChange={setSelection} />
          <button className="next" disabled={busy} onClick={create}>보고서 초안 만들기</button>
        </> : <ul>{preview.items.map(item => <li key={item.id}><a href={'/tickets/' + item.id}>{item.number} · {item.title}</a></li>)}</ul>}
      </>}
    </section>
    <section className="surface">
      <h2>인수인계 목록 및 이력</h2>
      <form className="row handover-actions" action="/handovers">
        <label>보고서 상태<select name="status" defaultValue={status}>
          <option value="">전체 상태</option>
          {Object.entries(reportStatuses).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
        </select></label><button className="secondary">조회</button>
      </form>
      {summary && !summary.items.length && <p>조건에 맞는 보고서가 없습니다.</p>}
      {!!summary?.items.length && <div className="table-scroll"><table>
        <thead><tr><th>인계조 → 인수조</th><th>작성 시각 (KST)</th><th>상태</th></tr></thead>
        <tbody>{summary.items.map(report => <tr key={report.id}>
          <td><a href={'/handovers/' + report.id}>{report.outgoing_shift.team_name} → {report.incoming_shift.team_name}</a>
            <div className="meta">{shiftLabel(report.outgoing_shift)}</div></td>
          <td>{displayDate(report.created_at)}</td><td>{reportStatuses[report.status]}</td>
        </tr>)}</tbody>
      </table></div>}
      {summary?.next_before && <a className="next" href={'/handovers?' + new URLSearchParams({
        ...(status ? { status } : {}), before: summary.next_before,
      })}>다음 페이지</a>}
    </section>
  </>
}

const eventTypes: Record<string, string> = {
  created: '등록', transitioned: '상태 변경', updated: '티켓 수정', archived: '보관',
  operations_updated: '업무 정보 변경', work_created: '작업 등록', work_updated: '작업 수정',
}

export function TicketHistory({ history, ticketId, anchorPrefix = '' }: { history: Event[]; ticketId: string; anchorPrefix?: string }) {
  return <details className="handover-actions">
    <summary>작업 및 변경 이력 ({history.length})</summary>
    <ol>{history.map(event => <li id={anchorPrefix + 'event-' + event.id} key={event.id}>
      {displayDate(event.created_at)} · {eventTypes[event.type] || event.type} · 버전 {event.version}
      {typeof event.details.reason === 'string' && <p className="knowledge-text">{event.details.reason}</p>}
      {event.details.after && typeof event.details.after === 'object' && 'title' in event.details.after
        ? <p>{String(event.details.after.title)} · {workStatuses[(event.details.after as Work).status]}</p> : null}
    </li>)}</ol>
    <a href={'/tickets/' + ticketId}>원본 티켓 열기</a>
  </details>
}

function SnapshotItem({ item, anchorPrefix }: { item: Item; anchorPrefix: string }) {
  return <>
    <div className="row"><h3><a href={'/tickets/' + item.id}>{item.number}</a> · {item.title}</h3>
      <span>{item.severity} · {statuses[item.status]}</span></div>
    <p className="meta">{item.reasons.map(reason => reasons[reason]).join(' · ')} · 원본 버전 {item.version}</p>
    <dl className="details">
      <div><dt>영향 범위</dt><dd>{item.impact || '확인 필요'}</dd></div>
      <div><dt>담당자</dt><dd>{item.owner_name || '미지정'}</dd></div>
      <div><dt>완료·해결 기록</dt><dd>{item.resolution || '해결 요약 없음'}</dd></div>
      <div><dt>후속 조치</dt><dd>{item.next_action || '미기록'}{item.recheck_at && ' · 재확인 ' + displayDate(item.recheck_at)}</dd></div>
      <div><dt>주의사항</dt><dd>{item.warnings || '미기록'}</dd></div>
      <div><dt>추가 승인</dt><dd>{item.approval_required === null ? '확인 필요' : item.approval_required ? '필요' : '해당 없음'}</dd></div>
    </dl>
    {!!item.work.length && <ul>{item.work.map(work => <li id={anchorPrefix + 'work-' + work.id} key={work.id}>
      {workStatuses[work.status]} · {work.title} · {work.owner_name || '담당 미지정'}
      {work.due_at && ' · 마감 ' + displayDate(work.due_at)}{work.note && ' · ' + work.note}
      {work.overdue && ' · 마감 초과'}
    </li>)}</ul>}
    <TicketHistory history={item.history} ticketId={item.id} anchorPrefix={anchorPrefix} />
  </>
}

function SavedReport({ content, edit = false, anchorPrefix = '' }: { content: SavedContent; edit?: boolean; anchorPrefix?: string }) {
  const responses = new Map(content.body.gap_responses.map(response => [response.gap_id, response.answer]))
  const notes = new Map(content.body.item_notes.map(entry => [entry.ticket_id, entry]))
  return <>
    {edit ? <label>인계 요약<textarea name="summary" maxLength={4000} rows={4} defaultValue={content.body.summary || ''} /></label>
      : <p className="knowledge-text">{content.body.summary || '운영자 인계 요약이 비어 있습니다.'}</p>}
    {!content.items.length && <p>스냅샷에 포함된 티켓이 없습니다.</p>}
    {content.items.map(item => <article className="agent-evidence" id={anchorPrefix + 'snapshot-' + item.id} key={item.id}>
      <SnapshotItem item={item} anchorPrefix={anchorPrefix} />
      {(['summary', 'followup', 'warnings'] as const).map((field, index) => {
        const label = ['운영자 요약', '운영자 후속 조치', '운영자 주의사항'][index]
        const text = notes.get(item.id)?.[field]
        return edit ? <label key={field} className="handover-actions">{label}
          <textarea name={field + ':' + item.id} maxLength={4000} rows={2} defaultValue={text || ''} />
        </label> : text && <p key={field} className="knowledge-text"><strong>{label}</strong> {text}</p>
      })}
      {!!item.gaps.length && <div className="handover-actions">
        <h4>추가 확인 요청</h4>
        {item.gaps.map(gap => <div key={gap.id} className="work-line">
          {edit ? <label>{gap.question}{gap.requested_from_name && ' · 요청 대상 ' + gap.requested_from_name}
            <textarea name={gap.id} maxLength={4000} rows={2} defaultValue={responses.get(gap.id) || ''}
              placeholder="답변 또는 미확인 사유" />
          </label> : <><p>{gap.question}{gap.requested_from_name && ' · 요청 대상 ' + gap.requested_from_name}</p>
            <p className="knowledge-text">{responses.get(gap.id) || '답변 대기'}</p></>}
        </div>)}
      </div>}
    </article>)}
  </>
}

function EvidenceLink({ source }: { source: Evidence }) {
  const anchor = source.event_id ? 'event-' + source.event_id : source.work_id ? 'work-' + source.work_id :
    'snapshot-' + source.ticket_id
  function reveal() {
    const target = document.getElementById(anchor)
    const details = target?.closest('details')
    if (details) details.open = true
  }
  return <a href={source.ticket_id ? '#' + anchor : source.source_url} onClick={reveal}>
    {source.number || source.title || '원본'}{source.ticket_version && ' v' + source.ticket_version}
  </a>
}

function ToolTrace({ analysis }: { analysis: Analysis }) {
  return !!analysis.tool_calls.length && <details><summary>AI 도구 호출 ({analysis.tool_calls.length})</summary><ol>
    {analysis.tool_calls.map(call => <li key={call.sequence}>{call.name} · {call.status}</li>)}
  </ol></details>
}

function AiDraft({ analysis }: { analysis: Analysis }) {
  if (analysis.status === 'queued' || analysis.status === 'running') {
    return <p role="status">{analysis.status === 'queued' ? '분석 대기 중입니다.' : 'AI가 스냅샷을 분석하고 있습니다.'}</p>
  }
  if (analysis.error) return <>
    <ErrorNotice message={analysis.error.message + ' 수동으로 작성하거나 분석을 다시 요청할 수 있습니다.'} />
    <ToolTrace analysis={analysis} />
  </>
  const draft = analysis.result
  if (!draft) return null
  const sections: [string, Evidence[]][] = [
    ['주요 장애', draft.highlights], ['완료된 작업', draft.completed_work], ['후속 조치', draft.followups],
    ['주의사항', draft.warnings], ['확인 필요', draft.needs_review], ['참고 자료', draft.references],
  ]
  return <>
    {draft.limited_coverage && <p className="notice">AI가 우선순위와 실행 한도에 따라 일부 기록을 검토했습니다. 전체 기록은 아래 스냅샷에서 확인하세요.</p>}
    {!sections.some(([, sources]) => sources.length) && <p>추가로 제시할 근거가 없습니다. 스냅샷을 확인해 주세요.</p>}
    {sections.map(([label, sources]) => !!sources.length && <div key={label}>
      <h3>{label}</h3><ul>{sources.map(source => <li className="knowledge-text" key={source.key}>{source.text} · <EvidenceLink source={source} /></li>)}</ul>
    </div>)}
    {!!draft.possible_related_groups.length && <div><h3>연관 가능성 확인 필요</h3><ul>
      {draft.possible_related_groups.map((group, index) => <li className="row" key={index}>
        {group.map(source => <EvidenceLink key={source.key} source={source} />)}
      </li>)}</ul></div>}
    <ToolTrace analysis={analysis} />
  </>
}

export function HandoverDetail({ id, session }: { id: string; session: Session }) {
  const [report, setReport] = useState<Report>()
  const [selection, setSelection] = useState<Selection>(emptySelection)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [old, setOld] = useState<{ version: number; content: SavedContent }>()
  const path = '/handovers/' + id
  const active = report?.analysis?.status === 'queued' || report?.analysis?.status === 'running'
  const version = report?.version

  useEffect(() => {
    let cancelled = false
    api<Report>(path).then(value => {
      if (!cancelled) { setReport(value); setSelection(value.selection) }
    }).catch(failure => { if (!cancelled) setError(errorMessage(failure)) })
    return () => { cancelled = true }
  }, [path])
  useEffect(() => {
    if (!active) return
    let cancelled = false
    let timer: number
    async function poll() {
      try {
        const value = await api<Report>(path)
        if (!cancelled) {
          if (value.version === version) setReport(value)
          else {
            setError('다른 운영자가 보고서를 수정했습니다. 최신 보고서를 불러오세요.')
            cancelled = true
          }
        }
      } catch (failure) { if (!cancelled) setError(errorMessage(failure)) }
      finally { if (!cancelled) timer = window.setTimeout(poll, 1800) }
    }
    timer = window.setTimeout(poll, 1300)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [active, path, version])

  async function mutate(action: string, data: object = {}) {
    if (!report) return
    setBusy(true)
    setError('')
    try {
      const result = await api<Report | Analysis>(path + action, {
        method: action ? 'POST' : 'PUT', body: JSON.stringify({ expected_version: report.version, ...data }),
      }, session.csrf_token)
      if (action === '/revisions' && 'id' in result) window.location.assign('/handovers/' + result.id)
      else if ('run_id' in result) setReport({ ...report, analysis: result })
      else { setReport(result); setSelection(result.selection) }
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }
  async function revision(version: number) {
    try { setOld({ version, content: await api<SavedContent>(path + '/revisions/' + version) }) }
    catch (failure) { setError(errorMessage(failure)) }
  }
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!report) return
    const form = new FormData(event.currentTarget)
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null
    const item_notes = report.items.map(item => ({
      ticket_id: item.id, summary: note(form.get('summary:' + item.id)),
      followup: note(form.get('followup:' + item.id)), warnings: note(form.get('warnings:' + item.id)),
    })).filter(item => item.summary || item.followup || item.warnings)
    const gap_responses = report.items.flatMap(item => item.gaps.flatMap(gap => {
      const answer = note(form.get(gap.id))
      return answer ? [{ gap_id: gap.id, answer }] : []
    }))
    void mutate('', {
      status: submitter?.value === 'ready' ? 'READY' : 'DRAFT',
      body: { summary: note(form.get('summary')), item_notes, gap_responses },
      ...(submitter?.value === 'refresh' ? { refresh_snapshot: true, selection } : {}),
    })
  }
  if (!report) return <><a href="/handovers">인수인계 목록</a><ErrorNotice message={error} />
    {!error && <p role="status">보고서를 불러오는 중입니다.</p>}</>
  return <>
    <a href="/handovers">← 인수인계 목록</a>
    <div className="row heading"><h1>{report.outgoing_shift.team_name} → {report.incoming_shift.team_name}</h1>
      <span>{reportStatuses[report.status]} · 버전 {report.version}</span></div>
    <ErrorNotice message={error} />
    {error && <button className="secondary" onClick={() => window.location.reload()}>최신 보고서 불러오기</button>}
    <section className="surface">
      <h2>인계 현황</h2>
      <p>인계 {shiftLabel(report.outgoing_shift)}<br />인수 {shiftLabel(report.incoming_shift)}</p>
      <p className="meta">작성 {displayDate(report.created_at)} · 원본 수집 {displayDate(report.captured_at)} (KST)</p>
      <div className="row"><span>대상 {report.counts.total}건</span><span>미해결 {report.counts.unresolved}건</span>
        <span>완료 작업 {report.counts.completed_work}건</span>
        {report.confirmed_at && <span>확정 {displayDate(report.confirmed_at)}</span>}
        {report.acknowledged_at && <span>인수 {displayDate(report.acknowledged_at)}</span>}
      </div>
      <div className="row handover-actions">
        {report.permissions.confirm && <button disabled={busy} onClick={() => void mutate('/confirm')}>최종 확정</button>}
        {report.permissions.acknowledge && <button disabled={busy} onClick={() => void mutate('/acknowledge')}>인수 확인</button>}
        {report.permissions.revise && <button className="secondary" disabled={busy} onClick={() => void mutate('/revisions')}>새 버전 작성</button>}
        {report.supersedes_id && <a href={'/handovers/' + report.supersedes_id}>이전 확정본</a>}
        {report.superseded_by && <a href={'/handovers/' + report.superseded_by}>후속 버전</a>}
      </div>
    </section>
    {(report.analysis || report.permissions.edit) && <section className="surface">
      <h2>AI 초안</h2>
      {report.permissions.edit && report.status === 'DRAFT' &&
        <button disabled={busy || active} onClick={() => void mutate('/analyze')}>{report.analysis ? '다시 분석' : 'AI 분석 실행'}</button>}
      {report.analysis && <div className="handover-actions"><AiDraft analysis={report.analysis} /></div>}
    </section>}
    {report.permissions.edit && <section className="surface"><h2>대상 조정</h2>
      <TicketPicker items={report.items} selected={selection} onChange={setSelection} />
      <button className="secondary next" form="handover-edit" value="refresh" disabled={busy || active}>대상 적용 및 원본 재수집</button>
    </section>}
    <section className="surface">
      <h2>인계 보고서와 원본 기록</h2>
      {report.permissions.edit ? <form id="handover-edit" key={report.version} onSubmit={save}>
        <SavedReport content={report} edit />
        <div className="row">
          <button className="secondary" disabled={busy} value="draft">{report.status === 'READY' ? '초안으로 되돌려 저장' : '초안 저장'}</button>
          <button value="ready" disabled={busy || active}>인계 준비 완료</button>
        </div>
      </form> : <SavedReport content={report} />}
    </section>
    <section className="surface">
      <h2>작성 및 수정 이력</h2>
      <ol>{report.revisions.map(entry => <li className="work-line" key={entry.version}>
        <button className="secondary" onClick={() => void revision(entry.version)}>버전 {entry.version} 보기</button>
        {' '}{revisionKinds[entry.kind] || entry.kind} · {entry.updated_by_name} · {displayDate(entry.updated_at)}
      </li>)}</ol>
      {old && <div className="agent-evidence"><h3>버전 {old.version} · {reportStatuses[old.content.status]}</h3>
        <p>원본 수집 {displayDate(old.content.captured_at)}</p>
        <button className="secondary" onClick={() => setOld(undefined)}>이전 버전 닫기</button>
        <SavedReport content={old.content} anchorPrefix={'revision-' + old.version + '-'} />
      </div>}
    </section>
  </>
}
