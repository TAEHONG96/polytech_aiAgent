import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './api'
import type { Session } from './api'
import { TrainingWorkspace } from './training-workspace'
import { TrainingSource, isTrainingSource } from './training-source'
import { TrainingSopList } from './training-sops'

interface Run {
  id: string
  status: 'ACTIVE' | 'COMPLETED'
  scenario: { key: string; version: string }
  virtual_now: string
  started_at: string
  completed_at: string | null
}

interface Summary {
  active_run_id: string | null
  available_scenarios: { scenario_key: string; version: string; title?: string }[]
}

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
    return () => { live = false }
  }, [revision])

  async function start(scenario: Summary['available_scenarios'][number]) {
    const selection = JSON.stringify(scenario)
    if (startRequest.current?.scenario !== selection) {
      startRequest.current = { scenario: selection, key: crypto.randomUUID() }
    }
    setError('')
    setPending(true)
    try {
      const created = await api<Run>('/simulation-runs', {
        method: 'POST', headers: { 'Idempotency-Key': startRequest.current.key },
        body: JSON.stringify({ scenario_key: scenario.scenario_key, scenario_version: scenario.version }),
      }, session.csrf_token)
      window.location.assign(`/simulation-runs/${created.id}`)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setPending(false)
    }
  }

  return (
    <section aria-labelledby="training-home">
      <div className="training-hero">
        <div><p className="training-context">데이터센터 · 교대 근무</p><h1 id="training-home">내 실습</h1>
          <p>장애를 조사하고 가상 조치를 기록한 뒤 다음 근무조에 인계하세요.</p></div>
        {summary?.active_run_id && <a className="button" href={`/simulation-runs/${summary.active_run_id}`}>진행 중인 실습 계속하기</a>}
      </div>
      {error && <div className="notice" role="alert">{error} {!summary && <button type="button" className="secondary" onClick={() => {
        setError(''); setRevision(value => value + 1)
      }}>다시 불러오기</button>}</div>}
      {!error && !summary && <p role="status">실습을 불러오는 중입니다.</p>}
      {summary && <>
        {!summary.active_run_id && <section className="surface">
          <h2>{history.length ? '새 실습 또는 재실습' : '새 실습'}</h2>
          {summary.available_scenarios.length ? <div className="training-grid">{summary.available_scenarios.map(scenario => (
            <article className="training-tile" key={`${scenario.scenario_key}-${scenario.version}`}>
              <h3>{scenario.title ?? scenario.scenario_key}</h3>
              <p className="meta">{scenario.scenario_key} · v{scenario.version}</p>
              <button disabled={pending} onClick={() => void start(scenario)}>{scenario.scenario_key} v{scenario.version} 시작</button>
            </article>
          ))}</div> : <p>현재 시작할 수 있는 시나리오가 없습니다.</p>}
          {pending && <p role="status">새 실습을 준비하고 있습니다.</p>}
        </section>}
        <section className="surface"><h2>실습 기록</h2>
          {history.length === 0 ? <p>저장된 실습 기록이 없습니다.</p> : <ul className="training-list">{history.map(item => (
            <li key={item.id}><div><strong>{item.scenario.key} v{item.scenario.version}</strong>
              <p className="meta">{new Date(item.completed_at ?? item.started_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })} KST</p></div>
              <a href={`/simulation-runs/${item.id}${item.status === 'COMPLETED' ? '#evaluation' : ''}`}>
                {item.status === 'ACTIVE' ? '진행 중인 근무 열기' : '완료 결과 보기'}</a></li>
          ))}</ul>}
        </section>
      </>}
    </section>
  )
}
