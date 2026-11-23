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
