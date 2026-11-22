import { useEffect, useState } from 'react'
import { api, errorMessage, statuses } from './api'
import type { Status } from './api'

const runPath = /^\/simulation-runs\/[\da-f-]{36}/i
const sourcePath = /^\/(sops\/[\da-f-]{36}|tickets\/[\da-f-]{36}\/incident-sources\/[\da-f-]{36}|outgoing-handover\/revisions\/\d+)$/i

export function isTrainingSource(path: string) {
  const base = runPath.exec(path)?.[0]
  return !!base && sourcePath.test(path.slice(base.length))
}

export function trainingSourceHref(url: string) {
  const path = url.replace(/^\/api\/v1(?=\/)/, '')
  if (isTrainingSource(path)) return path
  if (/^\/simulation-runs\/[\da-f-]{36}\/tickets\/[\da-f-]{36}(\/operations)?$/i.test(path)) {
    return path.replace(/\/operations$/, '#work')
  }
  return url
}

type Source = {
  title?: string
  occurred_at?: string
  resolution?: string
  error_code?: string
  extracted_text?: string | null
  original_url?: string
  revision?: number
  content?: { summary: string | null; open_items: { ticket_id: string; next_action: string | null }[] }
  items?: { id: string; title: string; status: Status; next_action: string | null }[]
}

export function TrainingSource({ path }: { path: string }) {
  const base = runPath.exec(path)![0]
  const [source, setSource] = useState<Source>()
  const [error, setError] = useState('')
  const section = path.includes('/incident-sources/') ? '호스트 과거 장애' : path.includes('/sops/') ? '승인 SOP' : '확정 인계 리비전'
  useEffect(() => {
    const controller = new AbortController()
    api<Source>(path, { signal: controller.signal }).then(setSource)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path])
  return <>
    <nav className="training-nav" aria-label="근거 탐색"><a href={base}>근무·티켓 목록</a><a href={base + '#evaluation'}>실습 결과</a></nav>
    <article className="surface">
      <p className="training-context">{section}</p>
      <h1>{source?.title ?? (source?.revision ? `인계 리비전 ${source.revision}` : '실습 근거')}</h1>
      {error && <p className="notice" role="alert">{error}</p>}
      {!source && !error && <p role="status">근거를 확인하는 중입니다.</p>}
      {source?.occurred_at && <p>발생 {new Date(source.occurred_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })} KST {source.error_code && `· ${source.error_code}`}</p>}
      {source?.resolution && <p className="knowledge-text">{source.resolution}</p>}
      {source?.original_url && <p><a href={source.original_url}>SOP 원본 열기</a></p>}
      {source?.extracted_text && <div className="knowledge-text">{source.extracted_text}</div>}
      {source?.content && <>
        <h2>인계 요약</h2><p className="knowledge-text">{source.content.summary ?? '기록된 요약이 없습니다.'}</p>
        <h2>포함된 티켓</h2>
        {source.items?.length ? <ul className="training-list">{source.items.map(item => <li key={item.id}>
          <div><a href={`${base}/tickets/${item.id}`}>{item.title}</a><p>{source.content?.open_items.find(note => note.ticket_id === item.id)?.next_action
            ?? item.next_action ?? '기록된 다음 조치가 없습니다.'}</p></div><span className="training-badge">{statuses[item.status]}</span></li>)}</ul>
          : <p>포함된 티켓이 없습니다.</p>}
      </>}
    </article>
  </>
}
