import type { CompanionPosition } from './companion-events'

const settings = {
  size: 96,
  edge: 12,
  controlClearance: 52,
  roamWidth: 280,
  roamHeight: 180,
  stepMin: 52,
  stepMax: 120,
  speed: 76,
}

type Bounds = { left: number; top: number; right: number; bottom: number }
export type MovementArea = { bounds: Bounds; size: number }
export type Wall = keyof Bounds
export type DragSample = CompanionPosition & { time: number }

export const throwMotion = {
  sampleWindow: 100,
  releaseWindow: 60,
  minSpeed: 170,
  maxSpeed: 1_650,
  drag: 5,
  settleSpeed: 30,
  maxDuration: 680,
  impactDuration: 380,
}

export function movementArea(viewport: { width: number; height: number }, insets: { left: number; top: number }): MovementArea {
  const { width, height } = viewport
  const { edge, controlClearance } = settings
  const size = Math.max(0, Math.min(settings.size, width - 2 * edge, height - edge - controlClearance))
  const right = Math.max(0, width - edge - size)
  const bottom = Math.max(0, height - controlClearance - size)

  return {
    size,
    bounds: {
      left: Math.min(right, edge + insets.left),
      top: Math.min(bottom, edge + insets.top),
      right,
      bottom,
    },
  }
}

export function clampPosition(position: CompanionPosition, bounds: Bounds): CompanionPosition {
  return {
    x: Math.min(bounds.right, Math.max(bounds.left, position.x)),
    y: Math.min(bounds.bottom, Math.max(bounds.top, position.y)),
  }
}

export function distance(from: CompanionPosition, to: CompanionPosition) {
  return Math.hypot(to.x - from.x, to.y - from.y)
}

export function nearbyDestination(from: CompanionPosition, anchor: CompanionPosition, bounds: Bounds) {
  const roam = {
    left: Math.max(bounds.left, anchor.x - settings.roamWidth),
    top: Math.max(bounds.top, anchor.y - settings.roamHeight),
    right: Math.min(bounds.right, anchor.x + settings.roamWidth),
    bottom: Math.min(bounds.bottom, anchor.y + settings.roamHeight),
  }

  for (let attempt = 0; attempt < 8; attempt++) {
    const angle = Math.random() * 2 * Math.PI
    const step = settings.stepMin + Math.random() * (settings.stepMax - settings.stepMin)
    const target = clampPosition({ x: from.x + Math.cos(angle) * step, y: from.y + Math.sin(angle) * step }, roam)
    if (distance(from, target) >= settings.stepMin / 2) return target
  }
}

export function walkDuration(from: CompanionPosition, to: CompanionPosition) {
  return distance(from, to) / settings.speed * 1000
}

export function walkPosition(from: CompanionPosition, to: CompanionPosition, progress: number): CompanionPosition {
  const eased = progress * progress * (3 - 2 * progress)
  return { x: from.x + (to.x - from.x) * eased, y: from.y + (to.y - from.y) * eased }
}

export function releaseVelocity(samples: readonly DragSample[], now: number): CompanionPosition | undefined {
  const last = samples.at(-1)
  const first = samples.find(sample => now - sample.time <= throwMotion.sampleWindow)
  if (!first || !last || first === last || now - last.time > throwMotion.releaseWindow) return

  const seconds = Math.max(16, now - first.time) / 1000
  const x = (last.x - first.x) / seconds
  const y = (last.y - first.y) / seconds
  const speed = Math.hypot(x, y)
  if (speed < throwMotion.minSpeed) return
  const scale = Math.min(1, throwMotion.maxSpeed / speed)
  return { x: x * scale, y: y * scale }
}

export function flightStep(position: CompanionPosition, velocity: CompanionPosition, milliseconds: number, bounds: Bounds) {
  const decay = Math.exp(-throwMotion.drag * milliseconds / 1000)
  const travel = (1 - decay) / throwMotion.drag
  const dx = velocity.x * travel
  const dy = velocity.y * travel
  const sideX = dx < 0 ? 'left' : 'right'
  const sideY = dy < 0 ? 'top' : 'bottom'
  const hitX = dx === 0 ? Infinity : (bounds[sideX] - position.x) / dx
  const hitY = dy === 0 ? Infinity : (bounds[sideY] - position.y) / dy
  const firstHit = Math.min(hitX, hitY)
  const fraction = Math.min(firstHit, 1)
  const next = { x: position.x + dx * fraction, y: position.y + dy * fraction }
  const wall: Wall | undefined = firstHit <= 1 ? (hitX <= hitY ? sideX : sideY) : undefined
  if (wall === 'left' || wall === 'right') next.x = bounds[wall]
  if (wall === 'top' || wall === 'bottom') next.y = bounds[wall]
  return { position: next, velocity: { x: velocity.x * decay, y: velocity.y * decay }, wall }
}
