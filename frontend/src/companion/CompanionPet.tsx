import { useLayoutEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { companionAsset } from './companion-assets'
import { createCompanionController, initialCompanionView } from './companion-controller'
import { companionCommandEvent, companionStates } from './companion-events'
import type { CompanionPosition, CompanionState } from './companion-events'
import { distance, movementArea } from './companion-movement'
import './companion.css'

type Gesture = { id: number; start: CompanionPosition; origin: CompanionPosition; dragged: boolean }
const dragThreshold = 6
const protectedControls = 'a[href], button, input, select, textarea, label, summary, [role="button"], [contenteditable="true"]'

const assets = companionStates.flatMap(state => {
  const url = companionAsset(state)
  return url ? [{ state, url }] : []
})

function readMovementArea() {
  const sidebar = document.querySelector('.training-sidebar')
  const header = document.querySelector('.training-topbar')
  const viewport = { width: document.documentElement.clientWidth, height: window.innerHeight }
  const fixedSidebar = sidebar && getComputedStyle(sidebar).position === 'fixed'
  return movementArea(viewport, {
    left: fixedSidebar ? sidebar.getBoundingClientRect().right : 0,
    top: header?.getBoundingClientRect().height ?? 0,
  })
}

export default function CompanionPet() {
  const [view, setView] = useState(initialCompanionView)
  const [imageStatus, setImageStatus] = useState<Partial<Record<CompanionState, 'ready' | 'failed'>>>({})
  const positioner = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const controller = useRef<ReturnType<typeof createCompanionController> | null>(null)
  const gesture = useRef<Gesture | null>(null)
  const syncPointerTarget = useRef<(() => void) | null>(null)

  useLayoutEffect(() => {
    let disposed = false
    for (const image of button.current!.querySelectorAll<HTMLImageElement>('img[data-image-state]')) {
      const state = image.dataset.imageState as CompanionState
      function mark(status: 'ready' | 'failed') {
        if (!disposed) setImageStatus(current => ({ ...current, [state]: status }))
      }
      void image.decode().then(() => mark('ready'), () => mark('failed'))
    }
    return () => { disposed = true }
  }, [])

  useLayoutEffect(() => {
    const element = positioner.current!
    const target = button.current!
    let lastPointer: CompanionPosition | undefined
    let position = { x: 0, y: 0 }
    let size = 0

    function protectControls() {
      let passThrough = false
      if (lastPointer && !gesture.current && !element.hidden) {
        const { x, y } = lastPointer
        if (x >= position.x && x <= position.x + size && y >= position.y && y <= position.y + size) {
          const underlying = document.elementsFromPoint(x, y).find(node => !node.closest('.companion-widget'))
          passThrough = Boolean(underlying?.closest(protectedControls))
        }
      }
      const pointerEvents = passThrough ? 'none' : ''
      if (target.style.pointerEvents !== pointerEvents) target.style.pointerEvents = pointerEvents
      if (passThrough) controller.current?.setHovered(false)
    }

    const pet = createCompanionController({
      readArea: readMovementArea,
      render: setView,
      place(next, width) {
        position = next
        if (size !== width) {
          size = width
          element.style.setProperty('--companion-size', `${size}px`)
        }
        element.style.transform = `translate3d(${position.x}px, ${position.y}px, 0)`
        protectControls()
      },
    })
    controller.current = pet
    syncPointerTarget.current = protectControls

    function trackPointer(event: globalThis.PointerEvent) {
      lastPointer = { x: event.clientX, y: event.clientY }
      protectControls()
    }

    function syncArea() {
      pet.syncArea()
      protectControls()
    }

    const onCommand = (event: WindowEventMap[typeof companionCommandEvent]) => {
      if (event.detail) pet.command(event.detail)
    }
    const root = document.getElementById('root')!
    const resizeObserver = new ResizeObserver(syncArea)
    resizeObserver.observe(root)
    const layoutObserver = new MutationObserver(syncArea)
    layoutObserver.observe(root, { childList: true })
    window.addEventListener(companionCommandEvent, onCommand)
    window.addEventListener('resize', syncArea)
    window.addEventListener('pointermove', trackPointer, { capture: true, passive: true })
    window.addEventListener('scroll', protectControls, { capture: true, passive: true })

    return () => {
      resizeObserver.disconnect()
      layoutObserver.disconnect()
      window.removeEventListener(companionCommandEvent, onCommand)
      window.removeEventListener('resize', syncArea)
      window.removeEventListener('pointermove', trackPointer, true)
      window.removeEventListener('scroll', protectControls, true)
      pet.dispose()
      controller.current = null
      gesture.current = null
      syncPointerTarget.current = null
    }
  }, [])

  function startPointer(event: PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || gesture.current || !controller.current) return
    event.currentTarget.setPointerCapture(event.pointerId)
    gesture.current = {
      id: event.pointerId,
      start: { x: event.clientX, y: event.clientY },
      origin: controller.current.beginPointer(),
      dragged: false,
    }
  }

  function movePointer(event: PointerEvent<HTMLButtonElement>) {
    const current = gesture.current
    if (current?.id !== event.pointerId) return
    const point = { x: event.clientX, y: event.clientY }
    if (!current.dragged && distance(current.start, point) < dragThreshold) return
    current.dragged = true
    controller.current?.dragTo({
      x: current.origin.x + point.x - current.start.x,
      y: current.origin.y + point.y - current.start.y,
    })
  }

  function finishPointer(event: PointerEvent<HTMLButtonElement>) {
    const current = gesture.current
    if (current?.id !== event.pointerId) return
    const released = event.type === 'pointerup'
    if (released) movePointer(event)
    gesture.current = null
    controller.current?.endPointer()
    syncPointerTarget.current?.()
    if (released && !current.dragged) controller.current?.command({ type: 'chat' })
  }

  const isReady = (state: CompanionState) => imageStatus[state] === 'ready'
  const awaitingIdle = companionAsset('idle') && !imageStatus.idle
  const visible = isReady(view.state) ? view.state : !awaitingIdle && (
    isReady('idle') ? 'idle' : companionStates.find(isReady)
  )

  return <div className="companion-widget">
    <div className="companion-positioner" ref={positioner} hidden={view.hidden || !visible}>
      {view.hint && <p className="companion-hint" role="status">{view.hint}</p>}
      <button type="button" className="companion-pet" ref={button} aria-label="키로 컴패니언"
        data-state={view.state} data-direction={view.direction} data-paused={view.paused} data-dragging={view.dragging}
        onPointerEnter={() => controller.current?.setHovered(true)}
        onPointerLeave={() => controller.current?.setHovered(false)}
        onPointerDown={startPointer} onPointerMove={movePointer} onPointerUp={finishPointer}
        onPointerCancel={finishPointer} onLostPointerCapture={finishPointer}
        onClick={event => { if (event.detail === 0) controller.current?.command({ type: 'chat' }) }}>
        <span className="companion-hover">
          <span className="companion-animation">
            <span className="companion-facing">
              {assets.map(({ state, url }) => <img key={url} src={url} alt="" draggable={false}
                data-image-state={state} hidden={state !== visible} />)}
            </span>
          </span>
        </span>
      </button>
    </div>
    {assets.length > 0 && <details className="companion-settings">
      <summary>컴패니언</summary>
      <div className="companion-settings-panel">
        <label><input type="checkbox" checked={!view.hidden}
          onChange={event => controller.current?.setPreferences({ hidden: !event.target.checked })} />캐릭터 표시</label>
        <label><input type="checkbox" checked={view.paused}
          onChange={event => controller.current?.setPreferences({ paused: event.target.checked })} />애니메이션 정지</label>
        {import.meta.env.DEV && <label className="companion-preview">상태 미리보기
          <select value={view.preview ?? ''} disabled={view.hidden}
            onChange={event => controller.current?.preview(companionStates.find(state => state === event.target.value) ?? null)}>
            <option value="">자동</option>
            {companionStates.map(state => <option key={state} value={state}>{state}</option>)}
          </select>
        </label>}
      </div>
    </details>}
  </div>
}
