export const companionStates = [
  'idle', 'walking', 'hint', 'chatting', 'thinking', 'happy', 'sleeping', 'surprised',
] as const

export type CompanionState = typeof companionStates[number]
export type CompanionPosition = { x: number; y: number }

export type CompanionCommand =
  | { type: 'state'; state: CompanionState }
  | { type: 'hint'; text: string }
  | { type: 'hint-request' | 'hint-dismiss' }
  | { type: 'chat'; hintId?: string; tutorOfferId?: string; suggestedMessage?: string }
  | { type: 'move'; position: CompanionPosition }

export const companionCommandEvent = 'dco:companion-command'
export const companionChatRequestedEvent = 'dco:companion-chat-requested'

declare global {
  interface WindowEventMap {
    [companionCommandEvent]: CustomEvent<CompanionCommand>
    [companionChatRequestedEvent]: CustomEvent<{ hintId?: string; tutorOfferId?: string; suggestedMessage?: string }>
  }
}

function send(command: CompanionCommand) {
  window.dispatchEvent(new CustomEvent(companionCommandEvent, { detail: command }))
}

export function setCompanionState(state: CompanionState) {
  send({ type: 'state', state })
}

export function showCompanionHint(text: string) {
  send({ type: 'hint', text })
}

export function requestCompanionHint() {
  send({ type: 'hint-request' })
}

export function dismissCompanionHint() {
  send({ type: 'hint-dismiss' })
}

export function openCompanionChat(hintId?: string) {
  send({ type: 'chat', hintId })
}

/** Move the top-left corner of the companion to viewport coordinates in CSS pixels. */
export function moveCompanionTo(position: CompanionPosition) {
  send({ type: 'move', position })
}
