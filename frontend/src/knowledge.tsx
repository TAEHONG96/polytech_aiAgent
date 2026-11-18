import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { Session } from './api'
import { ErrorNotice } from './ui'

type IndexStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
type SourceType = 'SOP' | 'TICKET'

interface Sop {
  id: string
  title: string
  filename: string
  status: IndexStatus
  size_bytes: number
  original_url: string
  error_code: string | null
  error_message: string | null
  indexed_profile: string | null
}

interface SopDetailData extends Sop {
  extracted_text: string | null
}

interface SopPage {
  items: Sop[]
  next_cursor: string | null
}

interface SearchHit {
  source_type: SourceType
  source_id: string
  title: string
  content: string
  score: number
  chunk_index: number
  page_number: number | null
  section: string | null
  ticket_version: number | null
  source_url: string
}

interface SearchResponse {
  results: SearchHit[]
  embedding_profile: string
}

const statusLabel: Record<IndexStatus, string> = {
  PENDING: '처리 대기',
  PROCESSING: '처리 중',
  COMPLETED: '완료',
  FAILED: '실패',
}

const sourceLabel: Record<SourceType, string> = { SOP: 'SOP', TICKET: '해결 티켓' }
const uploadLimit = 8 * 1024 * 1024
const pending = (document: Sop) => document.status === 'PENDING' || document.status === 'PROCESSING'
const listHasPending = (page: SopPage) => page.items.some(pending)

function usePolled<T>(path: string, shouldPoll: (value: T) => boolean) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function fetchData() {
      try {
        const result = await api<T>(path, { signal: controller.signal })
        if (controller.signal.aborted) return
        setData(result)
        setError('')
        if (shouldPoll(result)) timer = setTimeout(fetchData, 2000)
      } catch (failure) {
        if (!controller.signal.aborted) setError(errorMessage(failure))
      }
    }

    void fetchData()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [path, revision, shouldPoll])

  return { data, error, refresh: () => setRevision(value => value + 1) }
}

export function SopList({ session }: { session: Session }) {
  const cursor = new URLSearchParams(window.location.search).get('cursor')
  const { data, error } = usePolled<SopPage>(cursor ? '/sops?cursor=' + encodeURIComponent(cursor) : '/sops', listHasPending)
  const [uploadError, setUploadError] = useState('')
  const [busy, setBusy] = useState(false)

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setUploadError('')
    const body = new FormData(event.currentTarget)
    const file = body.get('file')
    if (!(file instanceof File) || !file.size || file.size >= uploadLimit) {
      setUploadError('8 MiB 미만인 PDF, TXT 또는 Markdown 파일을 선택하세요.')
      return
    }
    if (!String(body.get('title') ?? '').trim()) body.delete('title')
    setBusy(true)
    try {
      const document = await api<Sop>('/sops', { method: 'POST', body }, session.csrf_token)
      window.location.assign('/knowledge/' + document.id)
    } catch (failure) {
      setUploadError(failure instanceof ApiError && failure.status === 413
        ? '업로드 요청 크기는 최대 8 MiB입니다.'
        : errorMessage(failure))
      setBusy(false)
    }
  }

  return (
    <>
      <div className="row heading"><h1>SOP 문서</h1><a className="button secondary" href="/knowledge/search">지식 검색</a></div>
      <section className="surface narrow">
        <h2>문서 업로드</h2>
        <ErrorNotice message={uploadError} />
        <form onSubmit={upload}>
          <label>문서 제목<input name="title" maxLength={160} /></label>
          <label>파일<input name="file" type="file" accept=".pdf,.txt,.md,.markdown" required /></label>
          <p className="meta">텍스트 PDF, UTF-8 TXT·Markdown · 요청당 최대 8 MiB</p>
          <button disabled={busy}>{busy ? '업로드 중' : '업로드'}</button>
        </form>
      </section>
      <section className="surface">
        <ErrorNotice message={error} />
        {!data && !error && <p role="status">문서를 불러오는 중입니다.</p>}
        {data?.items.length === 0 && <p>등록된 문서가 없습니다.</p>}
        {!!data?.items.length && <div className="table-scroll">
          <table>
            <thead><tr><th scope="col">제목</th><th scope="col">파일</th><th scope="col">처리 상태</th></tr></thead>
            <tbody>{data.items.map(document => <tr key={document.id}>
              <td><a href={'/knowledge/' + document.id}>{document.title}</a></td>
              <td>{document.filename}</td>
              <td>{statusLabel[document.status]}{document.error_message && <div className="meta">{document.error_message}</div>}</td>
            </tr>)}</tbody>
          </table>
        </div>}
        <div className="row">
          {cursor && <a className="next" href="/knowledge">첫 페이지</a>}
          {data?.next_cursor && <a className="next" href={'/knowledge?cursor=' + data.next_cursor}>다음 페이지</a>}
        </div>
      </section>
    </>
  )
}

