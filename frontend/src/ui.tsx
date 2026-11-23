import { statuses } from './api'
import type { Ticket } from './api'

export function ErrorNotice({ message }: { message: string }) {
  return message && <p className="notice" role="alert">{message}</p>
}

export function Badge({ children, tone = 'neutral' }: {
  children: string; tone?: 'neutral' | 'info' | 'warning' | 'critical' | 'success'
}) {
  return <span className={'badge badge-' + tone}>{children}</span>
}

export function TicketStatus({ ticket }: { ticket: Pick<Ticket, 'status' | 'archived_at'> }) {
  const tones = { open: 'info', in_progress: 'info', waiting: 'warning', resolved: 'success', closed: 'neutral' } as const
  return <Badge tone={ticket.archived_at ? 'neutral' : tones[ticket.status]}>
    {ticket.archived_at ? '보관됨' : statuses[ticket.status]}
  </Badge>
}

export function Priority({ value }: { value: Ticket['severity'] }) {
  return <span className={'priority priority-' + value} aria-label={'우선순위 ' + value}>{value}</span>
}

export function Feedback({ children, kind = 'pending' }: { children: string; kind?: 'pending' | 'success' | 'empty' }) {
  return <p className={'feedback feedback-' + kind} role="status">{children}</p>
}
