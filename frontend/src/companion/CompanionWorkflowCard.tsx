import { useState } from 'react'

export type Workflow = {
  id: string; goal: string; status: 'PLANNING' | 'RUNNING' | 'WAITING_APPROVAL' | 'WAITING_USER_INPUT' | 'PAUSED'
    | 'COMPLETED' | 'FAILED' | 'CANCELLED' | 'EXPIRED';
  steps: { id: number; label: string; status: 'PENDING' | 'RUNNING' | 'WAITING_APPROVAL' | 'WAITING_USER_INPUT'
    | 'COMPLETED' | 'FAILED' | 'SKIPPED' }[];
  current_step_index: number; can_resume: boolean; model_calls: number; embedding_calls: number;
  input_kind?: 'clue' | 'sop_selection' | null;
  outcome?: { performed: string[]; evidence: string[]; incomplete: string[] } | null;
  error_code?: string | null;
}
export type WorkflowCommand = 'resume' | 'pause' | 'cancel'
const workflowLabels = {
  PLANNING: '계획 수립 중', RUNNING: '실행 중', WAITING_APPROVAL: '승인 대기', WAITING_USER_INPUT: '추가 정보 대기',
  PAUSED: '일시중지', COMPLETED: '완료', FAILED: '실패', CANCELLED: '취소됨', EXPIRED: '만료됨',
}
const stepLabels = { PENDING: '대기', RUNNING: '진행 중', WAITING_APPROVAL: '승인 대기', WAITING_USER_INPUT: '선택 대기',
  COMPLETED: '완료', FAILED: '실패', SKIPPED: '건너뜀' }
const errorLabels: Record<string, string> = {
  WORKFLOW_CONTEXT_CHANGED: '작업을 시작한 화면으로 돌아가 재개해 주세요.',
  WORKFLOW_NO_SOP: '관련 승인 SOP를 찾으려면 오류 코드나 관측 증상을 알려 주세요.',
  WORKFLOW_SEARCH_UNAVAILABLE: '승인 SOP 검색을 마치지 못했어요.',
  WORKFLOW_QUEUE_BUSY: '다른 질문 처리가 끝나면 이 화면에서 작업을 재개해 주세요.',
  WORKFLOW_GOAL_INCOMPLETE: '필수 결과가 확인되지 않아 작업을 완료하지 못했어요.',
  target_unavailable: '현재 화면에서 대상 요소를 찾을 수 없어요.',
  context_changed: '실행 도중 화면이 바뀌었어요.', navigation_failed: '승인한 화면을 열지 못했어요.',
}

export function CompanionWorkflowCard({ workflow, busy, control }: {
  workflow: Workflow; busy: boolean; control: (command: WorkflowCommand, clue?: string) => void
}) {
  const [clue, setClue] = useState('')
  const needsInput = workflow.input_kind === 'clue'
  const choosing = workflow.input_kind === 'sop_selection'
  const ordered = workflow.steps.filter(step => step.status === 'COMPLETED').length
  const canPause = choosing || ['PLANNING', 'RUNNING', 'WAITING_APPROVAL'].includes(workflow.status)
  const canCancel = canPause || workflow.can_resume || workflow.status === 'PAUSED'
  return <section className="companion-action-card companion-workflow" aria-label="키로 워크플로">
    <strong>워크플로 · {choosing ? '문서 선택 대기' : workflowLabels[workflow.status]}</strong>
    <p role="status" className="meta">{workflow.steps.length ? `${ordered}/${workflow.steps.length}단계 완료` : '요청의 실행 순서를 확인하는 중이에요.'}</p>
    <ol>{workflow.steps.map((step, index) => <li key={step.id} aria-current={index === workflow.current_step_index ? 'step' : undefined}>
      <span className="meta">{stepLabels[step.status]}</span> {step.label}
    </li>)}</ol>
    {workflow.error_code && errorLabels[workflow.error_code] && <p className="notice">{errorLabels[workflow.error_code]}</p>}
    {needsInput && workflow.can_resume && <label>오류 코드 또는 관측 증상
      <input value={clue} onChange={event => setClue(event.target.value)} maxLength={160} disabled={busy} />
    </label>}
    {import.meta.env.DEV && <p className="meta">모델 호출 {workflow.model_calls}회 · 검색 임베딩 {workflow.embedding_calls}회</p>}
    {(canCancel || workflow.can_resume) && <div className="companion-action-card-buttons">
      {canPause && <button type="button" className="secondary" disabled={busy} onClick={() => control('pause')}>일시중지</button>}
      {canCancel && <button type="button" className="secondary" disabled={busy} onClick={() => control('cancel')}>작업 취소</button>}
      {workflow.can_resume && <button type="button" disabled={busy || (needsInput && !clue.trim())}
        onClick={() => control('resume', needsInput ? clue.trim() : undefined)}>{workflow.status === 'FAILED' ? '실패 단계 재시도' : '작업 재개'}</button>}
    </div>}
  </section>
}

export function CompanionWorkflowOutcome({ outcome }: { outcome: NonNullable<Workflow['outcome']> }) {
  const rows = [
    { label: '수행한 작업', items: outcome.performed },
    { label: '참고 근거', items: outcome.evidence },
    { label: '미완료 작업', items: outcome.incomplete },
  ]
  return <section className="companion-action-card" aria-label="키로 작업 결과">
    <strong>작업 결과</strong>
    {rows.filter(row => row.items.length).map(row => <div key={row.label}>
      <strong>{row.label}</strong>
      <ul>{row.items.map(item => <li key={item}>{item}</li>)}</ul>
    </div>)}
  </section>
}
