import { useState } from 'react'
import { actionErrors, actionLabels, actionSuccess } from './companion-actions'
import type { ActionDecision, Proposal } from './companion-actions'

export function CompanionActionCard({ proposal, active, busy, decide }: {
  proposal: Proposal; active: boolean; busy: boolean; decide: (decision: ActionDecision, selected?: string) => void
}) {
  const [selected, select] = useState<string>()
  const options = proposal.options ?? []
  const choosing = proposal.requires_selection && !proposal.chosen_sop_id
  const chosen = proposal.chosen_sop_id ?? (options.length === 1 ? options[0].sop_id : selected)
  const pending = proposal.status === 'pending' && active
  const notes = {
    pending: active ? choosing ? '문서 선택 대기' : '승인 대기' : '현재 화면에서 다시 요청해 주세요.',
    approved: busy ? '실행 중…' : '승인 기록을 저장했어요. 새로 실행하려면 다시 요청해 주세요.',
    cancelled: '제안을 취소했어요.', expired: '제안이 만료됐어요. 다시 요청해 주세요.',
    stale: '화면이 바뀌었어요. 현재 화면에서 다시 요청해 주세요.',
    succeeded: actionSuccess[proposal.type],
    failed: proposal.error_code ? actionErrors[proposal.error_code] : '동작을 완료하지 못했어요.',
  }

  return <div className="companion-action-card" role="group" aria-label="키로 실행 제안">
    <strong>{proposal.label}</strong>
    <p role="status" className="meta">{notes[proposal.status]}{pending && busy ? ' · 확인 중…' : ''}</p>
    {!!options.length && (pending && !proposal.chosen_sop_id ? <fieldset disabled={busy}>
      <legend>열 문서 선택</legend>
      {options.map(option => <label key={option.sop_id}>
        <input type="radio" name={`sop-${proposal.id}`} checked={chosen === option.sop_id}
          onChange={() => select(option.sop_id)} />{option.title}
      </label>)}
    </fieldset> : proposal.chosen_sop_id ? <p>선택한 문서 · {options.find(option => option.sop_id === chosen)?.title}</p>
      : <ul>{options.map(option => <li key={option.sop_id}>{option.title}</li>)}</ul>)}
    {pending && <div className="companion-action-card-buttons">
      <button type="button" className="secondary" disabled={busy} onClick={() => decide('cancel')}>취소</button>
      <button type="button" disabled={busy || (proposal.type === 'open_sop' && !chosen)}
        onClick={() => decide(choosing ? 'select' : 'approve', chosen)}>{choosing ? '이 문서 선택' : actionLabels[proposal.type]}</button>
    </div>}
  </div>
}
