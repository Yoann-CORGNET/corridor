// All map code lives here, framework-free. React only creates the view and calls its setters.
// Setters can be called before the style is loaded: the latest data is applied on load.
import * as maplibregl from 'maplibre-gl'
import type { GeoJSONSource, ImageSource, Map as MlMap, StyleSpecification } from 'maplibre-gl'
import type { Feature, FeatureCollection } from 'geojson'
import 'maplibre-gl/dist/maplibre-gl.css'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import type { ChoiceKind, Grid, PlanUpdated, Position, RouteChoice, RoutePoint, Scenario, Zone } from '../api/protocol'
import { durationLabel, pointAlong } from '../domain/view'
import { boundsOf, gridBounds, type ZoneState } from '../domain/view'
import { levelColor, levelIndex } from './levels'

// Draft basemap: online OSM raster tiles. TODO: offline PMTiles extract of the demo area.
const STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#1a1b1e' } },
    {
      id: 'osm',
      type: 'raster',
      source: 'osm',
      paint: { 'raster-saturation': -0.7, 'raster-brightness-max': 0.65 },
    },
  ],
}

/** Marker colour of an enemy threat, by type. */
const THREAT_COLOR = { radar: '#fd7e14', jammer: '#9c36b5', avoid: '#fab005' } as const

export const COLORS = {
  friendlyOn: '#e03131',
  friendlyCut: '#2f9e44',
  friendlyPending: '#f08c00',
  enemy: '#9c36b5',
  radar: '#fd7e14',
  avoid: '#fab005',
  baseline: '#adb5bd',
  optimised: '#22b8cf',
  preview: '#f08c00',
  fallback: '#fa5252',
  drone: '#ffffff',
}

/** Heatmap classes, as the server paints them (``corridor.domain.heatmap``), for the legend. */
export const HEATMAP_COLORS = {
  safe: '#20c997',
  caution: '#fab005',
  denied: '#e03131',
  radar: '#fd7e14',
  /** 3D: out of the pilot's reach (its signal below the drone's minimum). */
  link: '#868e96',
}

/** Colour of each route choice, on the map and in the mission card. */
export const CHOICE_COLORS: Record<ChoiceKind, string> = {
  fastest: '#be4bdb',
  one_cut: '#f08c00',
  safe: '#22b8cf',
}

/** Route choices that are not highlighted, as Google Maps draws alternatives. */
const CHOICE_ALT = '#8a96a3'

/** Where each kind puts its bubble along its route, so bubbles of overlapping routes do not
 * cover each other. */
const BUBBLE_AT: Record<ChoiceKind, number> = { fastest: 0.3, one_cut: 0.5, safe: 0.62 }

/** Callout on a route choice: flight time, and what it saves against the detour. */
function bubble(
  map: MlMap,
  c: RouteChoice,
  highlighted: boolean,
  refused: boolean,
  hover: (kind: ChoiceKind | null) => void,
  click: (kind: ChoiceKind) => void,
): maplibregl.Marker {
  const el = document.createElement('div')
  el.className = highlighted ? 'choice-bubble highlighted' : 'choice-bubble'
  if (highlighted) el.style.setProperty('--bubble', CHOICE_COLORS[c.kind])
  const time = document.createElement('div')
  time.className = 'choice-bubble-time'
  time.textContent = durationLabel(c.flight_s)
  const sub = document.createElement('div')
  sub.className = 'choice-bubble-sub'
  const n = c.cuts.length
  const cuts = n === 0 ? 'no cut' : n + (n > 1 ? ' cuts' : ' cut')
  const parts = c.saved_s > 0 ? ['−' + durationLabel(c.saved_s), cuts] : [cuts]
  if (c.radars.length > 0) parts.push('through ' + c.radars.join(', '))
  if (refused) parts.unshift('refused')
  sub.textContent = parts.join(' · ')
  el.append(time, sub)
  el.addEventListener('mouseenter', () => hover(c.kind))
  el.addEventListener('mouseleave', () => hover(null))
  el.addEventListener('click', (e) => {
    e.stopPropagation()
    click(c.kind)
  })
  const at = pointAlong(c.route, BUBBLE_AT[c.kind])
  return new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -6] }).setLngLat([at.lon, at.lat]).addTo(map)
}

