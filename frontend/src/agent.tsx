import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { Session, Ticket, TicketPage } from './api'
import { ErrorNotice } from './ui'

type RunState = 'queued' | 'running' | 'succeeded' | 'failed'
interface Statement { text: string; citation_keys: string[]; basis_status: 'supported' | 'insufficient' }
interface Evidence {
  key: string
  kind: string
  title: string
  content: string
  source_url: string
  score: number | null
  chunk_index: number | null
  page_number: number | null
  section: string | null
  ticket_version: number | null
}
interface Report {
  summary: string
  confirmed_facts: Statement[]
  hypotheses: Statement[]
  diagnostic_steps: Statement[]
  additional_information: string[]
  limitations: string[]
  evidence: Evidence[]
}
interface Trace {
  sequence: number
  name: string
  status: string
  elapsed_ms: number
  details: Record<string, unknown>
}
export interface Run {
  run_id: string
  status: RunState
  message: string
  ticket_id: string | null
  provider: string | null
  model: string | null
  result: Report | null
  error: { message: string; code: string; retryable: boolean } | null
  usage: { model_requests: number; embedding_requests: number; total_tokens: number } | null
  tool_limit: number
  tool_calls: Trace[]
}

const statusText: Record<RunState, string> = { queued: '대기 중', running: '조사 중', succeeded: '완료', failed: '실패' }
const evidenceLabel: Record<string, string> = {
  SOP: 'SOP', SIMILAR_TICKET: '과거 해결 티켓', TICKET_DETAILS: '티켓 상세',
  TICKET_HISTORY: '티켓 이력', OPEN_TICKET: '미해결 티켓',
  HISTORICAL_INCIDENT: '승인된 합성 과거 사고',
}
const isActive = (state?: RunState) => state === 'queued' || state === 'running'

function rememberRun(runId: string) {
  const url = new URL(window.location.href)
  url.searchParams.set('run', runId)
  window.history.replaceState(null, '', url)
}

function Statements({ title, items }: { title: string; items: Statement[] }) {
  if (!items.length) return null
  return <section>
    <h2>{title}</h2>
    <ol>{items.map((item, i) => <li key={i}>
      {item.text}
      {item.citation_keys.map(key => <a className="agent-citation" href={'#evidence-' + key} key={key}>[{key}]</a>)}
      {item.basis_status === 'insufficient' && <span className="meta"> · 추가 확인 필요</span>}
    </li>)}</ol>
  </section>
}