export function SopDetail({ id, session }: { id: string; session: Session }) {
  const path = '/sops/' + encodeURIComponent(id)
  const { data, error, refresh } = usePolled<SopDetailData>(path, pending)
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)

  async function change(action: 'delete' | 'reindex') {
    if (action === 'delete' && !window.confirm('SOP 문서와 검색 색인을 삭제하시겠습니까?')) return
    setActionError('')
    setBusy(true)
    try {
      if (action === 'delete') {
        await api(path, { method: 'DELETE' }, session.csrf_token)
        window.location.assign('/knowledge')
      } else {
        await api(path + '/reindex', { method: 'POST' }, session.csrf_token)
        refresh()
      }
    } catch (failure) {
      setActionError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return error ? <ErrorNotice message={error} /> : <p role="status">문서를 불러오는 중입니다.</p>
  return (
    <>
      <a href="/knowledge">SOP 목록</a>
      <div className="row heading"><h1>{data.title}</h1></div>
      <ErrorNotice message={error || actionError} />
      <section className="surface">
        <dl className="details">
          <div><dt>파일</dt><dd>{data.filename}</dd></div>
          <div><dt>크기</dt><dd>{data.size_bytes.toLocaleString('ko-KR')} B</dd></div>
          <div><dt>처리 상태</dt><dd role="status">{statusLabel[data.status]}</dd></div>
          {data.indexed_profile && <div><dt>임베딩 프로필</dt><dd>{data.indexed_profile}</dd></div>}
        </dl>
        {data.error_message && <ErrorNotice message={data.error_message} />}
        <div className="row knowledge-actions">
          <a className="button secondary" href={data.original_url}>원본 다운로드</a>
          {data.status === 'FAILED' && <button disabled={busy} onClick={() => void change('reindex')}>재처리</button>}
          <button className="secondary" disabled={busy} onClick={() => void change('delete')}>삭제</button>
        </div>
      </section>
      {data.extracted_text && <section className="surface"><h2>추출 텍스트</h2>
        <div className="knowledge-text">{data.extracted_text}</div>
      </section>}
    </>
  )
}

export function KnowledgeSearch({ session }: { session: Session }) {
  const [response, setResponse] = useState<SearchResponse | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const sourceTypes = form.getAll('source_type')
    setError('')
    setResponse(null)
    if (!sourceTypes.length) {
      setError('검색할 출처 유형을 한 개 이상 선택하세요.')
      return
    }
    setBusy(true)
    try {
      setResponse(await api<SearchResponse>('/knowledge/search', {
        method: 'POST',
        body: JSON.stringify({
          query: String(form.get('query') ?? '').trim(),
          source_types: sourceTypes,
          limit: Number(form.get('limit')),
        }),
      }, session.csrf_token))
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="row heading"><h1>장애 지식 검색</h1><a href="/knowledge">SOP 목록</a></div>
      <section className="surface">
        <form onSubmit={search}>
          <label>장애 상황<textarea name="query" rows={3} maxLength={1000} required placeholder="서버 부팅 실패와 메인보드 오류" /></label>
          <fieldset className="knowledge-filters">
            <legend>검색 출처</legend>
            <label><input type="checkbox" name="source_type" value="SOP" defaultChecked /> SOP</label>
            <label><input type="checkbox" name="source_type" value="TICKET" defaultChecked /> 해결 티켓</label>
          </fieldset>
          <label>결과 개수<select name="limit" defaultValue="5">{[5, 10, 20].map(limit => <option key={limit}>{limit}</option>)}</select></label>
          <button disabled={busy}>검색</button>
        </form>
      </section>
      <ErrorNotice message={error} />
      {busy && <p role="status">검색 중입니다.</p>}
      {response?.results.length === 0 && <p role="status">이 검색 조건에서 색인된 자료를 찾지 못했습니다.</p>}
      {response?.results.map(result => (
        <article className="surface" key={result.source_id}>
          <div className="row heading">
            <h2><a href={result.source_url}>{result.title}</a></h2>
            <span className="meta">코사인 유사도 {result.score.toFixed(3)}</span>
          </div>
          <p className="meta">{sourceLabel[result.source_type]}
            {result.page_number && ' · ' + result.page_number + '쪽'}
            {result.section && ' · ' + result.section}
            {result.ticket_version && ' · 티켓 버전 ' + result.ticket_version}
            {' · 청크 ' + (result.chunk_index + 1)}
          </p>
          <p className="knowledge-text">{result.content}</p>
        </article>
      ))}
    </>
  )
}
