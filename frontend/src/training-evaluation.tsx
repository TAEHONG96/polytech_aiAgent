import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Status } from './api'
import { trainingSourceHref } from './training-source'

type Warning = { code: string; ticket_id?: string }
type Draft = {
  revision: number; status: string; finalized_at?: string; warnings: Warning[]
  content: { summary: string | null; open_items: { ticket_id: string; next_action: string | null }[] }
  items: { id: string; title: string; number: string; status: Status; next_action?: string | null }[]
}
type Stage = 'NOT_STARTED' | 'PENDING' | 'PROCESSING' | 'READY' | 'FAILED'
type Summary = {
  rules_status: Stage; explanation_status: Stage; warnings: Warning[]
  evaluated_ticket_counts: { total: number; resolved: number; unresolved: number }
  rule_summary: Record<string, number> | null
}
type Entry = { ticket_id: string; title: string; final_status: Status; activity_status: string }
type Detail = Entry & {
  rule_results: { rule_code: string; result: string; evidence_refs: string[] }[] | null
  evidence: { id: string; kind: string; source_url: string }[]
  timeline: { id: string; sequence: number; occurred_at: string; type: string; details: Record<string, unknown> }[]
  warnings: string[]
  feedback: { summary: string; strengths: string[]; improvements: string[]; recommendations: string[] } | null
}
const stateLabels: Record<Stage, string> = { NOT_STARTED: '대기 전', PENDING: '대기 중', PROCESSING: '처리 중', READY: '준비됨', FAILED: '실패' }
const warningLabels: Record<string, string> = {
  MISSING_SUMMARY: '인계 요약이 비어 있습니다.', UNRESOLVED_NOT_INCLUDED: '미해결 티켓이 인계에 포함되지 않았습니다.',
  MISSING_NEXT_ACTION: '다음 조치가 비어 있습니다.', MISSING_OWNER: '티켓 담당자가 미지정입니다.',
  MISSING_RECHECK: '재확인 시각이 없습니다.', MISSING_IMPACT: '영향 범위가 비어 있습니다.',
  PENDING_WORK_NOT_INCLUDED: '미완료 작업이 있는 티켓이 인계에서 빠졌습니다.',
  WORK_OWNER_MISSING: '미완료 작업 담당자가 미지정입니다.', WORK_DUE_MISSING: '미완료 작업 마감이 없습니다.',
}
const ruleLabels: Record<string, string> = {
  UNDERSTAND_INCIDENT: '장애 이해', CHECK_HOST_HISTORY: '동일 호스트 이력 확인', USE_HOST_HISTORY: '동일 호스트 이력 활용',
  CHECK_SOP: '승인 SOP 확인', USE_SOP: '승인 SOP 활용', APPROPRIATE_VIRTUAL_ACTION: '가상 조치 타당성',
  POST_REPAIR_VALIDATION: '조치 후 재검증', DOCUMENT_ACCURACY: '검증 가능한 기록', HANDOVER_UNRESOLVED: '미해결 업무 인계',
}
const verdicts: Record<string, string> = { PASS: '충족', NOT_MET: '보완 필요', NOT_OBSERVED: '미관측', NOT_APPLICABLE: '해당 없음' }
const eventLabels: Record<string, string> = {
  scenario_seeded: '초기 자료', virtual_action_performed: '수행한 가상 조치', training_note: '자유 메모',
  learning_evidence: '학습 자료 확인·활용', work_created: '후속 작업 등록', work_updated: '후속 작업 변경',
  transitioned: '티켓 상태 변경', updated: '티켓 수정', archived: '티켓 보관',
}
const date = (value: string) => new Date(value).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' }) + ' KST'

function Warnings({ base, warnings, titles, completed = true }: {
  base: string; warnings: Warning[]; titles: Record<string, string>; completed?: boolean
}) {
  return warnings.length > 0 && <aside className="training-warning"><h3>{completed ? '확정 경고' : '확정 전 검토 · 업무상 누락'}</h3>
    {!completed && <p>보완할 수 있는 항목입니다. 이 경고가 있어도 보고서를 확정할 수 있습니다.</p>}
    <ul>{warnings.map((warning, index) => (
    <li key={index}>{warning.ticket_id && <><a href={`${base}/tickets/${warning.ticket_id}${warning.code === 'MISSING_IMPACT' ? '#ticket-summary' : ''}`}>{titles[warning.ticket_id] ?? '관련 티켓'}</a> · </>}
      {warningLabels[warning.code] ?? warning.code}</li>
  ))}</ul></aside>
}

