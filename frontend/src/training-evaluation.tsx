import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Status } from './api'

type Warning = { code: string; ticket_id?: string }
type Draft = {
  revision: number; status: string; finalized_at?: string; warnings: Warning[]
  content: { summary: string | null; open_items: { ticket_id: string; next_action: string | null }[] }
  items: { id: string; title: string; number: string; status: Status }[]
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

function Warnings({ warnings, titles }: { warnings: Warning[]; titles: Record<string, string> }) {
  return warnings.length > 0 && <><h3>확정 경고</h3><ul>{warnings.map((warning, index) => (
    <li key={index}>{warning.ticket_id && titles[warning.ticket_id] && titles[warning.ticket_id] + ' · '}
      {warningLabels[warning.code] ?? warning.code}</li>
  ))}</ul></>
}

export function TrainingHandover({ base, session, completed }: { base: string; session: Session; completed: boolean }) {
  const endpoint = base + '/outgoing-handover'
  const [draft, setDraft] = useState<Draft>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const receipt = useRef<{ revision: number; key: string } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    api<Draft>(endpoint, { signal: controller.signal }).then(setDraft)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [endpoint])

  async function save(extra: object) {
    if (!draft) return
    setBusy(true)
    setError('')
    try {
      setDraft(await api<Draft>(endpoint, {
        method: 'PUT', body: JSON.stringify({ expected_revision: draft.revision, ...extra }),
      }, session.csrf_token))
    } catch (failure) { setError(errorMessage(failure)) }
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
    if (!draft) return
    if (receipt.current?.revision !== draft.revision) receipt.current = { revision: draft.revision, key: crypto.randomUUID() }
    setBusy(true)
    setError('')
    try {
      await api(endpoint + '/finalize', {
        method: 'POST', headers: { 'Idempotency-Key': receipt.current.key },
        body: JSON.stringify({ confirm: true, expected_revision: draft.revision }),
      }, session.csrf_token)
      window.location.hash = 'evaluation'
      window.location.reload()
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }

  const titles = Object.fromEntries(draft?.items.map(item => [item.id, item.title]) ?? [])
  return <section className="surface" id="outgoing">
    <h2>다음 Bot에게 인계</h2>
    {error && <p role="alert">{error}</p>}
    {!draft && !error && <p role="status">인계 보고서를 불러오는 중입니다.</p>}
    {draft && <>
      {completed ? <>
        <p>최종 확정 · 리비전 {draft.revision}{draft.finalized_at && ' · ' + date(draft.finalized_at)}</p>
        <p className="knowledge-text">{draft.content.summary ?? '요약이 비어 있습니다.'}</p>
        <ul>{draft.content.open_items.map(item => <li key={item.ticket_id}>
          <a href={base + '/tickets/' + item.ticket_id}>{titles[item.ticket_id] ?? '티켓'}</a> · {item.next_action}</li>)}</ul>
      </> : <>
        <button type="button" className="secondary" disabled={busy} onClick={() => void save({ refresh_snapshot: true })}>인계 자료 다시 수집</button>
        <form key={draft.revision} onSubmit={saveNotes}>
          <label>인계 요약<textarea name="summary" rows={3} maxLength={4000} defaultValue={draft.content.summary ?? ''} /></label>
          {draft.items.map(item => <label key={item.id}>{item.title} · 다음 조치
            <textarea name={item.id} maxLength={4000} rows={2}
              defaultValue={draft.content.open_items.find(note => note.ticket_id === item.id)?.next_action ?? ''} />
          </label>)}
          <button className="secondary" disabled={busy}>인계 초안 저장</button>
        </form>
        <form onSubmit={finalize}>
          <label><input type="checkbox" required /> 저장된 보고서를 최종 확정하고 실습을 종료합니다.</label>
          <button disabled={busy}>인계 최종 확정</button>
        </form>
      </>}
      <Warnings warnings={draft.warnings} titles={titles} />
    </>}
  </section>
}

export function TrainingEvaluation({ base }: { base: string }) {
  const endpoint = base + '/evaluation'
  const [summary, setSummary] = useState<Summary>()
  const [entries, setEntries] = useState<Entry[]>([])
  const [selected, select] = useState('')
  const [detail, setDetail] = useState<Detail>()
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const data = await api<Summary>(endpoint, { signal: controller.signal })
        if (controller.signal.aborted) return
        setSummary(data)
        if (['PENDING', 'PROCESSING'].includes(data.rules_status) || ['PENDING', 'PROCESSING'].includes(data.explanation_status)) {
          timer = setTimeout(poll, 1000)
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(errorMessage(failure))
          if (!(failure instanceof ApiError) || failure.status >= 500) timer = setTimeout(poll, 3000)
        }
      }
    }
    api<{ items: Entry[] }>(endpoint + '/tickets', { signal: controller.signal }).then(result => {
      setEntries(result.items)
      select(result.items[0]?.ticket_id ?? '')
    }).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [endpoint])
  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    api<Detail>(endpoint + '/tickets/' + selected, { signal: controller.signal }).then(setDetail)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [endpoint, selected, summary?.rules_status, summary?.explanation_status])
  const current = detail?.ticket_id === selected ? detail : undefined
  return <section className="surface" id="evaluation">
    <h2>실습 평가</h2>
    {error && <p role="alert">{error}</p>}
    {summary && <>
      <p role="status">규칙 평가 {stateLabels[summary.rules_status]} · AI 설명 {stateLabels[summary.explanation_status]}</p>
      <p>평가 대상 {summary.evaluated_ticket_counts.total}건 · 해결·종료 {summary.evaluated_ticket_counts.resolved}건 · 미해결 {summary.evaluated_ticket_counts.unresolved}건</p>
      {summary.rule_summary && <p>규칙 판정 · {Object.entries(summary.rule_summary).map(([key, count]) => `${verdicts[key]} ${count}`).join(' · ')}</p>}
      <Warnings warnings={summary.warnings} titles={Object.fromEntries(entries.map(entry => [entry.ticket_id, entry.title]))} />
    </>}
    <ul>{entries.map(entry => <li key={entry.ticket_id}><button type="button" className="secondary" onClick={() => select(entry.ticket_id)}>{entry.title}</button></li>)}</ul>
    {current && <>
      <h3>{current.title}</h3>
      <p>{statuses[current.final_status]} · {current.activity_status === 'ATTEMPTED' ? '대응 기록 있음' : '대응 기록 없음'}</p>
      {current.warnings.length > 0 && <ul>{current.warnings.map((warning, index) => (
        <li key={index}>{warningLabels[warning] ?? warning}</li>
      ))}</ul>}
      <h4>행동 타임라인</h4>
      <ol>{current.timeline.map(event => <li key={event.id}>{event.sequence} · {date(event.occurred_at)} · {eventLabels[event.type] ?? event.type}
        {event.type === 'virtual_action_performed' && ' · ' + String(event.details.action_code)}
        {event.type === 'training_note' && ' · ' + String(event.details.note)}
      </li>)}</ol>
      <h4>규칙별 판정과 근거</h4>
      {current.rule_results === null ? <p>준비된 규칙 판정이 없습니다.</p> : <ul>{current.rule_results.map(rule => (
        <li key={rule.rule_code}>{ruleLabels[rule.rule_code]} · {verdicts[rule.result]}
          {rule.evidence_refs.map(ref => {
            const evidence = current.evidence.find(item => item.id === ref)
            return evidence && <a key={ref} className="agent-citation" href={evidence.source_url}>[{evidence.kind === 'handover' ? '확정 인계' : '근거'}]</a>
          })}
        </li>
      ))}</ul>}
      {current.feedback && <>
        <h4>AI 피드백</h4><p>{current.feedback.summary}</p>
        {([['잘한 점', current.feedback.strengths], ['보완할 점과 위험', current.feedback.improvements],
          ['권장 점검', current.feedback.recommendations]] as const).map(([label, points]) => points.length > 0 &&
          <section key={label}><h4>{label}</h4><ul>{points.map(point => <li key={point}>{point}</li>)}</ul></section>)}
      </>}
    </>}
  </section>
}