/** Colour of the planned route for each plan status. */
export const ROUTE_COLORS: Record<PlanUpdated['status'], string> = {
  baseline: COLORS.optimised,
  approved: COLORS.optimised,
  preview: COLORS.preview,
  fallback: COLORS.fallback,
  straight: COLORS.preview,
}

type FC = FeatureCollection
const empty = (): FC => ({ type: 'FeatureCollection', features: [] })

export interface LayerVisibility {
  zones?: boolean
  baseline?: boolean
  grid?: boolean
  rf?: boolean
  heatmap?: boolean
  relief?: boolean
  buildings?: boolean
}

/** Building fill by height (m): darker low, brighter high, so towers stand out on the dark map. */
const BUILDING_COLOR: maplibregl.ExpressionSpecification = [
  'interpolate', ['linear'], ['get', 'height_m'],
  0, '#343a40', 20, '#495057', 60, '#868e96', 150, '#ced4da', 300, '#f1f3f5',
]

export interface Scene {
  /** Only what this callsign may see. */
  scenario: Scenario
  /** Grid of every mission in view; the map fits them all. */
  grids: Grid[]
  /** Grid of the selected mission. */
  missionGrid: Grid | null
  /** Zones of the selected mission: only its zones to avoid are drawn. Jammers and radars are
   * points; their reach is the Terrain RF layer, what the planner avoids the heatmap. */
  missionZones: Zone[]
  /** Missions whose route, launch and target open the mission form on a click (on the
   * ground, this viewer is their pilot). */
  movable: string[]
}

/** Which point a marker stands for: of a mission, or of the form (`mission` null). */
export interface MissionPoint {
  mission: string | null
  point: 'launch' | 'target'
}

export interface MissionRoutes {
  mission: string
  baseline: RoutePoint[] | null
  optimised: RoutePoint[] | null
  /** The selected mission shows its detour too, and stands out. */
  selected: boolean
  /** Routes its pilot may still pick; drawn instead of the planned route while non-empty, the
   * highlighted one in colour, the others in grey, each with a bubble (time, saving). */
  choices?: RouteChoice[]
  highlight?: ChoiceKind | null
  /** Choice the command refused: grey, its bubble says so. */
  refused?: ChoiceKind | null
}

export interface DroneMark {
  mission: string
  pos: Position
  selected: boolean
}

export interface RequestPoints {
  launch: Position | null
  target: Position | null
  /** Crosshair cursor while a point is being picked. */
  picking: boolean
}

export interface ThreatDraftPoints {
  pos: Position | null
  kind: 'radar' | 'jammer' | 'avoid'
  picking: boolean
}

/** J/S heatmap from `/api/rf/heatmap`: a PNG and its corners (NW, NE, SE, SW as [lon, lat]). */
export interface RfHeatmap {
  image: string
  coordinates: [[number, number], [number, number], [number, number], [number, number]]
}

export interface MapView {
  setScene(scene: Scene): void
  /** Points of the mission request being filled in. */
  setRequest(points: RequestPoints): void
  /** Preview marker and picking cursor for a threat draft. */
  setThreatDraft(draft: ThreatDraftPoints): void
  onClick(handler: (pos: Position) => void): void
  /** Click on the route, launch or target of a mission this viewer may change. */
  onMissionClick(handler: (mission: string) => void): void
  setZoneStates(states: Record<string, ZoneState>): void
  setRoutes(routes: MissionRoutes[]): void
  /** Pointer on (or off, null) a route choice bubble, and a click on one. */
  onChoice(hover: (kind: ChoiceKind | null) => void, click: (kind: ChoiceKind) => void): void
  setDrones(drones: DroneMark[]): void
  setRfCoverage(coverage: FeatureCollection | null): void
  setRfHeatmap(heatmap: RfHeatmap | null): void
  onHover(cb: (pos: Position | null) => void): void
  setLayerVisibility(vis: LayerVisibility): void
  set3D(enabled: boolean): void
  is3D(): boolean
  fitScene(): void
  /** Frames these points: centred on one (zoom kept), fitted around several. */
  frame(points: Position[]): void
  centerOnDrone(pos?: Position | null): void
  destroy(): void
}

// MapLibre resolves its worker relative to its own module, which breaks once Vite bundles it.
maplibregl.setWorkerUrl(workerUrl)