export function TrainingHandover({ base, session, completed }: { base: string; session: Session; completed: boolean }) {
  const endpoint = base + '/outgoing-handover'
  const [draft, setDraft] = useState<Draft>()
  const [error, setError] = useState<{ title: string; message: string; code?: string }>()
  const [refresh, setRefresh] = useState(0)
  const [dirty, setDirty] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const receipt = useRef<{ revision: number; key: string } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    api<Draft>(endpoint, { signal: controller.signal }).then(result => { setDraft(result); setDirty(false); setError(undefined) })
      .catch(failure => { if (!controller.signal.aborted) showError(failure) })
    return () => controller.abort()
  }, [endpoint, refresh])

  function showError(failure: unknown) {
    const status = failure instanceof ApiError ? failure.status : undefined
    setError({ title: status === 422 ? '보고서 입력 오류' : status === 409 ? '보고서 확정 또는 수정 충돌' : '보고서 요청 실패',
      message: errorMessage(failure), code: failure instanceof ApiError ? failure.code : undefined })
  }

  async function save(extra: object) {
    if (!draft) return
    setBusy(true)
    setError(undefined)
    setNotice('')
    try {
      setDraft(await api<Draft>(endpoint, {
        method: 'PUT', body: JSON.stringify({ expected_revision: draft.revision, ...extra }),
      }, session.csrf_token))
      setDirty(false)
      setNotice('초안이 저장되었습니다.')
    } catch (failure) { showError(failure) }
    finally { setBusy(false) }
  }

  function saveNotes(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const open_items = (draft?.items ?? []).flatMap(item => {
      const next_action = String(form.get(item.id) ?? '').trim()
      return next_action ? [{ ticket_id: item.id, next_action }] : []
    })
    void save({ content: { summary: String(form.get('summary') ?? '').trim() || null, open_items } })
  }

  async function finalize(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft || dirty) return
    if (receipt.current?.revision !== draft.revision) receipt.current = { revision: draft.revision, key: crypto.randomUUID() }
    setBusy(true)
    setError(undefined)
    try {
      await api(endpoint + '/finalize', {
        method: 'POST', headers: { 'Idempotency-Key': receipt.current.key },
        body: JSON.stringify({ confirm: true, expected_revision: draft.revision }),
      }, session.csrf_token)
      window.location.hash = 'evaluation'
      window.location.reload()
    } catch (failure) { showError(failure) }
    finally { setBusy(false) }
  }

  const titles = Object.fromEntries(draft?.items.map(item => [item.id, item.title]) ?? [])
  const finalized = completed || draft?.status === 'FINALIZED'
  return <section className="surface" id="outgoing" tabIndex={-1}>
    <div className="training-heading"><h2>다음 Bot에게 인계</h2>{draft && <span className="training-badge">
      {finalized ? '확정 · 읽기 전용' : '초안'} · 리비전 {draft.revision}</span>}</div>
    {error && <div className="notice" role="alert"><strong>{error.title}</strong><p>{error.message}</p>
      {error.code === 'VERSION_CONFLICT' && <button type="button" className="secondary" onClick={() => setRefresh(value => value + 1)}>최신 보고서 열기</button>}
      {error.code === 'REPORT_SOURCE_CHANGED' && <p>인계 자료를 다시 수집하고 최신 티켓 내용을 확인하세요.</p>}
      {!draft && <button type="button" className="secondary" onClick={() => setRefresh(value => value + 1)}>다시 불러오기</button>}
    </div>}
    {!draft && !error && <p role="status">인계 보고서를 불러오는 중입니다.</p>}
    {draft && <>
      {finalized ? <>
        <p>최종 확정 · 리비전 {draft.revision}{draft.finalized_at && ' · ' + date(draft.finalized_at)}</p>
        <p className="knowledge-text">{draft.content.summary ?? '요약이 비어 있습니다.'}</p>
        {draft.items.length ? <ul className="training-list">{draft.items.map(item => <li key={item.id}>
          <div><a href={base + '/tickets/' + item.id}>{item.title}</a><p>{draft.content.open_items.find(note => note.ticket_id === item.id)?.next_action
            ?? item.next_action ?? '기록된 다음 조치가 없습니다.'}</p></div><span className="training-badge">{statuses[item.status]}</span></li>)}</ul>
          : <p>포함된 티켓이 없습니다.</p>}
        {!completed && <a href={base + '#evaluation'} onClick={event => { event.preventDefault(); window.location.hash = 'evaluation'; window.location.reload() }}>완료 결과 열기</a>}
        <a href={`${base}/outgoing-handover/revisions/${draft.revision}`}>확정 리비전 열기</a>
      </> : <>
        <p>미해결 티켓이 있어도 확정할 수 있습니다. 확정하면 실습이 종료되고 티켓, 작업, 인계 원본은 읽기 전용이 됩니다.</p>
        <button type="button" className="secondary" disabled={busy || dirty} onClick={() => void save({ refresh_snapshot: true })}>인계 자료 다시 수집</button>
        {draft.revision === 0 && <p className="meta">자료를 수집하면 티켓과 후속 작업을 확인할 수 있습니다.</p>}
        <form key={`${draft.revision}-${refresh}`} onSubmit={saveNotes} onChange={() => setDirty(true)}>
          <label>인계 요약<textarea name="summary" rows={3} maxLength={4000}
            placeholder="예: 메모리 오류는 복구했고, 스토리지 응답 지연은 계속 확인 중입니다." defaultValue={draft.content.summary ?? ''} /></label>
          {draft.items.map(item => <label key={item.id}>{item.title} · 다음 조치
            <textarea name={item.id} maxLength={4000} rows={2} placeholder="예: 응답 지연과 대기열 수치를 재확인하고 결과를 티켓에 기록"
              defaultValue={draft.content.open_items.find(note => note.ticket_id === item.id)?.next_action ?? ''} />
          </label>)}
          <button className="secondary" disabled={busy}>인계 초안 저장</button>
        </form>
        {dirty && <p role="status">저장하지 않은 초안이 있습니다. 먼저 저장하세요.</p>}
        {!dirty && notice && <p role="status">{notice}</p>}
        <Warnings base={base} warnings={draft.warnings} titles={titles} completed={false} />
        <form className="training-finalize" onSubmit={finalize}>
          <label className="training-confirm"><input type="checkbox" required /> 저장된 보고서를 최종 확정하고 실습을 종료합니다.</label>
          <button disabled={busy || dirty}>인계 최종 확정</button>
        </form>
      </>}
      {finalized && <Warnings base={base} warnings={draft.warnings} titles={titles} />}
    </>}
  </section>
}

