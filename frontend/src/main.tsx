/// <reference types="vite/client" />

import { lazy, StrictMode, Suspense, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, errorMessage, statuses } from './api'
import type { Session, Severity, Status, Ticket, TicketPage } from './api'
import { TrainingPage } from './training'
import { CompanionContextProvider } from './companion/companion-context'
import { AgentPage } from './agent'
import { KnowledgeSearch, SopDetail, SopList } from './knowledge'
import { HandoverDashboard, HandoverDetail } from './handover'
import { TicketOperations } from './ticket-operations'
import { ErrorNotice } from './ui'
import './style.css'
import './training.css'

const priorities: Severity[] = ['P1', 'P2', 'P3', 'P4']
const showDemoAccount = import.meta.env.VITE_PUBLIC_DEMO === 'true'
const CompanionPet = lazy(() => import('./companion/CompanionPet'))
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

function Login({ onLogin, path }: { onLogin: (session: Session) => void; path: string }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [registration, setRegistration] = useState<boolean>()
  const [created, setCreated] = useState(false)
  const joining = path === '/register' && !created

  useEffect(() => {
    const controller = new AbortController()
    api<{ enabled: boolean }>('/auth/registration', { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setRegistration(result.enabled) })
      .catch(() => { if (!controller.signal.aborted) setRegistration(false) })
    return () => controller.abort()
  }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const form = new FormData(event.currentTarget)
    const credentials = { username: form.get('username'), password: form.get('password') }
    if (joining && credentials.password !== form.get('password_confirmation')) {
      setError('비밀번호와 비밀번호 확인이 일치하지 않습니다.')
      return
    }
    setBusy(true)
    try {
      if (joining) {
        await api('/auth/register', { method: 'POST', body: JSON.stringify(credentials) })
        setCreated(true)
      } else {
        onLogin(await api<Session>('/auth/login', { method: 'POST', body: JSON.stringify(credentials) }))
      }
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="surface narrow training-login">
      <h1>{joining ? '교육 계정 만들기' : '로그인'}</h1>
      <p>{joining ? '교육 계정은 본인의 가상 근무와 평가 기록을 사용합니다.' : '교육 또는 운영 계정으로 로그인하세요.'}</p>
      {showDemoAccount && !joining && <aside className="demo-account" aria-labelledby="demo-account-title">
        <h2 id="demo-account-title">시연용 데모 계정</h2>
        <dl className="details">
          <div><dt>사용자명</dt><dd><code>demo</code></dd></div>
          <div><dt>비밀번호</dt><dd><code>local-demo-password</code></dd></div>
        </dl>
        <p className="meta">공용 계정입니다. 시연에는 가상 데이터만 사용해 주세요.</p>
      </aside>}
      <ErrorNotice message={error} />
      {created && <p role="status">교육 계정이 만들어졌습니다. 새 계정으로 로그인하세요.</p>}
      {joining && registration === undefined && <p role="status">가입 가능 여부를 확인하는 중입니다.</p>}
      {joining && registration === false && <p>현재 교육 계정 가입이 닫혀 있습니다.</p>}
      {(!joining || registration) && <form key={String(joining)} onSubmit={submit}>
        <label>사용자명<input name="username" autoComplete="username" maxLength={64} required placeholder="예: trainee-1234" /></label>
        <label>비밀번호<input name="password" type="password" autoComplete={joining ? 'new-password' : 'current-password'}
          minLength={joining ? 8 : undefined} placeholder={joining ? '8자 이상 입력' : '계정 비밀번호 입력'} required /></label>
        {joining && <label>비밀번호 확인<input name="password_confirmation" type="password" autoComplete="new-password"
          required placeholder="위 비밀번호를 다시 입력" /></label>}
        <button disabled={busy}>{joining ? '교육 계정 만들기' : '로그인'}</button>
      </form>}
      {joining && <p><a href="/login">로그인으로 돌아가기</a></p>}
      {!joining && !created && registration && <p><a href="/register">교육 계정 만들기</a></p>}
    </section>
  )
}

const features = [
  { href: '/tickets', title: '장애 티켓', description: '장애를 등록하고 우선순위, 상태, 담당 정보를 관리합니다. 처리 이력과 해결 내용을 티켓에서 확인할 수 있습니다.' },
  { href: '/knowledge', title: 'SOP 문서', description: '운영 절차 문서를 등록하고 색인 상태를 확인합니다. 준비된 가상 SOP의 원본도 내려받을 수 있습니다.' },
  {
    href: '/knowledge/search',
    title: '지식 검색',
    description: 'RAG(검색 증강 생성)의 검색 단계를 활용합니다. SOP와 해결·종료 티켓을 텍스트 조각으로 나누어 임베딩하고, 장애 증상이나 오류 메시지를 입력하면 pgvector의 코사인 유사도로 관련 문서와 티켓의 원문 구절을 찾아 보여줍니다.',
  },
  {
    href: '/agent',
    title: 'AI 장애 분석',
    description: 'Gemini 에이전트는 함수 호출(Function Calling)로 티켓 상세·변경 이력을 조회하고, SOP와 유사 해결 티켓을 검색합니다. 수집한 출처를 인용해 확인된 사실, 원인 가설, 점검 순서를 분석 초안으로 정리합니다. 이 시연 환경에서는 모의 공급자로 도구 호출과 분석 흐름을 체험할 수 있습니다.',
  },
  { href: '/handovers', title: '교대 인수인계', description: '교대 회차별 티켓과 작업 기록을 검토하고, 인계조 확정과 인수조 확인을 남깁니다.' },
]

function About({ signedIn }: { signedIn: boolean }) {
  return (
    <section aria-labelledby="about-title">
      <div className="surface">
        <h1 id="about-title">IT 운영 지원 소개</h1>
        <p>장애 접수부터 운영 절차 검색, 분석 초안 검토까지 한곳에서 진행합니다.</p>
        {!signedIn && <a className="button" href="/login">로그인하고 체험하기</a>}
      </div>
      <div className="feature-grid">
        {features.map(({ href, title, description }) => <article className="surface" key={href}>
          <h2>{signedIn ? <a href={href}>{title}</a> : title}</h2>
          <p>{description}</p>
        </article>)}
      </div>
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
          <label>검색<input name="q" maxLength={200} defaultValue={search.get('q') ?? ''} placeholder="예: INC-000123 또는 스토리지 지연" /></label>
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
        <label>제목<input name="title" maxLength={160} required defaultValue={ticket?.title} placeholder="예: 스토리지 응답 지연" /></label>
        <label>증상<textarea rows={4} name="description" maxLength={10000} required defaultValue={ticket?.description}
          placeholder="예: 스토리지 요청 응답에 평소보다 3초 더 걸리고 지연 경보가 발생함" /></label>
        <div className="fields">
          <label>영향 서비스<input name="service" maxLength={80} required defaultValue={ticket?.service} placeholder="예: 파일 저장 서비스" /></label>
          <label>우선순위<select name="severity" defaultValue={ticket?.severity ?? 'P3'}>{priorities.map(priority => <option key={priority}>{priority}</option>)}</select></label>
        </div>
        <label>발생 시각 (KST)<input type="datetime-local" name="detected_at" required defaultValue={initialDetectedAt} /></label>
        <label>영향 범위<textarea rows={2} name="impact" maxLength={4000} defaultValue={ticket?.impact ?? ''}
          placeholder="예: 일부 사용자의 파일 저장 요청이 지연됨" /></label>
        <label>다음 조치<textarea rows={2} name="next_action" maxLength={4000} defaultValue={ticket?.next_action ?? ''}
          placeholder="예: 스토리지 대기열과 응답 시간을 재확인" /></label>
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
        <div className="row">
          <a className="button secondary" href={'/agent?ticket=' + encodeURIComponent(id)}>AI 분석</a>
          {!ticket.archived_at && <a className="button secondary" href={'/tickets/' + id + '/edit'}>수정</a>}
        </div>
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
          <label>변경 사유<textarea name="reason" rows={2} required maxLength={4000} placeholder="예: 응답 시간이 정상 범위로 돌아와 해결로 변경" /></label>
          {target === 'resolved' && <label>해결 요약<textarea name="resolution" rows={2} required maxLength={4000}
            placeholder="예: 대기열을 정리하고 파일 저장 요청이 정상 처리되는 것을 확인" /></label>}
          <div className="row">
            <button disabled={busy || !target}>상태 저장</button>
            {ticket.status === 'closed' && <button className="secondary" type="button" onClick={archive} disabled={busy}>티켓 보관</button>}
          </div>
        </form>
      </section>}
      <TicketOperations ticket={ticket} session={session} onVersion={version => {
        setTicket(current => current ? { ...current, version } : current)
      }} />
    </>
  )
}