export function createMapView(container: HTMLElement): MapView {
  const map = new maplibregl.Map({
    container,
    style: STYLE,
    center: [0, 0],
    zoom: 1,
    attributionControl: { compact: true },
    maxPitch: 85,
  })
  map.addControl(new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }), 'top-left')
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left')

  let loaded = false
  let is3dMode = false
  const pending: Record<string, FC> = {}
  let heatmap: RfHeatmap | null = null
  let scene: Scene | null = null
  let fitted = ''
  let states: Record<string, ZoneState> = {}
  let visibility: Required<LayerVisibility> = { grid: true, baseline: true, zones: true, rf: true, heatmap: false, relief: true, buildings: true }
  const markers: maplibregl.Marker[] = []
  const threatMarkers = new Map<string, maplibregl.Marker>()
  let friendlyIds = new Set<string>()
  const drones = new Map<string, maplibregl.Marker>()
  const requestMarkers: maplibregl.Marker[] = []
  const threatDraftMarkers: maplibregl.Marker[] = []
  let requestPicking = false
  let threatPicking = false
  const updateCursor = () => {
    map.getCanvas().style.cursor = requestPicking || threatPicking ? 'crosshair' : ''
  }
  let clickHandler: ((pos: Position) => void) | null = null
  let missionClickHandler: ((mission: string) => void) | null = null
  let movableIds = new Set<string>()
  /** Label marker; clickable when `move` names a mission point. */
  const point = (pos: Position, text: string, color: string, move?: MissionPoint) => {
    // Never draggable: a point moved by accident moves the mission. A click on a mission's
    // point opens its form, which sets points from GPS coordinates or a pick on the map.
    const marker = label(map, pos, text, color)
    const mission = move?.mission ?? null
    if (mission !== null) {
      marker.getElement().classList.add('map-label-clickable')
      marker.getElement().addEventListener('click', (e) => {
        e.stopPropagation()
        missionClickHandler?.(mission)
      })
    }
    return marker
  }
  map.on('click', (e) => clickHandler?.({ lat: e.lngLat.lat, lon: e.lngLat.lng }))
  const choiceBubbles: maplibregl.Marker[] = []
  let choiceHover: (kind: ChoiceKind | null) => void = () => {}
  let choiceClick: (kind: ChoiceKind) => void = () => {}

  const setData = (source: string, data: FC) => {
    pending[source] = data
    if (loaded) (map.getSource(source) as GeoJSONSource | undefined)?.setData(data)
  }

  const applyVisibility = () => {
    if (!loaded) return
    const v = (on: boolean) => (on ? 'visible' : 'none')
    map.setLayoutProperty('hillshade-relief', 'visibility', v(visibility.relief))
    map.setLayoutProperty('buildings-fill', 'visibility', v(visibility.buildings))
    map.setLayoutProperty('grid-line', 'visibility', v(visibility.grid))
    map.setLayoutProperty('route-baseline', 'visibility', v(visibility.baseline))
    map.setLayoutProperty('zones-fill', 'visibility', v(visibility.zones))
    map.setLayoutProperty('zones-line', 'visibility', v(visibility.zones))
    map.setLayoutProperty('rf-coverage-fill', 'visibility', v(visibility.rf))
    map.setLayoutProperty('rf-coverage-line', 'visibility', v(visibility.rf))
    // The heatmap covers the mission grid only: no green "safe" past it.
    map.setLayoutProperty('rf-safe-baseline', 'visibility', 'none')
    if (map.getLayer('rf-heatmap')) {
      map.setLayoutProperty('rf-heatmap', 'visibility', v(visibility.heatmap))
    }
  }

  // An image source needs its image at creation: added on the first heatmap, removed on null.
  const applyHeatmap = () => {
    if (!loaded) return
    const source = map.getSource('rf-heatmap') as ImageSource | undefined
    if (!heatmap) {
      if (map.getLayer('rf-heatmap')) map.removeLayer('rf-heatmap')
      if (source) map.removeSource('rf-heatmap')
      return
    }
    const image = { url: heatmap.image, coordinates: heatmap.coordinates }
    if (source) {
      source.updateImage(image)
      return
    }
    map.addSource('rf-heatmap', { type: 'image', ...image })
    // Below the grid. The server already rounds the cells' edges; linear resampling keeps them
    // smooth when zoomed in.
    map.addLayer(
      {
        id: 'rf-heatmap',
        type: 'raster',
        source: 'rf-heatmap',
        layout: { visibility: visibility.heatmap ? 'visible' : 'none' },
        paint: { 'raster-resampling': 'linear', 'raster-fade-duration': 0 },
      },
      'grid-line',
    )
  }

  map.on('error', (e) => {
    // Suppress non-critical tile loading errors (e.g. offline fallback for remote terrain)
    if (e.error?.message?.includes('terrarium') || e.error?.message?.includes('tile')) {
      return
    }
    console.warn('MapLibre warning:', e.error || e)
  })

  map.on('load', () => {
    // 1. Add 3D Terrarium raster-dem source
    map.addSource('terrain-dem', {
      type: 'raster-dem',
      tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
      tileSize: 256,
      encoding: 'terrarium',
      maxzoom: 15,
    })

    // 2. Global safe airspace baseline layer covering entire Earth in emerald green
    map.addSource('rf-safe-baseline', {
      type: 'geojson',
      data: {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            properties: {},
            geometry: {
              type: 'Polygon',
              coordinates: [
                [
                  [-180, -85],
                  [180, -85],
                  [180, 85],
                  [-180, 85],
                  [-180, -85],
                ],
              ],
            },
          },
        ],
      },
    })
    map.addLayer({
      id: 'rf-safe-baseline',
      type: 'fill',
      source: 'rf-safe-baseline',
      paint: {
        'fill-color': 'rgba(32, 201, 151, 0.35)', // Translucent emerald green
        'fill-opacity': 0.85,
      },
    })

    for (const id of ['grid', 'zones', 'routes', 'rf-coverage']) {
      map.addSource(id, { type: 'geojson', data: pending[id] ?? empty() })
    }

    // 3. Shaded relief (hillshade) layer: casts real sunlight and shadows on terrain features
    map.addLayer({
      id: 'hillshade-relief',
      type: 'hillshade',
      source: 'terrain-dem',
      paint: {
        'hillshade-exaggeration': 0.85,
        'hillshade-shadow-color': '#090d16',
        'hillshade-highlight-color': '#ffffff',
        'hillshade-accent-color': '#1e293b',
        'hillshade-illumination-direction': 315,
        'hillshade-illumination-altitude': 45,
      },
    })

    // Buildings of the scenario (empty without), coloured by height; served by the backend.
    map.addSource('buildings', { type: 'geojson', data: '/api/buildings' })
    map.addLayer({
      id: 'buildings-fill',
      type: 'fill',
      source: 'buildings',
      paint: { 'fill-color': BUILDING_COLOR, 'fill-opacity': 0.75 },
    })

    map.addLayer({
      id: 'grid-line',
      type: 'line',
      source: 'grid',
      paint: {
        'line-color': '#868e96',
        'line-width': ['case', ['get', 'selected'], 1.5, 1],
        'line-opacity': ['case', ['get', 'selected'], 1, 0.4],
        'line-dasharray': [4, 4],
      },
    })
    map.addLayer({
      id: 'rf-coverage-fill',
      type: 'fill',
      source: 'rf-coverage',
      paint: { 'fill-color': ['get', 'fill'], 'fill-opacity': ['get', 'fill_opacity'] },
    })
    map.addLayer({
      id: 'rf-coverage-line',
      type: 'line',
      source: 'rf-coverage',
      paint: { 'line-color': ['get', 'stroke'], 'line-width': ['get', 'stroke_width'] },
    })
    map.addLayer({
      id: 'zones-fill',
      type: 'fill',
      source: 'zones',
      paint: { 'fill-color': ['get', 'color'], 'fill-opacity': ['get', 'opacity'] },
    })
    map.addLayer({
      id: 'zones-line',
      type: 'line',
      source: 'zones',
      paint: { 'line-color': ['get', 'color'], 'line-width': 2 },
    })
    map.addLayer({
      id: 'route-baseline',
      type: 'line',
      source: 'routes',
      filter: ['==', ['get', 'kind'], 'baseline'],
      paint: { 'line-color': COLORS.baseline, 'line-width': 3, 'line-dasharray': [2, 2] },
    })
    // Google Maps style: alternatives in grey underneath, the highlighted route in colour on
    // top with a dark casing.
    map.addLayer({
      id: 'route-choices-alt',
      type: 'line',
      source: 'routes',
      filter: ['all', ['==', ['get', 'kind'], 'choice'], ['!', ['get', 'highlighted']]],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': CHOICE_ALT, 'line-width': 5, 'line-opacity': 0.9 },
    })
    map.addLayer({
      id: 'route-choices-casing',
      type: 'line',
      source: 'routes',
      filter: ['all', ['==', ['get', 'kind'], 'choice'], ['get', 'highlighted']],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#0b1d2a', 'line-width': 10 },
    })
    map.addLayer({
      id: 'route-choices',
      type: 'line',
      source: 'routes',
      filter: ['all', ['==', ['get', 'kind'], 'choice'], ['get', 'highlighted']],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': 6 },
    })
    map.addLayer({
      id: 'route-optimised',
      type: 'line',
      source: 'routes',
      filter: ['==', ['get', 'kind'], 'optimised'],
      paint: {
        'line-color': ['coalesce', ['get', 'color'], COLORS.optimised],
        'line-width': ['case', ['get', 'selected'], 4, 2.5],
        'line-opacity': ['case', ['get', 'selected'], 1, 0.6],
      },
    })
    // A click on a route of a mission this viewer may change opens its form.
    for (const layer of ['route-optimised', 'route-choices', 'route-choices-alt']) {
      map.on('click', layer, (e) => {
        const mission = e.features?.[0]?.properties?.mission
        if (typeof mission === 'string' && movableIds.has(mission)) missionClickHandler?.(mission)
      })
      map.on('mouseenter', layer, (e) => {
        const mission = e.features?.[0]?.properties?.mission
        if (typeof mission === 'string' && movableIds.has(mission)) map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', layer, () => {
        map.getCanvas().style.cursor = ''
      })
    }
    loaded = true
    applyHeatmap()
    if (is3dMode) {
      map.setTerrain({ source: 'terrain-dem', exaggeration: 2.8 })
    }
    applyVisibility()
  })

  const zoneFeatures = (zones: Zone[], outer: number[][] | null): Feature[] => {
    const worldOuter = [
      [-180, -85],
      [180, -85],
      [180, 85],
      [-180, 85],
      [-180, -85],
    ]
    return zones.map((z) => {
      const ring = [...z.ring.map((p) => [p.lon, p.lat]), [z.ring[0].lon, z.ring[0].lat]]
      const { color, opacity } = zoneStyle(z, states[z.threat] ?? 'on')
      // forbidden "outside": everything but the ring
      const coordinates = z.forbidden === 'outside' ? [outer ?? worldOuter, ring] : [ring]
      return {
        type: 'Feature',
        properties: { threat: z.threat, color, opacity },
        geometry: { type: 'Polygon', coordinates },
      }
    })
  }

  const renderZones = () => {
    if (!scene) return
    const g = scene.missionGrid
    const outer = g ? [...gridBounds(g), gridBounds(g)[0]] : null
    const avoid = scene.missionZones.filter((z) => z.kind === 'avoid')
    setData('zones', { type: 'FeatureCollection', features: zoneFeatures(avoid, outer) })
  }

  /** Threat points follow the Zones toggle; a friendly jammer's dot takes its cut state. */
  const renderThreats = () => {
    for (const [id, marker] of threatMarkers) {
      const el = marker.getElement()
      el.style.display = visibility.zones ? '' : 'none'
      if (friendlyIds.has(id)) el.style.background = kindStyle('friendly_jammer', states[id] ?? 'on').color
    }
  }

  return {
    setScene(next) {
      scene = next
      const { scenario, grids, missionGrid } = next
      setData('grid', {
        type: 'FeatureCollection',
        features: grids.map((g) => {
          const b = gridBounds(g)
          return {
            type: 'Feature',
            properties: { selected: g === missionGrid },
            geometry: { type: 'LineString', coordinates: [...b, b[0]] },
          }
        }),
      })
      renderZones()
      // Fit once per set of missions: not on every selection, nor when a mission's points move
      // (the map would jump under the pointer). The fit button refits on demand.
      const box = boundsOf(grids)
      const key = scenario.missions.map((m) => m.id).join(',')
      if (box && key !== fitted) {
        fitted = key
        map.fitBounds(box, { padding: 30, duration: 0 })
      }

      markers.splice(0).forEach((m) => m.remove())
      movableIds = new Set(scene.movable)
      for (const m of scenario.missions) {
        const movable = scene.movable.includes(m.id)
        markers.push(point(m.launch, `${m.pilot_callsign} (${m.id})`, '#4dabf7', movable ? { mission: m.id, point: 'launch' } : undefined))
        markers.push(point(m.target, `TARGET ${m.id}`, '#ffd43b', movable ? { mission: m.id, point: 'target' } : undefined))
      }
      threatMarkers.forEach((m) => m.remove())
      threatMarkers.clear()
      friendlyIds = new Set(scenario.friendly_jammers.map((j) => j.id))
      for (const j of scenario.friendly_jammers) threatMarkers.set(j.id, label(map, j.pos, `${j.id} · ${j.operator_callsign}`, COLORS.friendlyOn))
      for (const t of scenario.enemy_threats) threatMarkers.set(t.id, label(map, t.pos, t.id, THREAT_COLOR[t.type]))
      renderThreats()
    },
    setRequest({ launch, target, picking }) {
      requestMarkers.splice(0).forEach((m) => m.remove())
      if (launch) requestMarkers.push(point(launch, 'NEW LAUNCH', '#4dabf7', { mission: null, point: 'launch' }))
      if (target) requestMarkers.push(point(target, 'NEW TARGET', '#ffd43b', { mission: null, point: 'target' }))
      requestPicking = picking
      updateCursor()
    },
    setThreatDraft({ pos, kind, picking }) {
      threatDraftMarkers.splice(0).forEach((m) => m.remove())
      if (pos) {
        const color = kind === 'radar' ? COLORS.radar : kind === 'jammer' ? COLORS.enemy : COLORS.avoid
        threatDraftMarkers.push(point(pos, `NEW ${kind.toUpperCase()}`, color))
      }
      threatPicking = picking
      updateCursor()
    },
    onClick(handler) {
      clickHandler = handler
    },
    onMissionClick(handler) {
      missionClickHandler = handler
    },
    setZoneStates(s) {
      states = s
      renderThreats()
    },
    setRoutes(routes) {
      const levels = scene?.scenario.terrain.levels_m ?? []
      const line = (kind: string, r: RoutePoint[] | null, mission: string, selected: boolean): Feature[] => {
        if (!r || r.length < 2) return []
        const flat = kind !== 'optimised' || r.every((p) => p.alt_m === null || p.alt_m === undefined)
        if (flat) {
          return [{ type: 'Feature', properties: { kind, mission, selected }, geometry: { type: 'LineString', coordinates: r.map((p) => [p.lon, p.lat]) } }]
        }
        // 3D: one segment per leg, coloured by its flight level (a climb is a leg in place).
        const legs: Feature[] = []
        for (let i = 0; i + 1 < r.length; i++) {
          const a = r[i]
          const b = r[i + 1]
          const index = Math.max(levelIndex(levels, a.alt_m), levelIndex(levels, b.alt_m))
          const color = index >= 0 ? levelColor(index) : undefined
          legs.push({
            type: 'Feature',
            properties: { kind, mission, selected, color },
            geometry: { type: 'LineString', coordinates: [[a.lon, a.lat], [b.lon, b.lat]] },
          })
        }
        return legs
      }
      const choice = (r: MissionRoutes): Feature[] =>
        (r.choices ?? [])
          .filter((c) => c.route.length > 1)
          .map((c) => ({
            type: 'Feature',
            properties: { kind: 'choice', mission: r.mission, color: CHOICE_COLORS[c.kind], highlighted: c.kind === r.highlight },
            geometry: { type: 'LineString', coordinates: c.route.map((p) => [p.lon, p.lat]) },
          }))
      choiceBubbles.splice(0).forEach((m) => m.remove())
      for (const r of routes) {
        for (const c of r.choices ?? []) {
          if (c.route.length < 2) continue
          choiceBubbles.push(
            bubble(map, c, c.kind === r.highlight, c.kind === r.refused, (k) => choiceHover(k), (k) => choiceClick(k)),
          )
        }
      }
      // While the pilot picks, its route choices; otherwise the planned route (per level in 3D).
      const features = routes.flatMap((r) =>
        r.choices && r.choices.length > 0
          ? choice(r)
          : [
              ...(r.selected ? line('baseline', r.baseline, r.mission, true) : []),
              ...line('optimised', r.optimised, r.mission, r.selected),
            ],
      )
      setData('routes', { type: 'FeatureCollection', features })
    },
    setDrones(next) {
      const seen = new Set<string>()
      for (const d of next) {
        seen.add(d.mission)
        let marker = drones.get(d.mission)
        if (!marker) {
          const el = document.createElement('div')
          el.className = 'drone-marker'
          el.title = d.mission
          const pulse = document.createElement('span')
          pulse.className = 'drone-pulse'
          const glyph = document.createElement('div')
          glyph.className = 'drone-glyph'
          glyph.innerHTML =
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="#22b8cf" stroke="#0b0f17" stroke-width="2" stroke-linejoin="round"><path d="M12 2 20 20l-8-4-8 4 8-18z"/></svg>'
          el.append(pulse, glyph)
          marker = new maplibregl.Marker({ element: el }).setLngLat([d.pos.lon, d.pos.lat]).addTo(map)
          drones.set(d.mission, marker)
        }
        marker.getElement().classList.toggle('other', !d.selected)
        marker.getElement().classList.toggle('drone-live', true)
        marker.setLngLat([d.pos.lon, d.pos.lat])
      }
      for (const [mission, marker] of drones) {
        if (!seen.has(mission)) {
          marker.remove()
          drones.delete(mission)
        }
      }
    },
    setRfCoverage(coverage) {
      setData('rf-coverage', coverage ?? empty())
    },
    setRfHeatmap(next) {
      heatmap = next
      applyHeatmap()
    },
    onHover(cb) {
      map.on('mousemove', (e) => {
        cb({ lat: e.lngLat.lat, lon: e.lngLat.lng })
      })
      map.on('mouseout', () => cb(null))
    },
    setLayerVisibility(vis) {
      visibility = { ...visibility, ...vis }
      applyVisibility()
      renderThreats()
    },
    set3D(enabled: boolean) {
      is3dMode = enabled
      if (!loaded) return
      if (enabled) {
        map.setTerrain({ source: 'terrain-dem', exaggeration: 2.8 })
        map.easeTo({ pitch: 62, bearing: -20, duration: 800 })
      } else {
        map.setTerrain(null)
        map.easeTo({ pitch: 0, bearing: 0, duration: 600 })
      }
    },
    is3D() {
      return is3dMode
    },
    fitScene() {
      if (!scene || scene.grids.length === 0) return
      const box = boundsOf(scene.grids)
      if (box) {
        map.fitBounds(box, { padding: 48, duration: 450 })
      }
    },
    frame(points: Position[]) {
      if (points.length === 0) return
      if (points.length === 1) {
        map.easeTo({ center: [points[0].lon, points[0].lat], duration: 450 })
        return
      }
      const lons = points.map((p) => p.lon)
      const lats = points.map((p) => p.lat)
      const box: [[number, number], [number, number]] = [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ]
      map.fitBounds(box, { padding: 80, duration: 450, maxZoom: 15 })
    },
    centerOnDrone(pos?: Position | null) {
      if (pos) {
        map.easeTo({ center: [pos.lon, pos.lat], duration: 450 })
        return
      }
      for (const [, marker] of drones) {
        if (!marker.getElement().classList.contains('other')) {
          const lngLat = marker.getLngLat()
          map.easeTo({ center: [lngLat.lng, lngLat.lat], duration: 450 })
          return
        }
      }
    },
    onChoice(hover, click) {
      choiceHover = hover
      choiceClick = click
    },
    destroy() {
      map.remove()
    },
  }
}

