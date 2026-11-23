import type { CompanionState } from './companion-events'

// Optional matches let a missing source image fall back without breaking the Vite build.
const images = import.meta.glob<string>('../../kiro_asset/*.png', {
  eager: true,
  import: 'default',
  query: '?url',
})

const filenames: Record<CompanionState, string> = {
  idle: 'idle.png',
  walking: 'walking.png',
  hint: 'hint.png',
  chatting: 'chatting.png',
  thinking: 'thinking.png',
  happy: 'happy.png',
  sleeping: 'sleeping.png',
  surprised: 'suprised.png',
}

export function companionAsset(state: CompanionState): string | undefined {
  return images[`../../kiro_asset/${filenames[state]}`]
}
