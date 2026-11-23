import { createContext, useContext, useEffect, useMemo } from 'react'
import type { ReactNode } from 'react'
import type { Severity, SimulationStatus, Status } from '../api'
import type { ActiveCompanionHint } from './companion-hints'

export type ResourcePhase = 'loading' | 'ready' | 'empty' | 'error'

type ContextParts = {
  run: { phase: ResourcePhase; status?: SimulationStatus; scenario?: string }
  tickets: { phase: ResourcePhase }
  ticket: { phase: ResourcePhase; id?: string; number?: string; status?: Status; severity?: Severity; kind?: string; origin?: string }
  search: { phase: ResourcePhase; kind: 'sop' | 'case'; request: number }
  analysis: { phase: ResourcePhase; id?: string }
  incoming: { phase: ResourcePhase }
  work: { editing: boolean }
  handover: { phase: ResourcePhase; status?: string; revision?: number; dirty: boolean }
  source: { phase: ResourcePhase }
}

const pages = {
  home: '내 실습', about: '서비스 소개', workspace: '근무 워크스페이스', ticket: '실습 티켓',
  'sop-list': '승인 SOP 목록', sop: '승인 SOP', incident: '과거 장애', handover: '확정 인계 리비전',
} as const

const sections = ['tickets', 'schedule', 'knowledge', 'activity', 'outgoing', 'evaluation', 'ticket-summary',
  'incidents', 'analysis', 'actions', 'work'] as const
type Section = typeof sections[number]

export type CompanionPageContext = {
  pageId: keyof typeof pages
  pathname: string
  pageTitle: string
  userRole: 'trainee'
  runId?: string
  ticketId?: string
  entity?: { type: 'ticket' | 'sop' | 'incident' | 'handover'; id: string }
  section?: Section
  resources: Partial<ContextParts>
}

const runPath = /^\/simulation-runs\/([\da-f-]{36})(?=\/|$)/i
const entityPath = /^\/(tickets|sops)\/([\da-f-]{36})(?:\/incident-sources\/([\da-f-]{36}))?$/i
const handoverPath = /^\/outgoing-handover\/revisions\/(\d+)$/

function describePage(pathname: string) {
  const run = runPath.exec(pathname)
  const suffix = run ? pathname.slice(run[0].length) : ''
  const entity = entityPath.exec(suffix)
  const revision = handoverPath.exec(suffix)?.[1]
  const type = revision ? 'handover' : entity?.[3] ? 'incident' : entity?.[1] === 'sops' ? 'sop' : 'ticket'
  const id = revision ?? entity?.[3] ?? entity?.[2]
  const pageId = id ? type : suffix === '/sops' ? 'sop-list' : run && !suffix ? 'workspace' : pathname === '/about' ? 'about' : 'home'
  return {
    pageId, pathname, pageTitle: pages[pageId], userRole: 'trainee', runId: run?.[1],
    ticketId: entity?.[1] === 'tickets' ? entity[2] : undefined,
    entity: id ? { type, id } : undefined,
  } satisfies Omit<CompanionPageContext, 'resources' | 'section'>
}

function currentSection() {
  return sections.find(section => '#' + section === window.location.hash)
}

function createContextStore(pathname: string) {
  const route = describePage(pathname)
  const resources: Partial<ContextParts> = {}
  const listeners = new Set<() => void>()
  let section = currentSection()
  let snapshot: CompanionPageContext = { ...route, section, resources: {} }
  let activeHint: ActiveCompanionHint | undefined
  let scheduled = false

  function publish() {
    if (scheduled) return
    scheduled = true
    // Effects on the same commit may publish different parts or replay in Strict Mode.
    queueMicrotask(() => {
      scheduled = false
      const next = { ...route, section, resources: { ...resources } }
      if (JSON.stringify(next) === JSON.stringify(snapshot)) return
      snapshot = next
      for (const notify of listeners) notify()
    })
  }

  return {
    getCurrentPageContext: () => snapshot,
    getActiveCompanionHint: () => activeHint,
    setActiveHint(hint: ActiveCompanionHint | undefined) { activeHint = hint },
    setPart<K extends keyof ContextParts>(key: K, part?: ContextParts[K]) {
      if (JSON.stringify(resources[key]) === JSON.stringify(part)) return
      if (part) resources[key] = part
      else delete resources[key]
      publish()
    },
    syncSection() { section = currentSection(); publish() },
    subscribe(notify: () => void) {
      listeners.add(notify)
      return () => { listeners.delete(notify) }
    },
  }
}

const StoreContext = createContext<ReturnType<typeof createContextStore> | null>(null)

export function CompanionContextProvider({ path, enabled, children }: { path: string; enabled: boolean; children: ReactNode }) {
  const store = useMemo(() => enabled ? createContextStore(path) : null, [enabled, path])
  useEffect(() => {
    if (!store) return
    window.addEventListener('hashchange', store.syncSection)
    window.addEventListener('popstate', store.syncSection)
    return () => {
      window.removeEventListener('hashchange', store.syncSection)
      window.removeEventListener('popstate', store.syncSection)
    }
  }, [store])
  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>
}

export function useCompanionContextStore() {
  return useContext(StoreContext)
}

export function useCompanionPart<K extends keyof ContextParts>(key: K, part?: ContextParts[K]) {
  const store = useCompanionContextStore()
  useEffect(() => { store?.setPart(key, part) })
  useEffect(() => () => { store?.setPart(key) }, [store, key])
}
