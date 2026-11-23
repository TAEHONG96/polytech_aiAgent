import { companionChatRequestedEvent } from './companion-events'
import type { CompanionCommand, CompanionPosition, CompanionState } from './companion-events'
import { clampPosition, distance, nearbyDestination, walkDuration, walkPosition } from './companion-movement'
import type { MovementArea } from './companion-movement'

const behavior = {
  idleMin: 8_000,
  idleMax: 16_000,
  walkChance: 0.75,
  sleepAfter: 75_000,
}

const reactionDuration: Partial<Record<CompanionState, number>> = {
  hint: 5_000,
  chatting: 1_800,
  happy: 850,
  surprised: 450,
}

const storageKey = 'dco.companion'
const reducedMotionQuery = '(prefers-reduced-motion: reduce)'

export type CompanionView = {
  state: CompanionState
  direction: 'left' | 'right'
  hint?: string
  dragging: boolean
  hidden: boolean
  paused: boolean
  preview: CompanionState | null
}

export const initialCompanionView: CompanionView = {
  state: 'idle', direction: 'right', dragging: false, hidden: false, paused: false, preview: null,
}

type SavedCompanion = Pick<CompanionView, 'direction' | 'hidden' | 'paused'> & {
  position: CompanionPosition
  anchor: CompanionPosition
}

function savedPreferences(saved: Partial<SavedCompanion>): Pick<SavedCompanion, 'direction' | 'hidden' | 'paused'> {
  return {
    direction: saved.direction === 'left' ? 'left' : 'right',
    hidden: saved.hidden === true,
    paused: saved.paused === true,
  }
}

function validPosition(position?: CompanionPosition): position is CompanionPosition {
  return Number.isFinite(position?.x) && Number.isFinite(position?.y)
}

function restore(): Partial<SavedCompanion> {
  try {
    return JSON.parse(sessionStorage.getItem(storageKey) ?? 'null') ?? {}
  } catch {
    return {}
  }
}

type Walk = {
  from: CompanionPosition
  target: CompanionPosition
  duration: number
  elapsed: number
  lastFrame?: number
  relocate: boolean
}

