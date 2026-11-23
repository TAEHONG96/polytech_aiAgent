import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, errorMessage } from './api'
import type { Session, Ticket } from './api'
import { displayDate, shiftLabel, TicketHistory, workStatuses } from './handover'
import type { Event, Shift, Work } from './handover'
import { ErrorNotice } from './ui'

interface Operator { id: string; name: string; team_ids: string[] }
interface IncomingHandover { summary: string; tickets: { ticket_id: string }[] }
interface TrainingShift { crew_code: string; type: string; actor_type: string }
interface Operations {
  shift_instance_id: string | null
  owner_id: string | null
  warnings: string | null
  approval_required: boolean | null
  version: number
  work: Work[]
  history: Event[]
}
const koreanInputDate = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
const nullable = (form: FormData, name: string) => String(form.get(name) ?? '').trim() || null

export function TicketOperations({ ticket, session, onVersion, trainingPath = '', readOnly = false }: {
  ticket: Ticket; session: Session; onVersion: (version: number) => void; trainingPath?: string; readOnly?: boolean
}) {
  const [data, setData] = useState<Operations>()
  const [incoming, setIncoming] = useState<IncomingHandover>()
  const [incomingError, setIncomingError] = useState('')
  const [nextShift, setNextShift] = useState<TrainingShift>()
  const [scheduleError, setScheduleError] = useState('')
  const [roster, setRoster] = useState<Operator[]>([])
  const [shifts, setShifts] = useState<Shift[]>([])
  const [shiftId, setShiftId] = useState('')
  const [ownerId, setOwnerId] = useState('')
  const [editing, setEditing] = useState<Work | 'new' | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const path = trainingPath + '/tickets/' + ticket.id
  const team = shifts.find(shift => shift.id === shiftId)?.team_id
  const locked = readOnly || !!ticket.archived_at

  useEffect(() => {
    if (!trainingPath) return
    const controller = new AbortController()
    api<IncomingHandover>(trainingPath + '/incoming-handover', { signal: controller.signal })
      .then(setIncoming)
      .catch(failure => { if (!controller.signal.aborted) setIncomingError(errorMessage(failure)) })
    api<{ slots: TrainingShift[] }>(trainingPath + '/schedule', { signal: controller.signal })
      .then(schedule => setNextShift(schedule.slots.at(-1)))
      .catch(failure => { if (!controller.signal.aborted) setScheduleError(errorMessage(failure)) })
    return () => controller.abort()
  }, [trainingPath])

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api<Operations>(path + '/operations'),
      trainingPath ? { items: [] } : api<{ items: Shift[] }>('/shift-instances'),
      trainingPath ? { items: [{ id: session.user.id, name: session.user.display_name, team_ids: [] }] }
        : api<{ items: Operator[] }>('/handover-operators'),
    ]).then(([operations, shifts, roster]) => {
      if (!cancelled) {
        setData(operations)
        setShiftId(operations.shift_instance_id || '')
        setOwnerId(operations.owner_id || '')
        setShifts(shifts.items)
        setRoster(roster.items)
      }
    }).catch(failure => { if (!cancelled) setError(errorMessage(failure)) })
    return () => { cancelled = true }
  }, [path, ticket.version, trainingPath, session.user.id, session.user.display_name])

  async function save(endpoint: string, method: string, payload: object) {
    if (!data) return
    setBusy(true)
    setError('')
    try {
      const result = await api<Operations>(path + endpoint, {
        method, body: JSON.stringify({ expected_version: data.version, ...payload }),
      }, session.csrf_token)
      setData(result)
      setEditing(null)
      onVersion(result.version)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(false) }
  }

  function saveOperations(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const approval = form.get('approval_required')
    void save('/operations', 'PUT', {
      shift_instance_id: shiftId || null, owner_id: ownerId || null, warnings: nullable(form, 'warnings'),
      approval_required: approval === '' ? null : approval === 'true',
    })
  }

  function saveTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const due = nullable(form, 'due_at')
    const task = editing && editing !== 'new' ? editing : null
    const status = form.get('status')
    void save(task ? '/work/' + task.id : '/work', task ? 'PUT' : 'POST', {
      title: nullable(form, 'title'), status,
      owner_id: trainingPath && status !== 'done' ? null : nullable(form, 'owner_id'),
      due_at: trainingPath ? (task?.due_at ?? null) : due ? new Date(due + ':00+09:00').toISOString() : null,
      note: nullable(form, 'note'),
    })
  }

  const task = editing && editing !== 'new' ? editing : null
  const operations = data && (locked ? <dl className="details">
        <div><dt>담당 근무 회차</dt><dd>{shifts.find(shift => shift.id === data.shift_instance_id)?.team_name || '미배정'}</dd></div>
        <div><dt>담당자</dt><dd>{roster.find(user => user.id === data.owner_id)?.name || '미지정'}</dd></div>
        <div><dt>주의사항</dt><dd>{data.warnings || '미기록'}</dd></div>
        <div><dt>추가 승인</dt><dd>{data.approval_required === null ? '미확인' : data.approval_required ? '필요' : '해당 없음'}</dd></div>
      </dl> : <form onSubmit={saveOperations} key={data.version}>
        <div className="fields">
          {!trainingPath && <label>담당 근무 회차<select value={shiftId} onChange={event => { setShiftId(event.target.value); setOwnerId('') }}>
            <option value="">미배정</option>
            {shiftId && !shifts.some(shift => shift.id === shiftId) && <option value={shiftId}>기존 담당 회차</option>}
            {shifts.map(shift => <option key={shift.id} value={shift.id}>{shiftLabel(shift)}</option>)}
          </select></label>}
          <label>담당자<select value={ownerId} onChange={event => setOwnerId(event.target.value)}>
            <option value="">미지정</option>
            {ownerId && !roster.some(user => user.id === ownerId && (trainingPath || team && user.team_ids.includes(team))) &&
              <option value={ownerId}>기존 담당자</option>}
            {roster.filter(user => trainingPath || team && user.team_ids.includes(team)).map(user => <option key={user.id} value={user.id}>{user.name}</option>)}
          </select></label>
          <label className="full">주의사항<textarea name="warnings" maxLength={4000} rows={2}
            placeholder="예: 재진단 결과를 확인한 뒤 호스트 상태를 변경하세요." defaultValue={data.warnings || ''} /></label>
          <label>추가 승인<select name="approval_required" defaultValue={data.approval_required === null ? '' : String(data.approval_required)}>
            <option value="">미확인</option><option value="true">승인 필요</option><option value="false">해당 없음</option>
          </select></label>
        </div>
        <button className="secondary" disabled={busy}>업무 정보 저장</button>
      </form>)
  const workList = (workItems: Work[]) => <ul>{workItems.map(work => <li id={'work-' + work.id} className="work-line" key={work.id}>
          <div className="row"><strong>{work.title}</strong><span>{workStatuses[work.status]}</span>
            <span>{trainingPath && work.status !== 'done' ? '인수 후 배정'
              : roster.find(user => user.id === work.owner_id)?.name || '담당 미지정'}</span>
            {!trainingPath && work.due_at && <span>{displayDate(work.due_at)} KST</span>}
            {!locked && <button className="secondary" disabled={busy} onClick={() => setEditing(work)}>작업 수정</button>}
          </div>
          {work.note && <p className="knowledge-text">{work.note}</p>}
        </li>)}</ul>
  const workForm = !locked && editing && <form className="handover-actions" onSubmit={saveTask} key={task?.id || 'new'}>
          <div className="fields">
            <label>작업명<input name="title" maxLength={160} defaultValue={task?.title} required placeholder="예: 스토리지 응답 지연 재확인" /></label>
            <label>작업 상태<select name="status" defaultValue={task?.status || (ticket.status === 'closed' ? 'done' : 'todo')}>
              {Object.entries(workStatuses).filter(([key]) => ticket.status !== 'closed' || key === 'done')
                .map(([key, value]) => <option key={key} value={key}>{value}</option>)}
            </select></label>
            {(!trainingPath || task?.status === 'done') && <label>작업 담당자<select name="owner_id" defaultValue={task?.owner_id || ''}>
              <option value="">미지정</option>
              {roster.map(user => <option key={user.id} value={user.id}>{user.name}</option>)}
            </select></label>}
            {!trainingPath && <label>마감 시각 (KST)<input type="datetime-local" name="due_at"
              defaultValue={task?.due_at ? koreanInputDate.format(new Date(task.due_at)).replace(' ', 'T') : ''} /></label>}
            <label className="full">작업 메모<textarea name="note" rows={2} maxLength={4000}
              placeholder="예: 대기열과 응답 시간을 비교해 지연이 지속되는지 기록" defaultValue={task?.note || ''} /></label>
          </div>
          <div className="row"><button disabled={busy}>작업 저장</button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setEditing(null)}>취소</button></div>
        </form>
  const addWork = !locked && !editing && <button className="secondary" disabled={busy} onClick={() => setEditing('new')}>후속 작업 추가</button>
  const completed = data?.work.filter(work => work.status === 'done') ?? []
  const pending = data?.work.filter(work => work.status !== 'done') ?? []
  return <section className="surface">
    <h2>{trainingPath ? '작업 이력' : '교대 업무 및 작업 이력'}</h2>
    <ErrorNotice message={error} />
    {!data && !error && <p role="status">업무 정보를 불러오는 중입니다.</p>}
    {error && <button className="secondary" onClick={() => window.location.reload()}>최신 티켓 불러오기</button>}
    {trainingPath ? <>
      <section className="handover-actions">
        <h3>이전 근무조 인수 내역</h3>
        <ErrorNotice message={incomingError} />
        {!incoming && !incomingError && <p role="status">이전 인수 내역을 불러오는 중입니다.</p>}
        {incoming && <p className="knowledge-text">{incoming.tickets.some(link => link.ticket_id === ticket.id)
          ? (incoming.summary || '인수 요약이 없습니다.') : '이전 근무조에서 인수한 내역이 없습니다.'}</p>}
      </section>
      <section className="handover-actions">
        <h3>현재 근무조 작업 이력</h3>
        {operations}
        {completed.length > 0 && workList(completed)}
        {task?.status === 'done' && workForm}
        {data && <TicketHistory history={data.history} ticketId={ticket.id} ticketPath={path} />}
      </section>
      <section className="handover-actions">
        <h3>다음 근무조 연계 예정</h3>
        {scheduleError ? <ErrorNotice message={scheduleError} /> : <dl className="details">
          <div><dt>인계 대상</dt><dd>{nextShift
            ? `${nextShift.crew_code}조 ${nextShift.type === 'DAY' ? '주간' : '야간'} · ${nextShift.actor_type === 'BOT' ? 'Bot' : '교육생'}`
            : '다음 근무조를 불러오는 중입니다.'}</dd></div>
          <div><dt>개인 담당자</dt><dd>인수 후 배정</dd></div>
        </dl>}
        {data && <>
          {pending.length ? workList(pending) : <p>연계 예정 작업이 없습니다.</p>}
          {addWork}
          {task?.status !== 'done' && workForm}
        </>}
      </section>
    </> : data && <>
      {operations}
      <div className="handover-actions">
        <h3>후속 작업</h3>
        {!data.work.length && <p>등록된 작업이 없습니다.</p>}
        {workList(data.work)}
        {addWork}
        {workForm}
      </div>
      <TicketHistory history={data.history} ticketId={ticket.id} />
    </>}
  </section>
}
