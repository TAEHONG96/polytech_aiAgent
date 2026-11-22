import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './api'
import type { Session } from './api'
import { TrainingWorkspace } from './training-workspace'

interface Run {
  id: string
  status: 'ACTIVE' | 'COMPLETED'
  scenario: { key: string; version: string }
  virtual_now: string
}

interface Summary {
  active_run_id: string | null
  available_scenarios: { scenario_key: string; version: string }[]
}

export function TrainingPage({ path, session }: { path: string; session: Session }) {
  const route = /^\/simulation-runs\/([0-9a-f-]{36})(?:\/tickets\/([0-9a-f-]{36}))?$/i.exec(path)
  return route ? <TrainingWorkspace runId={route[1]} ticketId={route[2]} session={session} />
    : <TrainingHome session={session} />
}

function TrainingHome({ session }: { session: Session }) {
  const [summary, setSummary] = useState<Summary>()
  const [history, setHistory] = useState<Run[]>([])
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
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
  }, [])

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
    <section>
      <h1>내 실습</h1>
      {error && <p role="alert">{error}</p>}
      {!error && !summary && <p role="status">실습을 불러오는 중입니다.</p>}
      {summary && <>
        {summary.active_run_id
          ? <a href={`/simulation-runs/${summary.active_run_id}`}>진행 중인 실습 계속하기</a>
          : summary.available_scenarios.map(scenario => (
            <button key={JSON.stringify(scenario)} disabled={pending} onClick={() => void start(scenario)}>
              {scenario.scenario_key} v{scenario.version} 시작
            </button>
          ))}
        <h2>실습 기록</h2>
        {history.length === 0 ? <p>저장된 실습 기록이 없습니다.</p> : <ul>{history.map(item => (
          <li key={item.id}><a href={`/simulation-runs/${item.id}`}>{item.scenario.key} v{item.scenario.version}</a>
            {' · '}{item.status === 'ACTIVE' ? '진행 중' : '완료'}</li>
        ))}</ul>}
      </>}
    </section>
  )
}