export function ReportView({ report, sourceHref = (url: string) => url }: { report: Report; sourceHref?: (url: string) => string }) {
  return <>
    <section className="surface">
      <h2>분석 요약</h2><p>{report.summary}</p>
      <Statements title="확인된 사실" items={report.confirmed_facts} />
      <Statements title="가능한 원인" items={report.hypotheses} />
      <Statements title="권장 진단 절차" items={report.diagnostic_steps} />
      {!!report.additional_information.length && <section><h2>추가 확인 정보</h2>
        <ul>{report.additional_information.map((item, i) => <li key={i}>{item}</li>)}</ul>
      </section>}
      {!!report.limitations.length && <section><h2>불확실성과 제한 사항</h2>
        <ul>{report.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
      </section>}
    </section>
    {!!report.evidence.length && <section className="surface">
      <h2>참고 출처</h2>
      {report.evidence.map(source => <article className="agent-evidence" key={source.key} id={'evidence-' + source.key}>
        <h3>[{source.key}] <a href={sourceHref(source.source_url)}>{source.title}</a></h3>
        <p className="meta">{evidenceLabel[source.kind] ?? source.kind}
          {source.page_number !== null && ' · ' + source.page_number + '쪽'}
          {source.section && ' · ' + source.section}
          {source.chunk_index !== null && ' · 청크 ' + (source.chunk_index + 1)}
          {source.ticket_version !== null && ' · 버전 ' + source.ticket_version}
          {source.score !== null && ' · 유사도 ' + source.score.toFixed(3)}
        </p>
        <p className="knowledge-text">{source.content}</p>
      </article>)}
    </section>}
  </>
}

export function AgentPage({ session }: { session: Session }) {
  const params = new URLSearchParams(window.location.search)
  const [runId, setRunId] = useState(params.get('run') ?? '')
  const [ticket, setTicket] = useState(params.get('ticket') ?? '')
  const [ticketOptions, setTicketOptions] = useState<Ticket[]>([])
  const [ticketQuery, setTicketQuery] = useState('')
  const [message, setMessage] = useState('')
  const [run, setRun] = useState<Run | null>(null)
  const [notify, setNotify] = useState('')
  const [busy, setBusy] = useState(false)
  const active = !!runId && (!run || isActive(run.status))

  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      const query = new URLSearchParams({ limit: '30' })
      if (ticketQuery.trim()) query.set('q', ticketQuery.trim())
      api<TicketPage>('/tickets?' + query, { signal: controller.signal })
        .then(page => setTicketOptions(page.items))
        .catch(() => { if (!controller.signal.aborted) setTicketOptions([]) })
    }, 200)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [ticketQuery])

  useEffect(() => {
    if (!runId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const next = await api<Run>('/agent/runs/' + encodeURIComponent(runId), { signal: controller.signal })
        if (controller.signal.aborted) return
        setRun(next)
        if (isActive(next.status)) timer = setTimeout(poll, 1200)
      } catch (failure) {
        if (!controller.signal.aborted) {
          setNotify(errorMessage(failure))
          if (failure instanceof ApiError && [401, 403, 404, 422].includes(failure.status)) setRunId('')
          else timer = setTimeout(poll, 3000)
        }
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [runId])

  async function start(question: string, ticketId: string | null) {
    setNotify('')
    setBusy(true)
    try {
      const accepted = await api<Run>('/agent/analyze', {
        method: 'POST', body: JSON.stringify({ message: question, ticket_id: ticketId }),
      }, session.csrf_token)
      setRun(accepted)
      setRunId(accepted.run_id)
      rememberRun(accepted.run_id)
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 && typeof failure.details?.run_id === 'string') {
        setRun(null)
        setRunId(failure.details.run_id)
        rememberRun(failure.details.run_id)
      } else setNotify(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const question = message.trim()
    if (question) void start(question, ticket || null)
  }

  return <>
    <h1>AI 장애 분석</h1>
    <ErrorNotice message={notify} />
    <section className="surface">
      <form onSubmit={submit}>
        <label>분석할 장애<textarea name="message" rows={4} maxLength={2000} required
          value={message} onChange={e => setMessage(e.target.value)}
          placeholder="예: 서버가 POST 진단에서 멈추는 원인을 분석해줘" /></label>
        <div className="fields">
          <label>티켓 검색<input value={ticketQuery} onChange={e => setTicketQuery(e.target.value)} placeholder="예: INC-000123 또는 메모리 경보" /></label>
          <label>연결할 티켓<select value={ticket} onChange={e => setTicket(e.target.value)}>
            <option value="">선택 안 함</option>
            {ticket && !ticketOptions.some(item => item.id === ticket) && <option value={ticket}>선택한 티켓 ({ticket})</option>}
            {ticketOptions.map(item => <option key={item.id} value={item.id}>{item.number} · {item.title}</option>)}
          </select></label>
        </div>
        <button disabled={busy || active}>분석 요청</button>
      </form>
    </section>
    {run && <>
      <section className="surface">
        <div className="row heading"><h2>분석 요청</h2><span role="status">{statusText[run.status]} · 도구 {run.tool_calls.length}/{run.tool_limit}</span></div>
        <p className="knowledge-text">{run.message}</p>
        {run.model && <p className="meta">{run.provider} · {run.model}</p>}
        {run.error && <><ErrorNotice message={run.error.message} />
          {run.error.retryable && <button disabled={busy} onClick={() => void start(run.message, run.ticket_id)}>다시 시도</button>}
        </>}
      </section>
      {run.result && <ReportView report={run.result} />}
      <details className="surface">
        <summary>개발자 패널 · 도구 호출 기록 ({run.tool_calls.length})</summary>
        {run.usage && <p className="meta">모델 요청 {run.usage.model_requests}회 · 임베딩 요청 {run.usage.embedding_requests}회 · 모델 토큰 {run.usage.total_tokens}</p>}
        <ol>{run.tool_calls.map(call => <li key={call.sequence}>
          <strong>{call.name}</strong> · {call.status} · {call.elapsed_ms}ms
          <pre className="agent-trace">{JSON.stringify(call.details, null, 2)}</pre>
        </li>)}</ol>
      </details>
    </>}
  </>
}
