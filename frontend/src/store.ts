// Single client store. It only mirrors what the server says (snapshot + events) plus view
// settings (who we are, which mission and which instant we look at). No business rule lives
// here: the server already filtered what this callsign may see.
import { create } from 'zustand'
import type { ConnStatus } from './api/socket'
import type {
  ChoiceKind,
  ClockState,
  Cut,
  MissionView,
  Notification,
  Position,
  ServerMessage,
  Snapshot,
} from './api/protocol'

/** Mission form at the top of the sidebar: a pilot's new mission, or a change to one still on
 * the ground. */
export interface RequestDraft {
  /** Form shown at the top of the sidebar. */
  open: boolean
  /** Mission being changed; null = a new mission. */
  editing: string | null
  launch: Position | null
  target: Position | null
  /** Which point the next map click sets; null = map clicks do nothing. */
  picking: 'launch' | 'target' | null
}

const NO_DRAFT: RequestDraft = { open: false, editing: null, launch: null, target: null, picking: null }

/** What the sidebar shows under its action bar, when no form is open. */
export type SidePanel =
  | { view: 'list' }
  | { view: 'mission'; id: string }
  | { view: 'object'; kind: 'jammer' | 'threat'; id: string }

/** EW cut form, over the sidebar panel: fields it starts with. */
export interface CutDraft {
  jammer: string | null
  mission: string | null
}

export type ThreatKind = 'radar' | 'jammer' | 'avoid'

export interface ThreatDraft {
  open: boolean
  kind: ThreatKind
  pos: Position | null
  radius_m: number
  eirp_w: number
  bands: string[]
  position_error_m: number
  picking: boolean
}

export const DEFAULT_THREAT_DRAFT: ThreatDraft = {
  open: false,
  kind: 'radar',
  pos: null,
  radius_m: 2000,
  eirp_w: 25,
  bands: ['2.4GHz'],
  position_error_m: 0,
  picking: false,
}

const LIST: SidePanel = { view: 'list' }

/** What the map (and, for a mission, the timeline) should frame after a click in the sidebar.
 * `seq` changes on every click, so a second click on the same item frames it again. */
export interface Focus {
  kind: 'mission' | 'jammer' | 'threat'
  id: string
  seq: number
}

let focusSeq = 0
const focusOn = (kind: Focus['kind'], id: string): Focus => ({ kind, id, seq: ++focusSeq })

export interface FeedItem {
  id: number
  atS: number
  notification: Notification
}

interface State {
  conn: ConnStatus
  /** Callsign we connect as, used as `actor` in commands; empty = not signed in. */
  callsign: string
  /** Why the last sign-in ended (refused callsign), shown on the sign-in screen. */
  signInError: string | null
  seq: number
  snapshot: Snapshot | null
  cuts: Record<string, Cut>
  clock: ClockState
  /** performance.now() when `clock` was last synced, to extrapolate live sim time. */
  clockSyncedAt: number
  /** Missions in view, by id, in snapshot order. */
  missions: Record<string, MissionView>
  /** Mission shown in detail (card, its zones on the map); null = none. */
  selectedMission: string | null
  feed: FeedItem[]
  lastError: string | null
  /** Instant shown on map and timeline; null = follow the live clock. */
  viewTimeS: number | null
  requestDraft: RequestDraft
  /** Sidebar panel; a form (mission or cut) shows over it and returns to it on close. */
  panel: SidePanel
  listTab: 'missions' | 'objects'
  /** EW cut form; null = closed. */
  cutDraft: CutDraft | null
  /** Enemy threat or avoid zone creation draft. */
  threatDraft: ThreatDraft
  focus: Focus | null
  /** Route choice the pointer is on (card or map), drawn on top while it stays there. */
  focusedChoice: ChoiceKind | null
  /** Route choice clicked (card or map); null = the first one, the fastest. */
  selectedChoice: ChoiceKind | null

