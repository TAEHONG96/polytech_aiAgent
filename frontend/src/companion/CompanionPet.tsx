import { useCallback, useId, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent } from 'react'
import type { Session } from '../api'
import { trainingSourceHref } from '../training-source'
import { CompanionChat } from './CompanionChat'
import { companionAsset } from './companion-assets'
import { createCompanionController, initialCompanionView } from './companion-controller'
import { companionCommandEvent, companionStates } from './companion-events'
import type { CompanionPosition, CompanionState } from './companion-events'
import { distance, movementArea, releaseVelocity, throwMotion } from './companion-movement'
import type { DragSample } from './companion-movement'
import { useCompanionContextStore } from './companion-context'
import type { CompanionPageContext } from './companion-context'
import { contextEvents, createHintHistory, hintScope, isContextHintId, selectHints } from './companion-hints'
import type { TutorFeedback, TutorPreference } from './companion-hints'
import { sendTutorFeedback, useAdaptiveTutor } from './companion-tutor'
import './companion.css'

type Gesture = { id: number; start: CompanionPosition; origin: CompanionPosition; dragged: boolean; wasHint: boolean; samples: DragSample[] }
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

export default function CompanionPet({ session }: { session: Session }) {
  const context = useCompanionContextStore()
  const hintId = useId()
  const [view, setView] = useState(initialCompanionView)
  const [tutorBusy, setTutorBusy] = useState(false)
  const [history] = useState(createHintHistory)
  const [imageStatus, setImageStatus] = useState<Partial<Record<CompanionState, 'ready' | 'failed'>>>({})
  const positioner = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const hintPanel = useRef<HTMLDivElement>(null)
  const closeHintButton = useRef<HTMLButtonElement>(null)
  const controller = useRef<ReturnType<typeof createCompanionController> | null>(null)
  const feedback = useCallback((offerId: string, event: TutorFeedback) => sendTutorFeedback(session, offerId, event), [session])
  const feedbackRef = useRef(feedback)
  useLayoutEffect(() => { feedbackRef.current = feedback }, [feedback])
  const recordTutor = useAdaptiveTutor({ store: context, session, view, busy: tutorBusy, controller })
  const gesture = useRef<Gesture | null>(null)
  const syncPointerTarget = useRef<(() => void) | null>(null)
  const setChatPhase = useCallback<ReturnType<typeof createCompanionController>['setChatPhase']>(phase => {
    controller.current?.setChatPhase(phase)
  }, [])
  const focusPet = useCallback(() => button.current?.focus({ preventScroll: true }), [])

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
      tutorFeedback: (offerId, event) => feedbackRef.current(offerId, event),
      readArea: readMovementArea,
      render: setView,
      place(next, width) {
        position = next
        if (size !== width) {
          size = width
          element.style.setProperty('--companion-size', `${size}px`)
        }
        element.style.transform = `translate3d(${position.x}px, ${position.y}px, 0)`
        if (hintPanel.current) positionHint(hintPanel.current, element)
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

    const root = document.getElementById('root')!
    const resizeObserver = new ResizeObserver(syncArea)
    resizeObserver.observe(root)
    const layoutObserver = new MutationObserver(syncArea)
    layoutObserver.observe(root, { childList: true })
    window.addEventListener('resize', syncArea)
    window.addEventListener('pointermove', trackPointer, { capture: true, passive: true })
    window.addEventListener('scroll', protectControls, { capture: true, passive: true })

    return () => {
      resizeObserver.disconnect()
      layoutObserver.disconnect()
      window.removeEventListener('resize', syncArea)
      window.removeEventListener('pointermove', trackPointer, true)
      window.removeEventListener('scroll', protectControls, true)
      pet.dispose()
      controller.current = null
      gesture.current = null
      syncPointerTarget.current = null
    }
  }, [])

  useLayoutEffect(() => {
    const pet = controller.current!
    let previous: CompanionPageContext | undefined
    let subscribed = true

    function evaluate(manual = false) {
      const current = context?.getCurrentPageContext()
      const active = pet.getActiveHint()
      if (active && (!current || active.hint.scope !== hintScope(current)
        || (active.hint.id === 'SOP_SEARCH_EMPTY' && current.resources.search?.phase !== 'empty')
        || (active.hint.id === 'ANALYSIS_COMPLETED' && current.resources.analysis?.phase !== 'ready'))) pet.dismissHint()
      if (!current) return
      const events = contextEvents(current, manual ? undefined : previous)
      previous = current
      for (const hint of selectHints(current, events)) {
        if (history.available(hint) && pet.offerHint(hint)) {
          history.remember(hint)
          break
        }
      }
    }

    const onCommand = (event: WindowEventMap[typeof companionCommandEvent]) => {
      const action = event.detail
      if (!action) return
      if (action.type === 'hint-dismiss') pet.dismissHint()
      else if (action.type === 'hint-request') evaluate(true)
      else if (action.type === 'hint') {
        const current = context?.getCurrentPageContext()
        if (!current || !action.text.trim()) return
        const hint = { id: 'external', key: 'external|' + current.pathname, scope: hintScope(current),
          priority: 2, message: action.text.trim(), cooldownMs: 60_000 }
        if (history.available(hint) && pet.offerHint(hint)) history.remember(hint)
      } else pet.command(action)
    }
    const unsubscribe = context?.subscribe(() => evaluate())
    queueMicrotask(() => { if (subscribed) evaluate() })
    window.addEventListener(companionCommandEvent, onCommand)
    return () => {
      subscribed = false
      window.removeEventListener(companionCommandEvent, onCommand)
      unsubscribe?.()
      context?.setActiveHint(undefined)
    }
  }, [context, history])

  useLayoutEffect(() => { context?.setActiveHint(view.activeHint) }, [context, view.activeHint])

  useLayoutEffect(() => {
    if (!view.activeHint || view.activeHint.mode === 'ready' || !hintPanel.current || !positioner.current) return
    const panel = hintPanel.current
    const position = positioner.current
    const place = () => positionHint(panel, position)
    place()
    const observer = new ResizeObserver(place)
    observer.observe(panel)
    window.addEventListener('resize', place)
    if (document.activeElement === button.current) closeHintButton.current?.focus({ preventScroll: true })

    const targetId = view.activeHint.hint.targetId
    const target = targetId && (document.querySelector(`[data-companion-anchor="${CSS.escape(targetId)}"]`)
      ?? document.getElementById(targetId))
    target && target.setAttribute('data-companion-highlight', '')
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', place)
      target && target.removeAttribute('data-companion-highlight')
    }
  }, [view.activeHint])

  function closeHint() {
    controller.current?.dismissHint('DISMISSED')
    button.current?.focus({ preventScroll: true })
  }

  function askKiro(workflow = false) {
    const current = context?.getCurrentPageContext()
    const active = controller.current?.getActiveHint()
    if (!current || active?.mode !== 'open' || active.hint.scope !== hintScope(current)) return
    const tutorOfferId = active.hint.tutor?.result?.status === 'succeeded' ? active.hint.tutor.offerId : undefined
    controller.current?.dismissHint('TRANSFER')
    controller.current?.command({ type: 'chat', hintId: isContextHintId(active.hint.id) ? active.hint.id : undefined,
      tutorOfferId, suggestedMessage: workflow && tutorOfferId ? '승인 SOP를 찾아 비교하고 내가 선택한 문서를 열어줘.' : undefined })
  }

  function startPointer(event: PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || gesture.current || !controller.current) return
    const target = event.currentTarget
    const start = { x: event.clientX, y: event.clientY }
    const wasHint = Boolean(controller.current.getActiveHint())
    const origin = controller.current.beginPointer()
    const grabX = (start.x - origin.x) / Math.max(1, target.clientWidth) * 100
    target.style.setProperty('--companion-grab-x', `${Math.max(25, Math.min(75, grabX))}%`)
    target.setPointerCapture(event.pointerId)
    gesture.current = {
      id: event.pointerId,
      start,
      origin,
      dragged: false,
      wasHint,
      samples: [{ ...start, time: performance.now() }],
    }
  }

  function movePointer(event: PointerEvent<HTMLButtonElement>) {
    const current = gesture.current
    if (current?.id !== event.pointerId) return
    const point = { x: event.clientX, y: event.clientY }
    const last = current.samples.at(-1)!
    if (point.x === last.x && point.y === last.y) return
    const time = performance.now()
    current.samples = current.samples.filter(sample => time - sample.time <= throwMotion.sampleWindow)
    current.samples.push({ ...point, time })
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
    const velocity = released && current.dragged ? releaseVelocity(current.samples, performance.now()) : undefined
    controller.current?.endPointer(velocity)
    syncPointerTarget.current?.()
    if (released && !current.dragged && (!current.wasHint || controller.current?.getActiveHint())) {
      controller.current?.activate()
    }
  }

  const isReady = (state: CompanionState) => imageStatus[state] === 'ready'
  const awaitingIdle = companionAsset('idle') && !imageStatus.idle
  const visible = isReady(view.state) ? view.state : !awaitingIdle && (
    isReady('idle') ? 'idle' : companionStates.find(isReady)
  )

  const tutor = view.activeHint?.hint.tutor
  const tutorResult = tutor?.result
  const canRequestMore = tutorResult?.status === 'succeeded' && tutorResult.level < 3
  return <div className="companion-widget">
    <div className="companion-positioner" ref={positioner} hidden={view.hidden || !visible}>
      <button type="button" className="companion-pet" ref={button} aria-label="키로 컴패니언"
        data-state={view.state} data-direction={view.direction} data-paused={view.paused}
        data-interaction={view.interaction ?? undefined} data-impact={view.impact}
        data-hint={view.activeHint?.mode} data-adaptive-hint={tutor ? view.activeHint?.mode : undefined}
        aria-expanded={view.activeHint ? view.activeHint.mode !== 'ready' : undefined}
        aria-controls={view.activeHint && view.activeHint.mode !== 'ready' ? hintId : undefined}
        aria-describedby={view.activeHint?.mode === 'ready' ? hintId : undefined}
        style={{
          '--companion-flight-angle': `${view.flightAngle ?? 0}deg`,
          '--companion-impact-time': `${throwMotion.impactDuration}ms`,
        } as CSSProperties}
        onPointerEnter={() => controller.current?.setHovered(true)}
        onPointerLeave={() => controller.current?.setHovered(false)}
        onPointerDown={startPointer} onPointerMove={movePointer} onPointerUp={finishPointer}
        onPointerCancel={finishPointer} onLostPointerCapture={finishPointer}
        onClick={event => { if (event.detail === 0) controller.current?.activate() }}>
        <span className="companion-hover">
          <span className="companion-animation">
            <span className="companion-facing">
              {assets.map(({ state, url }) => <img key={url} src={url} alt="" draggable={false}
                data-image-state={state} hidden={state !== visible} />)}
            </span>
          </span>
        </span>
      </button>
      {view.activeHint?.mode === 'ready' && <span id={hintId} className="companion-hint-ready" role="status">
        {tutor ? '맞춤 힌트' : '화면 안내'}
      </span>}
      {view.activeHint && view.activeHint.mode !== 'ready' && <div id={hintId} ref={hintPanel} className="companion-hint" role="group" aria-label="키로 힌트"
        onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closeHint() } }}>
        {view.activeHint.mode === 'loading' ? <p role="status">맞춤 힌트를 만드는 중이에요.</p> : tutorResult ? <>
          {tutorResult.level > 0 && <p className="meta">맞춤 힌트 {tutorResult.level}/3단계{tutorResult.execution_mode === 'mock' ? ' · 모의 응답'
            : tutorResult.execution_mode === 'gemini' ? ' · Gemini 응답' : ''}</p>}
          <p className="companion-hint-text" role="status" tabIndex={0}>{tutorResult.answer || tutorResult.error?.message}</p>
          {tutorResult.search_mode === 'keyword' && <p className="meta">근거 검색 · 키워드 검색</p>}
          {tutorResult.sources.length > 0 && <ul>{tutorResult.sources.map(source => <li key={source.source_url}>
            <a href={trainingSourceHref(source.source_url)}>{source.title}</a>
          </li>)}</ul>}
        </> : <>
          <p className="meta">화면 안내</p>
          <p className="companion-hint-text" role="status" tabIndex={0}>{view.activeHint.hint.message}</p>
          {view.activeHint.hint.id === 'SOP_SEARCH_EMPTY' && view.tutorEnabled && view.tutorFrequency === 'high'
            && context?.getCurrentPageContext().pageId === 'ticket' &&
            <p className="meta">같은 티켓에서 5분 안에 두 번 연속 검색 결과가 없으면 맞춤 힌트가 표시될 수 있어요.</p>}
        </>}
        <div className="companion-hint-actions">
          <button type="button" className="secondary" ref={closeHintButton} onClick={closeHint}>힌트 닫기</button>
          {view.activeHint.mode === 'open' && <>
            {canRequestMore && <button type="button"
              onClick={() => controller.current?.beginTutorRequest('more')}>조금 더 알려줘</button>}
            {tutorResult?.status === 'failed' && tutorResult.can_retry && <button type="button" className="secondary"
              onClick={() => controller.current?.beginTutorRequest('retry')}>힌트 다시 요청</button>}
            {tutorResult?.status === 'succeeded' && <button type="button" className="secondary"
              onClick={() => askKiro(true)}>관련 SOP 비교 요청</button>}
            <button type="button" className={canRequestMore ? 'secondary' : undefined} onClick={() => askKiro()}>키로에게 질문</button>
          </>}
        </div>
      </div>}
    </div>
    {assets.length > 0 && <details className="companion-settings">
      <summary>컴패니언</summary>
      <div className="companion-settings-panel">
        <label><input type="checkbox" checked={!view.hidden}
          onChange={event => controller.current?.setPreferences({ hidden: !event.target.checked })} />캐릭터 표시</label>
        <label><input type="checkbox" checked={view.paused}
          onChange={event => controller.current?.setPreferences({ paused: event.target.checked })} />애니메이션 정지</label>
        <label><input type="checkbox" checked={view.tutorEnabled}
          onChange={event => controller.current?.setPreferences({ tutorEnabled: event.target.checked })} />선제적 맞춤 힌트</label>
        <label>맞춤 힌트 빈도<select disabled={!view.tutorEnabled} value={view.tutorFrequency}
          onChange={event => controller.current?.setPreferences({ tutorFrequency: event.target.value as TutorPreference })}>
          <option value="low">적음</option><option value="normal">기본</option><option value="high">많음</option>
        </select></label>
        {import.meta.env.DEV && <label className="companion-preview">상태 미리보기
          <select value={view.preview ?? ''} disabled={view.hidden}
            onChange={event => controller.current?.preview(companionStates.find(state => state === event.target.value) ?? null)}>
            <option value="">자동</option>
            {companionStates.map(state => <option key={state} value={state}>{state}</option>)}
          </select>
        </label>}
        {import.meta.env.DEV && <div className="companion-debug-hints">
          <button type="button" className="secondary" onClick={() => debugHint('normal')}>힌트 테스트</button>
          <button type="button" className="secondary" onClick={() => debugHint('priority')}>우선 힌트 테스트</button>
        </div>}
      </div>
    </details>}
    <CompanionChat session={session} hidden={view.hidden} setPhase={setChatPhase} returnFocus={focusPet}
      setTutorBusy={setTutorBusy} recordTutor={recordTutor} />
  </div>

  function debugHint(kind: 'normal' | 'priority') {
    const current = context?.getCurrentPageContext()
    if (!current) return
    const scope = hintScope(current)
    const hint = { id: kind, key: 'debug|' + kind + '|' + scope, scope, priority: kind === 'normal' ? 1 : 4,
      message: kind === 'normal' ? '테스트 힌트입니다. 현재 티켓에 기록된 증상을 확인해 보세요.' : '우선순위 테스트 힌트입니다.' }
    if (history.available(hint) && controller.current?.offerHint(hint)) history.remember(hint)
  }
}

function positionHint(panel: HTMLElement, pet: HTMLElement) {
  const margin = 12
  const gap = 8
  const { left, top, width, height } = pet.getBoundingClientRect()
  const widthLimit = document.documentElement.clientWidth - margin
  const heightLimit = window.innerHeight - margin
  panel.style.maxWidth = `${Math.max(1, widthLimit - margin)}px`
  const x = Math.max(margin, Math.min(left + (width - panel.offsetWidth) / 2, widthLimit - panel.offsetWidth))
  const above = top - panel.offsetHeight - gap
  const below = top + height + gap
  const y = above >= margin ? above : Math.max(margin, Math.min(below, heightLimit - panel.offsetHeight))
  panel.style.left = `${x - left}px`
  panel.style.top = `${y - top}px`
}
