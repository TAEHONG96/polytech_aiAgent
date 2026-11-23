export type Status = 'open' | 'in_progress' | 'waiting' | 'resolved' | 'closed'
export type Severity = 'P1' | 'P2' | 'P3' | 'P4'
export type SimulationStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED'

export const statuses: Record<Status, string> = {
  open: '접수',
  in_progress: '처리 중',
  waiting: '대기',
  resolved: '해결',
  closed: '종료',
}

export interface Ticket {
  id: string
  number: string
  title: string
  description: string
  service: string
  severity: Severity
  status: Status
  detected_at: string
  impact: string | null
  next_action: string | null
  recheck_at: string | null
  resolution: string | null
  archived_at: string | null
  version: number
  allowed_transitions: Status[]
}

export interface TicketPage {
  items: Ticket[]
  next_cursor: string | null
}

export interface Session {
  user: { id: string; display_name: string }
  roles: string[]
  csrf_token: string
  corpus_training?: boolean
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly details?: Record<string, unknown>, readonly code?: string) {
    super(message)
  }
}

export async function api<T>(path: string, options?: RequestInit, token?: string): Promise<T> {
  const headers = new Headers(options?.headers)
  if (options?.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (token) headers.set('X-CSRF-Token', token)
  const response = await fetch('/api/v1' + path, { ...options, headers })
  if (!response.ok) {
    const content = response.headers.get('Content-Type') || ''
    const payload = content.includes('application/json') ? await response.json() : null
    throw new ApiError(payload?.error?.message ?? `요청 실패 (${response.status})`, response.status, payload?.error?.details, payload?.error?.code)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '서버에 연결할 수 없습니다.'
}
