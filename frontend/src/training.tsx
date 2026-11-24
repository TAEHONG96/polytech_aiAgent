import { useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { Session, SimulationStatus } from './api'
import { TrainingWorkspace } from './training-workspace'
import { TrainingSource, isTrainingSource } from './training-source'
import { TrainingSopList } from './training-sops'
import { Badge, Feedback } from './ui'

interface Run {
  id: string
  status: SimulationStatus
  scenario: { key: string; version: string }
  virtual_now: string
  started_at: string
  completed_at: string | null
}

interface Summary {
  active_run_id: string | null
  available_scenarios: { scenario_key: string; version: string; title?: string }[]
}

const runLabel: Record<SimulationStatus, string> = { ACTIVE: '진행 중', PAUSED: '일시정지', COMPLETED: '완료' }
const runLink: Record<SimulationStatus, string> = { ACTIVE: '진행 중인 근무 열기', PAUSED: '저장된 기록 보기', COMPLETED: '완료 결과 보기' }

export function TrainingPage({ path, session }: { path: string; session: Session }) {
  if (/^\/simulation-runs\/[\da-f-]{36}\/sops$/i.test(path)) return <TrainingSopList path={path} />
  if (isTrainingSource(path)) return <TrainingSource path={path} />
  const route = /^\/simulation-runs\/([0-9a-f-]{36})(?:\/tickets\/([0-9a-f-]{36}))?$/i.exec(path)
  return route ? <TrainingWorkspace runId={route[1]} ticketId={route[2]} session={session} />
    : <TrainingHome session={session} />
}

function TrainingHome({ session }: { session: Session }) {
  const [summary, setSummary] = useState<Summary>()
  const [history, setHistory] = useState<Run[]>([])
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [revision, setRevision] = useState(0)
  const startRequest = useRef<{ scenario: string; key: string } | null>(null)

  useEffect(() => {
    let live = true
    async function load() {
      try {
        const [home, runs] = await Promise.all([
          api<Summary>('/simulation'), api<{ items: Run[] }>('/simulation-runs'),
        ])
        if (live) { setSummary(home); setHistory(runs.items) }
      } catch (failure) {
        if (live) setError(errorMessage(failure))
      }
    }
    void load()
    function refreshRestoredPage(event: PageTransitionEvent) {
      if (event.persisted) void load()
    }
    window.addEventListener('pageshow', refreshRestoredPage)
    return () => { live = false; window.removeEventListener('pageshow', refreshRestoredPage) }
  }, [revision])

  async function start(scenario: Summary['available_scenarios'][number]) {
    const selection = JSON.stringify({ scenario_key: scenario.scenario_key, scenario_version: scenario.version,
      pause_active_run_id: summary?.active_run_id ?? null })
    if (startRequest.current?.scenario !== selection) {
      startRequest.current = { scenario: selection, key: crypto.randomUUID() }
    }
    await switchTo('/simulation-runs', selection, startRequest.current.key)
  }

  async function switchTo(path: string, body: string, key?: string) {
    setError('')
    setPending(true)
    try {
      const selected = await api<Run>(path, { method: 'POST', headers: key ? { 'Idempotency-Key': key } : {}, body }, session.csrf_token)
      window.location.assign(`/simulation-runs/${selected.id}`)
    } catch (failure) {
      setError(errorMessage(failure))
      if (failure instanceof ApiError && failure.status === 409) setRevision(value => value + 1)
    } finally {
      setPending(false)
    }
  }

  return (
    <section aria-labelledby="training-home" data-companion-destination={summary ? 'practice-home' : undefined}>
      <div className="training-hero">
        <div><p className="training-context">데이터센터 · 교대 근무</p><h1 id="training-home">내 실습</h1>
          <p>장애를 조사하고 가상 조치를 기록한 뒤 다음 근무조에 인계하세요.</p></div>
        {summary?.active_run_id && <a className="button" href={`/simulation-runs/${summary.active_run_id}`}>진행 중인 실습 계속하기</a>}
      </div>
      {error && <div className="notice" role="alert">{error} {!summary && <button type="button" className="secondary" onClick={() => {
        setError(''); setRevision(value => value + 1)
      }}>다시 불러오기</button>}</div>}
      {!error && !summary && <Feedback>실습을 불러오는 중입니다.</Feedback>}
      {summary && <>
        <section className="surface">
          <h2>{history.length ? '새 실습 또는 재실습' : '새 실습'}</h2>
          {summary.active_run_id && <p>새 실습을 시작하면 현재 회차가 일시정지됩니다. 실습 기록에서 나중에 이어서 진행할 수 있습니다.</p>}
          {summary.available_scenarios.length ? <div className="training-grid">{summary.available_scenarios.map(scenario => (
            <article className="training-tile" key={`${scenario.scenario_key}-${scenario.version}`}>
              <h3>{scenario.title ?? scenario.scenario_key}</h3>
              <p className="meta">{scenario.scenario_key} · v{scenario.version}</p>
              <button disabled={pending} onClick={() => void start(scenario)}>{scenario.scenario_key} v{scenario.version} 시작</button>
            </article>
          ))}</div> : <p>현재 시작할 수 있는 시나리오가 없습니다.</p>}
          {pending && <p role="status">선택한 실습을 여는 중입니다.</p>}
        </section>
        <section className="surface"><h2>실습 기록</h2>
          {history.length === 0 ? <Feedback kind="empty">저장된 실습 기록이 없습니다.</Feedback> : <ul className="training-list">{history.map(item => (
            <li key={item.id}><div><strong>{item.scenario.key} v{item.scenario.version}</strong>
              <p className="meta">{new Date(item.completed_at ?? item.started_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })} KST</p></div>
              <div className="row"><Badge tone={item.status === 'ACTIVE' ? 'info' : item.status === 'COMPLETED' ? 'success' : 'warning'}>{runLabel[item.status]}</Badge>
                {item.status === 'PAUSED' && <button type="button" className="secondary" disabled={pending} onClick={() => void switchTo(
                  `/simulation-runs/${item.id}/resume`, JSON.stringify({ pause_active_run_id: summary.active_run_id }),
                )}>이어서 진행</button>}
                <a href={`/simulation-runs/${item.id}${item.status === 'COMPLETED' ? '#evaluation' : ''}`}>
                  {runLink[item.status]}</a></div></li>
          ))}</ul>}
        </section>
      </>}
    </section>
  )
}