export function TrainingEvaluation({ base }: { base: string }) {
  const endpoint = base + '/evaluation'
  const [summary, setSummary] = useState<Summary>()
  const [entries, setEntries] = useState<Entry[]>([])
  const [selected, select] = useState('')
  const [detail, setDetail] = useState<Detail>()
  const [summaryError, setSummaryError] = useState('')
  const [listError, setListError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const data = await api<Summary>(endpoint, { signal: controller.signal })
        if (controller.signal.aborted) return
        setSummaryError('')
        setSummary(data)
        if (['PENDING', 'PROCESSING'].includes(data.rules_status) || ['PENDING', 'PROCESSING'].includes(data.explanation_status)) {
          timer = setTimeout(poll, 1000)
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setSummaryError(errorMessage(failure))
          if (!(failure instanceof ApiError) || failure.status >= 500) timer = setTimeout(poll, 3000)
        }
      }
    }
    api<{ items: Entry[] }>(endpoint + '/tickets', { signal: controller.signal }).then(result => {
      setEntries(result.items)
      select(previous => result.items.some(item => item.ticket_id === previous) ? previous : result.items[0]?.ticket_id ?? '')
      setListError('')
    }).catch(failure => { if (!controller.signal.aborted) setListError(errorMessage(failure)) })
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [endpoint, retry])
  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    api<Detail>(endpoint + '/tickets/' + selected, { signal: controller.signal }).then(result => { setDetail(result); setDetailError('') })
      .catch(failure => { if (!controller.signal.aborted) setDetailError(errorMessage(failure)) })
    return () => controller.abort()
  }, [endpoint, selected, summary?.rules_status, summary?.explanation_status, retry])
  const current = detail?.ticket_id === selected ? detail : undefined
  const error = summaryError || listError || detailError
  return <section className="surface" id="evaluation" tabIndex={-1}>
    <h2>실습 평가</h2>
    {error && <div className="notice" role="alert"><p>{error}</p><button className="secondary" onClick={() => setRetry(value => value + 1)}>평가 다시 불러오기</button></div>}
    {!summary && !error && <p role="status">평가 상태를 불러오는 중입니다.</p>}
    {summary && <>
      <div className="training-stages" role="status">
        <span className="training-badge" data-state={summary.rules_status}>규칙 평가 {stateLabels[summary.rules_status]}</span>
        <span className="training-badge" data-state={summary.explanation_status}>AI 설명 {stateLabels[summary.explanation_status]}</span>
      </div>
      {summary.explanation_status === 'FAILED' && <p className="training-warning">AI 설명을 준비하지 못했습니다. 규칙 결과와 행동 근거는 아래에서 확인할 수 있습니다.</p>}
      {summary.rules_status === 'FAILED' && <p className="training-warning">규칙 평가에 실패했습니다. 확정 인계와 행동 기록은 확인할 수 있습니다.</p>}
      <dl className="training-grid training-counts">
        <div className="training-tile"><dt>평가 대상</dt><dd>{summary.evaluated_ticket_counts.total}건</dd></div>
        <div className="training-tile"><dt>해결·종료</dt><dd>{summary.evaluated_ticket_counts.resolved}건</dd></div>
        <div className="training-tile"><dt>미해결</dt><dd>{summary.evaluated_ticket_counts.unresolved}건</dd></div>
      </dl>
      {summary.rule_summary && <p>규칙 판정 · {Object.entries(summary.rule_summary).map(([key, count]) => `${verdicts[key]} ${count}`).join(' · ')}</p>}
      <Warnings base={base} warnings={summary.warnings} titles={Object.fromEntries(entries.map(entry => [entry.ticket_id, entry.title]))} />
      {summary.evaluated_ticket_counts.total === 0 && <p>이 실행에는 평가할 티켓이 없습니다.</p>}
    </>}
    <ul className="row training-picker" aria-label="평가 티켓">{entries.map(entry => <li key={entry.ticket_id}><button type="button" className="secondary"
      aria-pressed={entry.ticket_id === selected} onClick={() => { setDetailError(''); select(entry.ticket_id) }}>{entry.title}</button></li>)}</ul>
    {selected && !current && !error && <p>티켓 평가를 불러오는 중입니다.</p>}
    {current && <article className="training-ticket-evaluation">
      <h3>{current.title}</h3>
      <p>{statuses[current.final_status]} · {current.activity_status === 'ATTEMPTED' ? '대응 기록 있음' : '대응 기록 없음'}</p>
      {current.warnings.length > 0 && <ul>{current.warnings.map((warning, index) => (
        <li key={index}>{warningLabels[warning] ?? warning}</li>
      ))}</ul>}
      <h4>행동 타임라인</h4>
      {current.timeline.length === 0 && <p>저장된 행동이 없습니다.</p>}
      <ol className="training-materials">{current.timeline.map(event => <li key={event.id}>{event.sequence} · {date(event.occurred_at)} · {eventLabels[event.type] ?? event.type}
        {event.type === 'virtual_action_performed' && ' · ' + String(event.details.action_code)}
        {event.type === 'training_note' && ' · ' + String(event.details.note)}
      </li>)}</ol>
      <h4>규칙별 판정과 근거</h4>
      {current.rule_results === null ? <p>준비된 규칙 판정이 없습니다.</p> : <ul className="training-materials">{current.rule_results.map(rule => (
        <li key={rule.rule_code}>{ruleLabels[rule.rule_code]} · {verdicts[rule.result]}
          {rule.evidence_refs.map(ref => {
            const evidence = current.evidence.find(item => item.id === ref)
            const label = evidence && ({ handover: '확정 인계', event: '행동 기록', work: '후속 작업', sop: 'SOP', incident: '과거 장애', learning: '학습 자료' }[evidence.kind] ?? '근거')
            return evidence && <a key={ref} className="agent-citation" href={trainingSourceHref(evidence.source_url)}>[{label}]</a>
          })}
        </li>
      ))}</ul>}
      {current.feedback && <>
        <h4>AI 피드백</h4><p>{current.feedback.summary}</p>
        {([['잘한 점', current.feedback.strengths], ['보완할 점과 위험', current.feedback.improvements],
          ['권장 점검', current.feedback.recommendations]] as const).map(([label, points]) => points.length > 0 &&
          <section key={label}><h4>{label}</h4><ul>{points.map(point => <li key={point}>{point}</li>)}</ul></section>)}
      </>}
      {summary?.explanation_status === 'READY' && !current.feedback && <p>현재 접근할 수 있는 AI 설명이 없습니다.</p>}
    </article>}
  </section>
}