export function createCompanionController({ readArea, render, place }: {
  readArea: () => MovementArea
  render: (view: CompanionView) => void
  place: (position: CompanionPosition, size: number) => void
}) {
  const motion = window.matchMedia(reducedMotionQuery)
  const saved = restore()
  let area = readArea()
  const home = { x: area.bounds.right, y: area.bounds.bottom }
  let position = clampPosition(validPosition(saved.position) ? saved.position : home, area.bounds)
  let anchor = clampPosition(validPosition(saved.anchor) ? saved.anchor : home, area.bounds)
  let view: CompanionView = {
    ...initialCompanionView,
    ...savedPreferences(saved),
  }
  let walk: Walk | undefined
  let frame: number | undefined
  let timer: number | undefined
  let reactionEndsAt: number | undefined
  let idleCheckAt = 0
  let lastActivity = performance.now()
  let hovered = false
  let pointing = false
  let disposed = false

  function save() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({
        position, anchor, direction: view.direction, hidden: view.hidden, paused: view.paused,
      } satisfies SavedCompanion))
    } catch {
      // The current page can still use the companion when storage is blocked.
    }
  }

  function update(patch: Partial<CompanionView>) {
    view = { ...view, ...patch }
    render(view)
  }

  function setPosition(next: CompanionPosition) {
    position = clampPosition(next, area.bounds)
    place(position, area.size)
  }

  function cancelFrame() {
    window.cancelAnimationFrame(frame ?? 0)
    frame = undefined
    if (walk) walk.lastFrame = undefined
  }

  function stopWalk() {
    cancelFrame()
    walk = undefined
  }

  function recordActivity() {
    lastActivity = performance.now()
  }

  function delayIdleCheck() {
    idleCheckAt = performance.now() + behavior.idleMin + Math.random() * (behavior.idleMax - behavior.idleMin)
  }

  function enter(state: CompanionState, duration?: number, hint?: string) {
    reactionEndsAt = duration === undefined ? undefined : performance.now() + duration
    update({ state, hint })
    if (state === 'idle') delayIdleCheck()
    schedule()
  }

  function canWalk() {
    return !disposed && !document.hidden && !view.hidden && !view.paused && !motion.matches && !hovered && !pointing
  }

  function requestFrame() {
    if (walk && canWalk() && frame === undefined) frame = window.requestAnimationFrame(step)
  }

  function step(now: number) {
    frame = undefined
    if (!walk || !canWalk()) return
    walk.elapsed += now - (walk.lastFrame ?? now)
    walk.lastFrame = now
    const progress = Math.min(1, walk.elapsed / walk.duration)
    setPosition(walkPosition(walk.from, walk.target, progress))
    if (progress === 1) {
      finishWalk(walk.relocate)
    } else {
      requestFrame()
    }
  }

  function finishWalk(relocate: boolean) {
    stopWalk()
    if (relocate) anchor = position
    save()
    enter('idle')
  }

  function startWalk(destination: CompanionPosition, relocate = false) {
    const target = clampPosition(destination, area.bounds)
    stopWalk()
    if (view.paused || motion.matches || distance(position, target) < 1) {
      setPosition(target)
      finishWalk(relocate)
      return
    }

    if (target.x !== position.x) update({ direction: target.x < position.x ? 'left' : 'right' })
    walk = { from: position, target, duration: walkDuration(position, target), elapsed: 0, relocate }
    enter('walking')
    requestFrame()
  }

  function randomWalk() {
    const destination = nearbyDestination(position, anchor, area.bounds)
    if (destination) startWalk(destination)
    return Boolean(destination)
  }

  function tick() {
    timer = undefined
    if (disposed || document.hidden || view.hidden) return
    const now = performance.now()
    if (reactionEndsAt !== undefined && now >= reactionEndsAt) {
      enter('idle')
      return
    }
    if (view.state === 'idle' && !view.preview && !pointing && !hovered && !view.paused) {
      if (now - lastActivity >= behavior.sleepAfter) {
        enter('sleeping')
        return
      }
      if (now >= idleCheckAt) {
        if (!motion.matches && Math.random() < behavior.walkChance && randomWalk()) return
        delayIdleCheck()
      }
    }
    schedule()
  }

  function schedule() {
    window.clearTimeout(timer)
    timer = undefined
    if (disposed || document.hidden || view.hidden) return
    const idle = view.state === 'idle' && !view.preview && !pointing && !hovered && !view.paused
    const sleepAt = lastActivity + behavior.sleepAfter
    const due = reactionEndsAt ?? (idle ? Math.min(motion.matches ? Infinity : idleCheckAt, sleepAt) : undefined)
    if (due !== undefined) timer = window.setTimeout(tick, Math.max(0, Math.ceil(due - performance.now())))
  }

  function syncArea() {
    const next = readArea()
    const { left, top, right, bottom } = next.bounds
    if (area.size === next.size && area.bounds.left === left && area.bounds.top === top
      && area.bounds.right === right && area.bounds.bottom === bottom) return
    area = next
    setPosition(position)
    anchor = clampPosition(anchor, area.bounds)
    if (walk) startWalk(walk.target, walk.relocate)
  }

  function syncVisibility() {
    if (document.hidden) cancelFrame()
    requestFrame()
    schedule()
  }

  function restorePage(event: PageTransitionEvent) {
    if (!event.persisted) return
    const restored = restore()
    if (!validPosition(restored.position)) return
    stopWalk()
    pointing = false
    hovered = false
    area = readArea()
    setPosition(restored.position)
    anchor = clampPosition(validPosition(restored.anchor) ? restored.anchor : position, area.bounds)
    update({ ...savedPreferences(restored), preview: null, dragging: false })
    recordActivity()
    enter('idle')
  }

  function syncMotion() {
    if (motion.matches && walk) finishWalk(walk.relocate)
    schedule()
  }

  function setHovered(next: boolean) {
    if (hovered === next) return
    hovered = next
    recordActivity()
    if (hovered) cancelFrame()
    if (!hovered && view.state === 'idle') delayIdleCheck()
    requestFrame()
    schedule()
  }

  function beginPointer() {
    pointing = true
    stopWalk()
    recordActivity()
    if (view.preview) update({ preview: null })
    if (view.state === 'walking' || view.state === 'sleeping') enter('idle')
    schedule()
    return position
  }

  function dragTo(next: CompanionPosition) {
    if (!view.dragging) {
      update({ dragging: true })
      enter('surprised', reactionDuration.surprised)
    }
    setPosition(next)
  }

  function endPointer() {
    pointing = false
    recordActivity()
    if (view.dragging) {
      anchor = position
      update({ dragging: false })
      save()
    }
    if (view.state === 'idle') delayIdleCheck()
    schedule()
  }

  function command(action: CompanionCommand) {
    if (view.hidden || pointing) return
    if (action.type === 'move' && !validPosition(action.position)) return
    stopWalk()
    recordActivity()
    if (view.preview) update({ preview: null })
    switch (action.type) {
      case 'move':
        startWalk(action.position, true)
        break
      case 'chat':
        enter('chatting', reactionDuration.chatting)
        window.dispatchEvent(new CustomEvent(companionChatRequestedEvent))
        break
      case 'hint':
        enter('hint', reactionDuration.hint, action.text)
        break
      case 'state':
        if (action.state === 'walking') {
          if (view.paused || motion.matches || !randomWalk()) enter('idle')
          break
        }
        enter(action.state, reactionDuration[action.state])
        break
    }
  }

  function preview(state: CompanionState | null) {
    if (view.hidden || pointing) return
    stopWalk()
    update({ preview: state })
    recordActivity()
    enter(state ?? 'idle')
  }

  function setPreferences(preferences: Partial<Pick<CompanionView, 'hidden' | 'paused'>>) {
    update(preferences)
    if (view.hidden) {
      pointing = false
      hovered = false
      update({ dragging: false, preview: null })
      finishWalk(false)
    } else if (view.paused && walk) {
      finishWalk(walk.relocate)
    }
    recordActivity()
    if (view.state === 'idle') delayIdleCheck()
    save()
    schedule()
  }

  document.addEventListener('visibilitychange', syncVisibility)
  window.addEventListener('pointerdown', recordActivity, { capture: true, passive: true })
  window.addEventListener('keydown', recordActivity, { capture: true, passive: true })
  window.addEventListener('wheel', recordActivity, { capture: true, passive: true })
  window.addEventListener('pagehide', save)
  window.addEventListener('pageshow', restorePage)
  motion.addEventListener('change', syncMotion)
  setPosition(position)
  render(view)
  delayIdleCheck()
  schedule()

  return {
    syncArea, setHovered, beginPointer, dragTo, endPointer, command, preview, setPreferences,
    dispose() {
      disposed = true
      stopWalk()
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', syncVisibility)
      window.removeEventListener('pointerdown', recordActivity, true)
      window.removeEventListener('keydown', recordActivity, true)
      window.removeEventListener('wheel', recordActivity, true)
      window.removeEventListener('pagehide', save)
      window.removeEventListener('pageshow', restorePage)
      motion.removeEventListener('change', syncMotion)
    },
  }
}
