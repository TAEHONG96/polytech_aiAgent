import { companionChatRequestedEvent } from './companion-events'
import type { CompanionCommand, CompanionPosition, CompanionState } from './companion-events'
import { clampPosition, distance, flightStep, nearbyDestination, throwMotion, walkDuration, walkPosition } from './companion-movement'
import type { MovementArea, Wall } from './companion-movement'
import { hintWaitMs } from './companion-hints'
import type { ActiveCompanionHint, CompanionHint, TutorCommand, TutorFeedback, TutorHintResult, TutorPreference } from './companion-hints'

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
  activeHint?: ActiveCompanionHint
  interaction: 'holding' | 'dragging' | 'flying' | null
  flightAngle?: number
  impact?: Wall
  hidden: boolean
  paused: boolean
  preview: CompanionState | null
  chatOpen: boolean
  tutorEnabled: boolean
  tutorFrequency: TutorPreference
}

export const initialCompanionView: CompanionView = {
  state: 'idle', direction: 'right', interaction: null, hidden: false, paused: false, preview: null, chatOpen: false,
  tutorEnabled: true, tutorFrequency: 'normal',
}

type Preferences = Pick<CompanionView, 'hidden' | 'paused' | 'tutorEnabled' | 'tutorFrequency'>
type SavedCompanion = Preferences & Pick<CompanionView, 'direction'> & {
  position: CompanionPosition
  anchor: CompanionPosition
}

