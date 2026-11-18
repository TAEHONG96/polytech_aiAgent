import { StrictMode, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Severity, Status, Ticket, TicketPage } from './api'
import { KnowledgeSearch, SopDetail, SopList } from './knowledge'
import { ErrorNotice } from './ui'
import './style.css'

const priorities: Severity[] = ['P1', 'P2', 'P3', 'P4']
const koreanDate = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', dateStyle: 'medium', timeStyle: 'short',
})
const inputDate = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

function dateInput(value: string) {
  return inputDate.format(new Date(value)).replace(' ', 'T')
}

function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const form = Object.fromEntries(new FormData(event.currentTarget))
    try {
      onLogin(await api<Session>('/auth/login', { method: 'POST', body: JSON.stringify(form) }))
      if (window.location.pathname === '/login') window.location.assign('/tickets')
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="surface narrow">
      <h1>운영자 로그인</h1>
      <ErrorNotice message={error} />
      <form onSubmit={submit}>
        <label>사용자명<input name="username" autoComplete="username" maxLength={64} required /></label>
        <label>비밀번호<input name="password" type="password" autoComplete="current-password" required /></label>
        <button disabled={busy}>로그인</button>
      </form>
    </section>
  )
}

function TicketList() {
  const [page, setPage] = useState<TicketPage | null>(null)
  const [error, setError] = useState('')
  const search = new URLSearchParams(window.location.search)

  useEffect(() => {
    const query = new URLSearchParams(window.location.search)
    for (const [key, value] of Array.from(query)) {
      if (!value) query.delete(key)
    }
    api<TicketPage>('/tickets?' + query).then(setPage).catch(failure => setError(errorMessage(failure)))
  }, [])

  if (page?.next_cursor) search.set('cursor', page.next_cursor)

  return (
    <>
      <div className="row heading"><h1>장애 티켓</h1><a className="button" href="/tickets/new">새 티켓</a></div>
      <section className="surface">
        <form className="filters" action="/tickets">
          <label>검색<input name="q" maxLength={200} defaultValue={search.get('q') ?? ''} placeholder="제목, 서비스, 티켓 번호" /></label>
          <label>상태<select name="status" defaultValue={search.get('status') ?? ''}>
            <option value="">전체</option>
            {Object.entries(statuses).map(([code, label]) => <option value={code} key={code}>{label}</option>)}
          </select></label>
          <label>우선순위<select name="severity" defaultValue={search.get('severity') ?? ''}>
            <option value="">전체</option>
            {priorities.map(priority => <option key={priority}>{priority}</option>)}
          </select></label>
          <label>보관<select name="scope" defaultValue={search.get('scope') ?? 'active'}>
            <option value="active">미보관</option><option value="archived">보관됨</option><option value="all">전체</option>
          </select></label>
          <button className="secondary">검색</button>
        </form>
        <ErrorNotice message={error} />
        {!page && !error && <p role="status">티켓을 불러오는 중입니다.</p>}
        {page && (page.items.length ? (
          <div className="table-scroll">
            <table>
              <thead><tr><th scope="col">번호</th><th scope="col">제목</th><th scope="col">서비스</th><th scope="col">우선순위</th><th scope="col">상태</th><th scope="col">발생 시각</th></tr></thead>
              <tbody>{page.items.map(ticket => (
                <tr key={ticket.id}>
                  <td>{ticket.number}</td><td><a href={'/tickets/' + ticket.id}>{ticket.title}</a></td>
                  <td>{ticket.service}</td><td>{ticket.severity}</td>
                  <td>{ticket.archived_at ? '보관됨' : statuses[ticket.status]}</td>
                  <td>{koreanDate.format(new Date(ticket.detected_at))}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p>조건에 맞는 티켓이 없습니다.</p>)}
        {page?.next_cursor && <a className="next" href={'/tickets?' + search}>다음 페이지</a>}
      </section>
    </>
  )
}

function TicketForm({ session, ticket }: { session: Session; ticket?: Ticket }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const initialDetectedAt = dateInput(ticket?.detected_at ?? new Date().toISOString())

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const form = new FormData(event.currentTarget)
    const value = (field: string) => String(form.get(field) ?? '').trim()
    const detectedAt = value('detected_at')
    const recheckAt = value('recheck_at')
    const body = {
      title: value('title'), description: value('description'), service: value('service'),
      severity: value('severity'), impact: value('impact') || null, next_action: value('next_action') || null,
      ...(!ticket || detectedAt !== initialDetectedAt ? { detected_at: detectedAt + ':00+09:00' } : {}),
      ...(ticket ? { expected_version: ticket.version } : {}),
      ...(ticket?.status === 'waiting' && recheckAt !== (ticket.recheck_at ? dateInput(ticket.recheck_at) : '')
        ? { recheck_at: recheckAt ? recheckAt + ':00+09:00' : null } : {}),
    }
    try {
      const saved = await api<Ticket>(ticket ? '/tickets/' + ticket.id : '/tickets', {
        method: ticket ? 'PATCH' : 'POST', body: JSON.stringify(body),
      }, session.csrf_token)
      window.location.assign('/tickets/' + saved.id)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="surface narrow">
      <h1>{ticket ? ticket.number + ' 수정' : '새 티켓'}</h1>
      <ErrorNotice message={error} />
      <form onSubmit={submit}>
        <label>제목<input name="title" maxLength={160} required defaultValue={ticket?.title} /></label>
        <label>증상<textarea rows={4} name="description" maxLength={10000} required defaultValue={ticket?.description} /></label>
        <div className="fields">
          <label>영향 서비스<input name="service" maxLength={80} required defaultValue={ticket?.service} /></label>
          <label>우선순위<select name="severity" defaultValue={ticket?.severity ?? 'P3'}>{priorities.map(priority => <option key={priority}>{priority}</option>)}</select></label>
        </div>
        <label>발생 시각 (KST)<input type="datetime-local" name="detected_at" required defaultValue={initialDetectedAt} /></label>
        <label>영향 범위<textarea rows={2} name="impact" maxLength={4000} defaultValue={ticket?.impact ?? ''} /></label>
        <label>다음 조치<textarea rows={2} name="next_action" maxLength={4000} defaultValue={ticket?.next_action ?? ''} /></label>
        {ticket?.status === 'waiting' && <label>재확인 시각 (KST)<input type="datetime-local" name="recheck_at" defaultValue={ticket.recheck_at ? dateInput(ticket.recheck_at) : ''} /></label>}
        <div className="row"><button disabled={busy}>저장</button><a href={ticket ? '/tickets/' + ticket.id : '/tickets'}>취소</a></div>
      </form>
    </section>
  )
}

function TicketDetail({ id, edit, session }: { id: string; edit: boolean; session: Session }) {
  const [ticket, setTicket] = useState<Ticket | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [target, setTarget] = useState<Status | ''>('')

  useEffect(() => {
    api<Ticket>('/tickets/' + id).then(setTicket).catch(failure => setError(errorMessage(failure)))
  }, [id])

  if (!ticket) return error ? <ErrorNotice message={error} /> : <p role="status">티켓을 불러오는 중입니다.</p>
  const current = ticket

  async function transition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      setTicket(await api<Ticket>('/tickets/' + id + '/transitions', {
        method: 'POST',
        body: JSON.stringify({ expected_version: current.version, to_status: target,
          reason: form.get('reason'), ...(target === 'resolved' ? { resolution: form.get('resolution') } : {}) }),
      }, session.csrf_token))
      setTarget('')
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  async function archive() {
    if (!window.confirm('이 티켓을 보관하시겠습니까?')) return
    setBusy(true)
    setError('')
    try {
      await api('/tickets/' + id + '?expected_version=' + current.version, { method: 'DELETE' }, session.csrf_token)
      window.location.assign('/tickets')
    } catch (failure) {
      setError(errorMessage(failure))
      setBusy(false)
    }
  }

  if (edit && !ticket.archived_at) return <TicketForm session={session} ticket={ticket} />
  return (
    <>
      <div className="row heading">
        <div><p className="meta">{ticket.number}</p><h1>{ticket.title}</h1></div>
        {!ticket.archived_at && <a className="button secondary" href={'/tickets/' + id + '/edit'}>수정</a>}
      </div>
      <ErrorNotice message={error} />
      <section className="surface">
        <dl className="details">
          <div><dt>상태</dt><dd>{ticket.archived_at ? '보관됨' : statuses[ticket.status]}</dd></div>
          <div><dt>우선순위</dt><dd>{ticket.severity}</dd></div>
          <div><dt>서비스</dt><dd>{ticket.service}</dd></div>
          <div><dt>발생 시각</dt><dd>{koreanDate.format(new Date(ticket.detected_at))} KST</dd></div>
          <div className="full"><dt>증상</dt><dd>{ticket.description}</dd></div>
          <div className="full"><dt>영향 범위</dt><dd>{ticket.impact ?? '미입력'}</dd></div>
          <div className="full"><dt>다음 조치</dt><dd>{ticket.next_action ?? '미입력'}</dd></div>
          {ticket.recheck_at && <div><dt>재확인 시각</dt><dd>{koreanDate.format(new Date(ticket.recheck_at))} KST</dd></div>}
          {ticket.resolution && <div className="full"><dt>해결 요약</dt><dd>{ticket.resolution}</dd></div>}
        </dl>
      </section>
      {!!ticket.allowed_transitions.length && <section className="surface narrow">
        <h2>상태 변경</h2>
        <form key={ticket.version} onSubmit={transition}>
          <label>다음 상태<select required value={target} onChange={event => setTarget(event.target.value as Status | '')}>
            <option value="" disabled>선택</option>
            {ticket.allowed_transitions.map(status => <option key={status} value={status}>{statuses[status]}</option>)}
          </select></label>
          <label>변경 사유<textarea name="reason" rows={2} required maxLength={4000} /></label>
          {target === 'resolved' && <label>해결 요약<textarea name="resolution" rows={2} required maxLength={4000} /></label>}
          <div className="row">
            <button disabled={busy || !target}>상태 저장</button>
            {ticket.status === 'closed' && <button className="secondary" type="button" onClick={archive} disabled={busy}>티켓 보관</button>}
          </div>
        </form>
      </section>}
    </>
  )
}

function App() {
  const [session, setSession] = useState<Session | null | undefined>()
  const [error, setError] = useState('')
  const path = window.location.pathname
  const detail = /^\/tickets\/([^/]+)(\/edit)?$/.exec(path)

  const sopDetail = /^\/knowledge\/([^/]+)$/.exec(path)

  useEffect(() => {
    api<Session>('/auth/session').then(setSession).catch(failure => {
      if (!(failure instanceof ApiError && failure.status === 401)) setError(errorMessage(failure))
      setSession(null)
    })
  }, [])

  async function logout() {
    if (!session) return
    try {
      await api('/auth/logout', { method: 'POST' }, session.csrf_token)
      setSession(null)
      window.location.assign('/login')
    } catch (failure) {
      setError(errorMessage(failure))
    }
  }

  return (
    <>
      <header><nav className="shell row">
        <div className="row">
          <a className="brand" href="/tickets">IT 운영 지원</a>
          {session && <><a href="/tickets">장애 티켓</a><a href="/knowledge">SOP 문서</a><a href="/knowledge/search">지식 검색</a></>}
        </div>
        {session && <div className="row account"><span>{session.user.display_name}</span><button className="secondary" onClick={logout}>로그아웃</button></div>}
      </nav></header>
      <main className="shell">
        <ErrorNotice message={error} />
        {session === undefined ? <p role="status">세션을 확인하는 중입니다.</p>
          : !session ? <Login onLogin={setSession} />
          : path === '/knowledge/search' ? <KnowledgeSearch session={session} />
          : path === '/knowledge' ? <SopList session={session} />
          : sopDetail ? <SopDetail id={sopDetail[1]} session={session} />
          : path === '/tickets/new' ? <TicketForm session={session} />
          : detail ? <TicketDetail id={detail[1]} edit={Boolean(detail[2])} session={session} />
          : <TicketList />}
      </main>
    </>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
