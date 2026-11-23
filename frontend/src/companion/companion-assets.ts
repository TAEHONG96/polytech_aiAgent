import type { CompanionState } from './companion-events'

// Optional matches let a missing cutout fall back without breaking the Vite build.
const images = import.meta.glob<string>('../../kiro_asset/cutouts/*.png', {
  eager: true,
  import: 'default',
  query: '?url',
})

export function companionAsset(state: CompanionState): string | undefined {
  return images[`../../kiro_asset/cutouts/${state}.png`]
}