function App() {
  const [session, setSession] = useState<Session | null | undefined>()
  const [error, setError] = useState('')
  const [path, setPath] = useState(() => window.location.pathname)
  const detail = /^\/tickets\/([^/]+)(\/edit)?$/.exec(path)
  const handover = /^\/handovers\/([^/]+)$/.exec(path)

  const sopDetail = /^\/knowledge\/([^/]+)$/.exec(path)

  useEffect(() => {
    let cancelled = false
    api<Session>('/auth/session').then(restored => {
      if (!cancelled) setSession(restored)
    }).catch(failure => {
      if (cancelled) return
      if (!(failure instanceof ApiError && failure.status === 401)) setError(errorMessage(failure))
      setSession(null)
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const followHistory = () => setPath(window.location.pathname)
    window.addEventListener('popstate', followHistory)
    return () => window.removeEventListener('popstate', followHistory)
  }, [])

  function startSession(authenticated: Session) {
    if (path === '/login' || path === '/register' || path === '/') {
      const destination = authenticated.roles.includes('trainee') ? '/simulation' : '/tickets'
      window.history.pushState(null, '', destination)
      setPath(destination)
    }
    setSession(authenticated)
  }

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

  const trainee = session?.roles.includes('trainee')
  const trainingRun = /^\/simulation-runs\/[\da-f-]{36}(?=\/|$)/i.exec(path)?.[0]
  if (session && trainee) return <CompanionContextProvider path={path} enabled={session.corpus_training === true}><div className="training-layout">
    <a className="skip-link" href="#main-content">본문으로 이동</a>
    <aside className="training-sidebar">
      <div className="training-brand"><span aria-hidden="true">D</span><div>DCO Ticket Assistant<small>장애 대응 실습</small></div></div>
      <nav aria-label="교육 메뉴">
        <a href="/simulation" aria-current={path === '/simulation' || path === '/' ? 'page' : undefined}>내 실습</a>
        {trainingRun && <>
          <a href={trainingRun} aria-current={path === trainingRun ? 'page' : undefined}>근무 워크스페이스</a>
          <a href={trainingRun + '/sops'} aria-current={path.startsWith(trainingRun + '/sops') ? 'page' : undefined}>승인 SOP 자료</a>
        </>}
        <a href="/about" aria-current={path === '/about' ? 'page' : undefined}>서비스 소개</a>
      </nav>
      <div className="training-account"><strong>{session.user.display_name}</strong><small>교육생</small>
        <button className="secondary" onClick={logout}>로그아웃</button></div>
    </aside>
    <div className="training-body">
      <header className="training-topbar"><strong>교육 시뮬레이션</strong><span>가상 운영 환경</span></header>
      <main className="training-shell" id="main-content" tabIndex={-1}>
        <ErrorNotice message={error} />
        {path === '/about' ? <section className="surface">
          <h1>장애 대응 실습 소개</h1>
          <p>가상 데이터센터에서 이전 교대의 인계를 읽고, 티켓과 승인 SOP를 조사하고, 조치 결과를 다음 근무조에 전달합니다.</p>
          <p>인계 확정 후 실습 평가에서 행동 기록과 판정 근거를 확인할 수 있습니다.</p>
          <a className="button" href="/simulation">내 실습 열기</a>
        </section> : <TrainingPage key={path} path={path} session={session} />}
      </main>
    </div>
    {session.corpus_training === true && <Suspense fallback={null}><CompanionPet key={session.user.id} session={session} /></Suspense>}
  </div></CompanionContextProvider>

  return (
    <>
      <a className="skip-link" href="#main-content">본문으로 이동</a>
      <header><nav className="shell row">
        <div className="row">
          <a className="brand" href="/tickets">IT 운영 지원</a>
          <a href="/about" aria-current={path === '/about' ? 'page' : undefined}>소개</a>
          {session && <><a href="/tickets">장애 티켓</a><a href="/knowledge">SOP 문서</a><a href="/knowledge/search">지식 검색</a><a href="/agent">AI 장애 분석</a><a href="/handovers" aria-current={path.startsWith('/handovers') ? 'page' : undefined}>교대 인수인계</a></>}
        </div>
        {session && <div className="row account"><span>{session.user.display_name}</span><button className="secondary" onClick={logout}>로그아웃</button></div>}
      </nav></header>
      <main className="shell" id="main-content" tabIndex={-1}>
        <ErrorNotice message={error} />
        {path === '/about' ? <About signedIn={Boolean(session)} />
          : session === undefined ? <p role="status">세션을 확인하는 중입니다.</p>
          : !session ? <Login key={path} onLogin={startSession} path={path} />
          : path === '/agent' ? <AgentPage session={session} />
          : path === '/handovers' ? <HandoverDashboard session={session} />
          : handover ? <HandoverDetail id={handover[1]} session={session} />
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