  setConn: (conn: ConnStatus) => void
  signIn: (callsign: string) => void
  signOut: (reason: string | null) => void
  apply: (msg: ServerMessage) => void
  selectMission: (id: string | null) => void
  setViewTime: (t: number | null) => void
  setError: (reason: string | null) => void
  /** Open the new mission form, ready to pick the launch on the map (`launch` is the fallback). */
  openRequest: (launch: Position | null) => void
  /** Open the form on a mission still on the ground, with its points. */
  openEdit: (mission: string, launch: Position, target: Position) => void
  closeRequest: () => void
  setRequestDraft: (patch: Partial<RequestDraft>) => void
  /** Map click: sets the point being picked, if any; for a new mission, the target comes next. */
  pickPoint: (pos: Position) => void
  /** Show a mission in the sidebar; it also becomes the selected mission, framed on the map and
   * the timeline. */
  openMission: (id: string) => void
  /** Show an object in the sidebar, centred on the map. */
  openObject: (kind: 'jammer' | 'threat', id: string) => void
  /** Back to the lists (the selected mission stays selected on the map). */
  openList: () => void
  setListTab: (tab: 'missions' | 'objects') => void
  openCut: (draft: CutDraft) => void
  closeCut: () => void
  openThreat: (kind?: ThreatKind, pos?: Position | null) => void
  closeThreat: () => void
  setThreatDraft: (patch: Partial<ThreatDraft>) => void
  focusChoice: (kind: ChoiceKind | null) => void
  selectChoice: (kind: ChoiceKind | null) => void
}

let feedId = 0

const SESSION = {
  seq: 0,
  snapshot: null,
  cuts: {},
  missions: {},
  selectedMission: null,
  feed: [],
  lastError: null,
  viewTimeS: null,
  requestDraft: NO_DRAFT,
  panel: LIST,
  listTab: 'missions',
  cutDraft: null,
  threatDraft: DEFAULT_THREAT_DRAFT,
  focus: null,
  focusedChoice: null,
  selectedChoice: null,
} satisfies Partial<State>