function zoneStyle(z: Zone, state: ZoneState): { color: string; opacity: number } {
  return kindStyle(z.kind, state)
}

function kindStyle(kind: Zone['kind'], state: ZoneState): { color: string; opacity: number } {
  if (kind === 'radar') return { color: COLORS.radar, opacity: 0.2 }
  if (kind === 'enemy_jammer') return { color: COLORS.enemy, opacity: 0.2 }
  if (kind === 'avoid') return { color: COLORS.avoid, opacity: 0.12 }
  if (state === 'cut') return { color: COLORS.friendlyCut, opacity: 0.08 }
  if (state === 'pending') return { color: COLORS.friendlyPending, opacity: 0.18 }
  return { color: COLORS.friendlyOn, opacity: 0.22 }
}

/** HTML marker with a dot and a text label (no glyph server needed, works offline).
 *
 * The marker element is the dot alone, anchored on its centre, so the position is exactly under
 * it and a click lands on the point itself. The text hangs to the right and never takes the mouse. */
function label(map: MlMap, pos: Position, text: string, color: string): maplibregl.Marker {
  const el = document.createElement('div')
  el.className = 'map-label-dot'
  el.style.background = color
  const span = document.createElement('span')
  span.className = 'map-label-text'
  span.textContent = text
  el.append(span)
  return new maplibregl.Marker({ element: el, anchor: 'center' }).setLngLat([pos.lon, pos.lat]).addTo(map)
}
