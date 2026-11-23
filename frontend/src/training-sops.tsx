import { useEffect, useState } from 'react'
import { api, errorMessage } from './api'

const statusLabels = { PENDING: '색인 대기', PROCESSING: '색인 중', COMPLETED: '검색 가능', FAILED: '색인 실패' }

type SopPage = {
  items: { id: string; title: string; filename: string; original_url: string; status: keyof typeof statusLabels }[]
  next_cursor: string | null
}

export function TrainingSopList({ path }: { path: string }) {
  const base = path.slice(0, -'/sops'.length)
  const cursor = new URLSearchParams(window.location.search).get('cursor')
  const [page, setPage] = useState<SopPage>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const endpoint = cursor ? path + '?cursor=' + encodeURIComponent(cursor) : path
    api<SopPage>(endpoint, { signal: controller.signal }).then(setPage)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path, cursor, retry])

  return <>
    <nav className="training-nav" aria-label="실습 탐색">
      <a href={base}>근무·티켓 목록</a>
      <a href={base + '#knowledge'}>SOP·유사 장애 검색</a>
    </nav>
    <section className="surface">
      <h1>승인 SOP 목록</h1>
      {error && <div className="notice" role="alert">{error}
        <button type="button" className="secondary" onClick={() => {
          setError('')
          setRetry(value => value + 1)
        }}>다시 불러오기</button>
      </div>}
      {!page && !error && <p role="status">승인 SOP를 불러오는 중입니다.</p>}
      {page && (page.items.length ? <ul className="training-list">{page.items.map(sop => (
        <li key={sop.id}>
          <div><a href={path + '/' + sop.id}>{sop.title}</a><p className="meta">{sop.filename}</p></div>
          <div className="row"><span className="training-badge">{statusLabels[sop.status]}</span><a href={sop.original_url}>원본 열기</a></div>
        </li>
      ))}</ul> : <p>이 페이지에 표시할 승인 SOP가 없습니다.</p>)}
      {(cursor || page?.next_cursor) && <nav className="training-nav" aria-label="SOP 목록 페이지">
        {cursor && <a href={path}>첫 페이지</a>}
        {page?.next_cursor && <a href={path + '?cursor=' + encodeURIComponent(page.next_cursor)}>다음 페이지</a>}
      </nav>}
    </section>
  </>
}