function savedPreferences(saved: Partial<SavedCompanion>): Preferences & Pick<SavedCompanion, 'direction'> {
  return {
    direction: saved.direction === 'left' ? 'left' : 'right',
    hidden: saved.hidden === true,
    paused: saved.paused === true,
    tutorEnabled: saved.tutorEnabled !== false,
    tutorFrequency: saved.tutorFrequency === 'low' || saved.tutorFrequency === 'high' ? saved.tutorFrequency : 'normal',
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

type Flight = { velocity: CompanionPosition; elapsed: number; lastFrame: number }

export function createCompanionController({ readArea, render, place, tutorFeedback }: {
  readArea: () => MovementArea
  render: (view: CompanionView) => void
  place: (position: CompanionPosition, size: number) => void
  tutorFeedback?: (offerId: string, event: TutorFeedback) => void
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
  let flight: Flight | undefined
  let frame: number | undefined
  let timer: number | undefined
  let reactionEndsAt: number | undefined
  let idleCheckAt = 0
  let lastActivity = performance.now()
  let hovered = false
  let disposed = false

  function save() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({
        position, anchor, direction: view.direction, hidden: view.hidden, paused: view.paused,
        tutorEnabled: view.tutorEnabled, tutorFrequency: view.tutorFrequency,
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
    if (flight) flight.lastFrame = performance.now()
  }

  function stopTravel() {
    cancelFrame()
    walk = undefined
    flight = undefined
  }

  function interruptTravel() {
    const wasFlying = Boolean(flight)
    stopTravel()
    if (wasFlying) {
      anchor = position
      update({ interaction: null, flightAngle: undefined })
      save()
    }
  }

  function recordActivity() {
    lastActivity = performance.now()
  }

  function delayIdleCheck() {
    idleCheckAt = performance.now() + behavior.idleMin + Math.random() * (behavior.idleMax - behavior.idleMin)
  }

  function enter(state: CompanionState, duration?: number, impact?: Wall) {
    reactionEndsAt = duration === undefined ? undefined : performance.now() + duration
    update({ state, impact })
    if (state === 'idle') delayIdleCheck()
    schedule()
  }

  function isHeld() {
    return view.interaction === 'holding' || view.interaction === 'dragging'
  }

  function canAnimate() {
    return !disposed && !document.hidden && !view.hidden && !view.paused && !view.chatOpen && !motion.matches && !isHeld()
  }

  function requestFrame() {
    if (frame === undefined && canAnimate() && (flight || (walk && !hovered))) {
      frame = window.requestAnimationFrame(step)
    }
  }

  function step(now: number) {
    frame = undefined
    if (!canAnimate()) return
    if (flight) {
      const elapsed = Math.max(0, Math.min(40, now - flight.lastFrame))
      const next = flightStep(position, flight.velocity, elapsed, area.bounds)
      flight.elapsed += elapsed
      flight.lastFrame = now
      flight.velocity = next.velocity
      setPosition(next.position)
      if (next.wall || flight.elapsed >= throwMotion.maxDuration
        || Math.hypot(next.velocity.x, next.velocity.y) < throwMotion.settleSpeed) {
        finishFlight(next.wall)
      } else {
        requestFrame()
      }
      return
    }
    if (!walk || hovered) return
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

  function finishFlight(wall?: Wall) {
    interruptTravel()
    if (wall) enter('surprised', throwMotion.impactDuration, wall)
    else enter('idle')
  }

  function finishWalk(relocate: boolean) {
    stopTravel()
    if (relocate) anchor = position
    save()
    enter('idle')
  }

  function startWalk(destination: CompanionPosition, relocate = false) {
    const target = clampPosition(destination, area.bounds)
    stopTravel()
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
      if (view.activeHint) dismissHint('EXPIRED')
      else enter(view.chatOpen ? 'chatting' : 'idle')
      return
    }
    if (view.state === 'idle' && !view.chatOpen && !view.preview && !isHeld() && !hovered && !view.paused) {
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
    const idle = view.state === 'idle' && !view.chatOpen && !view.preview && !isHeld() && !hovered && !view.paused
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
    else {
      if (flight) flight.lastFrame = performance.now()
      if (view.activeHint?.mode === 'ready' && hintExpired()) dismissHint('EXPIRED')
    }
    requestFrame()
    schedule()
  }

  function restorePage(event: PageTransitionEvent) {
    if (!event.persisted) return
    dismissHint()
    const restored = restore()
    if (!validPosition(restored.position)) return
    stopTravel()
    hovered = false
    area = readArea()
    setPosition(restored.position)
    anchor = clampPosition(validPosition(restored.anchor) ? restored.anchor : position, area.bounds)
    update({ ...savedPreferences(restored), activeHint: undefined, preview: null, interaction: null, flightAngle: undefined })
    recordActivity()
    enter('idle')
  }

  function syncMotion() {
    if (motion.matches && flight) finishFlight()
    else if (motion.matches && walk) finishWalk(walk.relocate)
    schedule()
  }

  function setHovered(next: boolean) {
    if (hovered === next) return
    hovered = next
    recordActivity()
    if (hovered && !flight) cancelFrame()
    if (!hovered && view.state === 'idle') delayIdleCheck()
    requestFrame()
    schedule()
  }

  function beginPointer() {
    interruptTravel()
    recordActivity()
    update({ interaction: 'holding', preview: null })
    if (!view.activeHint && !view.chatOpen) enter('idle')
    return position
  }

  function dragTo(next: CompanionPosition) {
    if (!isHeld()) return
    if (view.interaction !== 'dragging') {
      update({ interaction: 'dragging', activeHint: undefined })
      if (!view.chatOpen) enter('surprised')
    }
    setPosition(next)
  }

  function endPointer(velocity?: CompanionPosition) {
    if (!isHeld()) return
    const dragged = view.interaction === 'dragging'
    update({ interaction: null })
    recordActivity()
    if (view.chatOpen) {
      if (dragged) { anchor = position; save() }
      return
    }
    if (!dragged) {
      if (!view.activeHint) enter('idle')
      return
    }
    anchor = position
    save()
    if (velocity && canAnimate()) {
      flight = { velocity, elapsed: 0, lastFrame: performance.now() }
      update({
        interaction: 'flying',
        flightAngle: Math.atan2(velocity.y, velocity.x) * 180 / Math.PI,
        direction: velocity.x === 0 ? view.direction : velocity.x < 0 ? 'left' : 'right',
      })
      enter('surprised')
      requestFrame()
    } else {
      enter('surprised', reactionDuration.surprised)
    }
  }

  function hintExpired() {
    return reactionEndsAt !== undefined && performance.now() >= reactionEndsAt
  }

  function offerHint(hint: CompanionHint) {
    if (view.hidden || view.paused || view.chatOpen || view.interaction || view.preview || document.hidden) return false
    if (view.activeHint?.mode === 'ready' && hintExpired()) dismissHint('EXPIRED')
    const active = view.activeHint
    if (hint.tutor && (active || !view.tutorEnabled)) return false
    if (active && (active.mode !== 'ready' || active.hint.priority >= hint.priority)) return false
    if (active?.hint.tutor) dismissHint()
    interruptTravel()
    update({ activeHint: { hint, mode: 'ready' } })
    enter('hint', hintWaitMs)
    return true
  }

  function dismissHint(reason: TutorFeedback | 'TRANSFER' = 'INVALIDATED') {
    const active = view.activeHint
    if (!active) return
    if (active.hint.tutor && reason !== 'TRANSFER') tutorFeedback?.(active.hint.tutor.offerId, reason)
    update({ activeHint: undefined })
    enter('idle')
  }

  function activate() {
    if (view.hidden || isHeld()) return
    const active = view.activeHint
    if (!active) {
      command({ type: 'chat' })
    } else if (active.mode === 'ready') {
      if (hintExpired()) dismissHint('EXPIRED')
      else {
        recordActivity()
        if (active.hint.tutor) beginTutorRequest('view')
        else {
          update({ activeHint: { ...active, mode: 'open' } })
          enter('hint')
        }
      }
    }
  }

  function beginTutorRequest(command: TutorCommand) {
    const active = view.activeHint
    const tutor = active?.hint.tutor
    if (!tutor || active.mode === 'loading') return
    const request = command === 'retry' && tutor.retryTransport && tutor.request ? tutor.request : { id: crypto.randomUUID(), command }
    update({ activeHint: { mode: 'loading', hint: { ...active.hint, tutor: { ...tutor, request, retryTransport: false } } } })
    enter('thinking')
  }

  function finishTutorRequest(requestId: string, result: TutorHintResult, retryTransport = false) {
    const active = view.activeHint
    const tutor = active?.hint.tutor
    if (!tutor || active.mode !== 'loading' || tutor.request?.id !== requestId) return
    update({ activeHint: { mode: 'open', hint: { ...active.hint, tutor: { ...tutor, result, retryTransport } } } })
    enter('hint')
  }

  function command(action: CompanionCommand) {
    if (view.hidden || isHeld() || view.activeHint) return
    if (view.chatOpen && action.type !== 'chat') return
    if (action.type === 'move' && !validPosition(action.position)) return
    interruptTravel()
    recordActivity()
    if (view.preview) update({ preview: null })
    switch (action.type) {
      case 'move':
        startWalk(action.position, true)
        break
      case 'chat':
        if (!view.chatOpen) enter('chatting', reactionDuration.chatting)
        window.dispatchEvent(new CustomEvent(companionChatRequestedEvent, { detail: action }))
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

  function setChatPhase(phase: 'chatting' | 'thinking' | 'happy' | 'surprised' | null) {
    if (!phase && !view.chatOpen) return
    interruptTravel()
    dismissHint()
    update({ chatOpen: phase !== null, preview: null })
    const duration = phase === 'happy' || phase === 'surprised' ? reactionDuration[phase] : undefined
    enter(phase ?? 'idle', duration)
  }

  function preview(state: CompanionState | null) {
    if (view.hidden || isHeld()) return
    dismissHint()
    interruptTravel()
    update({ preview: state })
    recordActivity()
    enter(state ?? 'idle')
  }

  function setPreferences(preferences: Partial<Preferences>) {
    update(preferences)
    if (view.hidden || view.paused) dismissHint()
    if (!view.tutorEnabled && view.activeHint?.hint.tutor) dismissHint()
    if (view.hidden) {
      hovered = false
      interruptTravel()
      update({ interaction: null, preview: null })
      finishWalk(false)
    } else if (view.paused && flight) {
      finishFlight()
    } else if (view.paused && walk) {
      finishWalk(walk.relocate)
    }
    recordActivity()
    if (view.state === 'idle') delayIdleCheck()
    save()
    schedule()
  }

  function leavePage() {
    if (view.activeHint?.hint.tutor && view.activeHint.mode === 'ready') dismissHint('INVALIDATED')
    save()
  }

  document.addEventListener('visibilitychange', syncVisibility)
  window.addEventListener('pointerdown', recordActivity, { capture: true, passive: true })
  window.addEventListener('keydown', recordActivity, { capture: true, passive: true })
  window.addEventListener('wheel', recordActivity, { capture: true, passive: true })
  window.addEventListener('pagehide', leavePage)
  window.addEventListener('pageshow', restorePage)
  motion.addEventListener('change', syncMotion)
  setPosition(position)
  render(view)
  delayIdleCheck()
  schedule()

  return {
    syncArea, setHovered, beginPointer, dragTo, endPointer, command, preview, setPreferences,
    offerHint, dismissHint, activate, beginTutorRequest, finishTutorRequest, setChatPhase, getActiveHint: () => view.activeHint,
    dispose() {
      disposed = true
      stopTravel()
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', syncVisibility)
      window.removeEventListener('pointerdown', recordActivity, true)
      window.removeEventListener('keydown', recordActivity, true)
      window.removeEventListener('wheel', recordActivity, true)
      window.removeEventListener('pagehide', leavePage)
      window.removeEventListener('pageshow', restorePage)
      motion.removeEventListener('change', syncMotion)
    },
  }
}