export const useStore = create<State>((set, get) => ({
  ...SESSION,
  conn: 'idle',
  callsign: '',
  signInError: null,
  clock: { sim_time_s: 0, factor: 1, paused: true },
  clockSyncedAt: performance.now(),

  setConn: (conn) => set({ conn }),
  signIn: (callsign) => set({ ...SESSION, callsign, signInError: null }),
  signOut: (reason) => set({ ...SESSION, callsign: '', signInError: reason }),
  selectMission: (selectedMission) => set({ selectedMission, selectedChoice: null, focusedChoice: null }),
  setViewTime: (viewTimeS) => set({ viewTimeS }),
  setError: (lastError) => set({ lastError }),
  openRequest: (launch) => {
    const draft = get().requestDraft
    const fresh = draft.editing === null ? draft : NO_DRAFT
    set({
      requestDraft: { ...fresh, open: true, picking: 'launch', launch: fresh.launch ?? launch },
      cutDraft: null,
      threatDraft: DEFAULT_THREAT_DRAFT,
    })
  },
  openEdit: (mission, launch, target) =>
    set({
      requestDraft: { open: true, editing: mission, launch, target, picking: null },
      cutDraft: null,
      threatDraft: DEFAULT_THREAT_DRAFT,
    }),
  closeRequest: () => set({ requestDraft: NO_DRAFT }),
  setRequestDraft: (patch) => set({ requestDraft: { ...get().requestDraft, ...patch } }),
  pickPoint: (pos) => {
    const threat = get().threatDraft
    if (threat.open && threat.picking) {
      set({ threatDraft: { ...threat, pos, picking: false } })
      return
    }
    const draft = get().requestDraft
    if (!draft.picking) return
    const next = draft.picking === 'launch' && draft.editing === null && !draft.target ? 'target' : null
    set({ requestDraft: { ...draft, [draft.picking]: pos, picking: next } })
  },
  openMission: (id) =>
    set({ panel: { view: 'mission', id }, selectedMission: id, focus: focusOn('mission', id) }),
  openObject: (kind, id) => set({ panel: { view: 'object', kind, id }, focus: focusOn(kind, id) }),
  openList: () => set({ panel: LIST }),
  setListTab: (listTab) => set({ listTab }),
  openCut: (cutDraft) => set({ cutDraft, requestDraft: NO_DRAFT, threatDraft: DEFAULT_THREAT_DRAFT }),
  closeCut: () => set({ cutDraft: null }),
  openThreat: (kind = 'radar', pos = null) =>
    set({
      threatDraft: { ...DEFAULT_THREAT_DRAFT, open: true, kind, pos, picking: pos === null },
      cutDraft: null,
      requestDraft: NO_DRAFT,
    }),
  closeThreat: () => set({ threatDraft: DEFAULT_THREAT_DRAFT }),
  setThreatDraft: (patch) => set({ threatDraft: { ...get().threatDraft, ...patch } }),
  focusChoice: (focusedChoice) => set({ focusedChoice }),
  selectChoice: (selectedChoice) => set({ selectedChoice }),

  apply: (msg) => {
    const p = msg.payload
    // Every envelope carries the server sim time: resync the extrapolated clock on each one.
    const clockSync = {
      clock: { ...get().clock, sim_time_s: msg.sim_time_s },
      clockSyncedAt: performance.now(),
      seq: msg.seq,
    }
    const patchMission = (id: string, patch: Partial<MissionView>) => {
      const current = get().missions[id]
      if (!current) return clockSync // not in view: the server should not have sent it
      return { ...clockSync, missions: { ...get().missions, [id]: { ...current, ...patch } } }
    }
    switch (p.type) {
      case 'snapshot': {
        const missions = Object.fromEntries(p.missions.map((m) => [m.constraints.mission, m]))
        const kept = get().selectedMission
        set({
          ...clockSync,
          snapshot: p,
          cuts: Object.fromEntries(p.cuts.map((c) => [c.id, c])),
          clock: p.clock,
          missions,
          // A pilot has one mission; others start on the first one, and keep their choice on
          // reconnect.
          selectedMission: kept && kept in missions ? kept : (p.missions[0]?.constraints.mission ?? null),
        })
        return
      }
      case 'clock':
        set({ ...clockSync, clock: p.clock })
        return
      case 'cut.scheduled': {
        // The cut form that asked for it closes.
        const own = p.cut.requested_by === get().callsign && get().cutDraft !== null
        set({ ...clockSync, cuts: { ...get().cuts, [p.cut.id]: p.cut }, ...(own && { cutDraft: null }) })
        return
      }
      case 'cut.confirmed':
        set({ ...clockSync, cuts: { ...get().cuts, [p.cut.id]: p.cut } })
        return
      case 'cut.cancelled': {
        const cuts = { ...get().cuts }
        delete cuts[p.cut.id]
        set({ ...clockSync, cuts })
        return
      }
      case 'mission.created': {
        // A new snapshot follows: it carries the mission and what it brings into view, and
        // keeps the selection. The pilot who asked for it moves on to it.
        const own = p.mission.pilot_callsign === get().callsign
        const opened = { selectedMission: p.mission.id, panel: { view: 'mission', id: p.mission.id } as const }
        set(own ? { ...clockSync, ...opened, requestDraft: NO_DRAFT } : clockSync)
        return
      }
      case 'mission.updated': {
        // A new snapshot follows. The form that sent the change closes.
        const editing = get().requestDraft.editing === p.mission.id
        set(editing ? { ...clockSync, requestDraft: NO_DRAFT } : clockSync)
        return
      }
      case 'threat.added':
      case 'threat.removed':
        // A new snapshot follows with the zones, grids and threats in view; the
        // notification reaches the feed.
        set(clockSync)
        return
      case 'mission.status':
        set(patchMission(p.mission, { status: p.status }))
        return
      case 'notification':
        set({
          ...clockSync,
          feed: [{ id: ++feedId, atS: msg.sim_time_s, notification: p }, ...get().feed].slice(0, 100),
        })
        return
      case 'plan.updated':
        set(patchMission(p.mission, { plan: p }))
        return
      case 'route.request':
        // A refusal puts the detour back on top: drop the selection that pointed at the refused route.
        set({
          ...patchMission(p.request.mission, { request: p.request }),
          ...(p.request.status === 'refused' ? { selectedChoice: null, focusedChoice: null } : {}),
        })
        return
      case 'drone.state':
        set(patchMission(p.mission, { drone: p }))
        return
      case 'jammer.restarted':
        // The map derives jammer state from the cuts; the notification reaches the feed.
        set(clockSync)
        return
      case 'ack':
        set({ ...clockSync, lastError: null })
        return
      case 'error':
        set({ ...clockSync, lastError: p.reason })
        return
    }
  },
}))

/** The mission shown in detail, or null. */
export function useSelectedMission(): MissionView | null {
  return useStore((s) => (s.selectedMission ? (s.missions[s.selectedMission] ?? null) : null))
}

/** Live simulated time, extrapolated from the last sync. */
export function liveSimTime(clock: ClockState, syncedAt: number, now = performance.now()): number {
  if (clock.paused) return clock.sim_time_s
  return clock.sim_time_s + ((now - syncedAt) / 1000) * clock.factor
}
