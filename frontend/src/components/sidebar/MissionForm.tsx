// The mission form, over the sidebar panel: a pilot's new mission (action bar), or a change to a
// mission still on the ground (mission detail). Launch and target are typed or pasted
// ("lat, lon"), or picked on the map.
import { Alert, Button, Group, SegmentedControl, Stack, Text, TextInput } from '@mantine/core'
import { useState } from 'react'
import { send } from '../../api/client'
import type { Mission, Position } from '../../api/protocol'
import { clockLabel, distanceM, missionOf, nextClockTime } from '../../domain/view'
import { useLiveSimTime } from '../../hooks/useSimNow'
import { useStore } from '../../store'
import { PanelHeader, PointField } from './parts'

type When = 'keep' | 'asap' | '5' | '15' | 'at'

const WHEN = [
  { value: 'asap', label: 'ASAP' },
  { value: '5', label: 'In 5 min' },
  { value: '15', label: 'In 15 min' },
  { value: 'at', label: 'At time' },
]

export function MissionForm() {
  const snapshot = useStore((s) => s.snapshot)
  const draft = useStore((s) => s.requestDraft)
  if (!snapshot || !draft.open) return null
  const edited = draft.editing ? missionOf(snapshot.scenario, draft.editing) : undefined
  if (draft.editing && !edited) return null
  // Keyed: the departure choice starts over for each mission.
  return <Form key={draft.editing ?? 'new'} edited={edited ?? null} />
}

function Form({ edited }: { readonly edited: Mission | null }) {
  const snapshot = useStore((s) => s.snapshot)
  const callsign = useStore((s) => s.callsign)
  const draft = useStore((s) => s.requestDraft)
  const setDraft = useStore((s) => s.setRequestDraft)
  const close = useStore((s) => s.closeRequest)
  const now = useLiveSimTime(1)
  const [when, setWhen] = useState<When>(edited ? 'keep' : 'asap')
  const [at, setAt] = useState('')
  if (!snapshot) return null

  const startTime = snapshot.scenario.start_time
  const pilot = edited?.pilot_callsign ?? callsign
  const drone = snapshot.scenario.pilots.find((p) => p.callsign === pilot)?.drone
  const departureS = when === 'keep' && edited ? edited.departure_s : departure(when, at, startTime, now)
  const { launch, target } = draft
  const ready = launch && target && departureS !== null
  const options = edited
    ? [{ value: 'keep', label: `Keep ${clockLabel(startTime, edited.departure_s).slice(0, 5)}` }, ...WHEN]
    : WHEN

  const submit = () => {
    if (!launch || !target || departureS === null) return
    if (edited) send({ type: 'mission.update', mission: edited.id, launch, target, departure_s: departureS })
    else send({ type: 'mission.request', launch, target, departure_s: departureS })
  }

  return (
    <Stack gap="sm">
      <PanelHeader title={edited ? `Change mission ${edited.id}` : 'New mission'} onClose={close} />
      {drone && (
        <Text size="xs" c="dimmed">
          {drone.id} · {drone.band} · {drone.cruise_speed_mps} m/s · {Math.round(drone.battery_max_s / 60)} min of battery
        </Text>
      )}

      <PointField
        label="Launch point"
        hint="Where you stand: the drone takes off here."
        value={launch}
        picking={draft.picking === 'launch'}
        onChange={(p) => setDraft({ launch: p })}
        onPick={() => setDraft({ picking: draft.picking === 'launch' ? null : 'launch' })}
      />
      <PointField
        label="Target"
        value={target}
        picking={draft.picking === 'target'}
        onChange={(p) => setDraft({ target: p })}
        onPick={() => setDraft({ picking: draft.picking === 'target' ? null : 'target' })}
      />

      <div>
        <Text size="sm" fw={500} mb={4}>Departure</Text>
        <SegmentedControl fullWidth size="xs" data={options} value={when} onChange={(v) => setWhen(v as When)} />
        {when === 'at' && (
          <TextInput
            mt="xs"
            type="time"
            label="Time (UTC)"
            value={at}
            onChange={(e) => setAt(e.currentTarget.value)}
            error={at && departureS === null ? 'Not a valid time' : undefined}
          />
        )}
        <Text size="xs" c="dimmed" mt={4}>
          {departureS === null ? 'Pick a time.' : `Earliest take-off ${clockLabel(startTime, departureS)}`}
          {when === 'at' && departureS !== null && departureS - now > 43_200 && ' (tomorrow)'}
        </Text>
      </div>

      {launch && target && drone && <Reach launch={launch} target={target} speed={drone.cruise_speed_mps} battery={drone.battery_max_s} />}

      <Group justify="flex-end" gap="xs">
        <Button variant="subtle" color="gray" onClick={close}>Cancel</Button>
        <Button disabled={!ready} onClick={submit}>
          {edited ? 'Update mission' : 'Request mission'}
        </Button>
      </Group>
    </Stack>
  )
}

/** Departure for the choices that do not keep the mission's own ('keep' counts as ASAP). */
function departure(when: When, at: string, startTime: string, now: number): number | null {
  if (when === 'at') return nextClockTime(startTime, now, at)
  const delay = when === '5' || when === '15' ? Number(when) * 60 : 0
  return Math.round(now + delay)
}

interface ReachProps {
  readonly launch: Position
  readonly target: Position
  readonly speed: number
  readonly battery: number
}

/** Straight-line figures: the real route can only be longer (detours around zones). */
function Reach({ launch, target, speed, battery }: ReachProps) {
  const dist = distanceM(launch, target)
  const flightS = dist / speed
  const pct = (100 * flightS) / battery
  const color = pct > 100 ? 'red' : pct > 70 ? 'orange' : 'teal'
  const verdict =
    pct > 100
      ? 'Out of range even in a straight line.'
      : pct > 70
        ? 'Little margin left for detours around jamming zones.'
        : 'Within range; detours will add to these figures.'
  return (
    <Alert color={color} variant="light" p="xs">
      <Text size="sm" fw={600}>
        {(dist / 1000).toFixed(1)} km · {Math.round(flightS / 60)} min · {Math.round(pct)} % battery
      </Text>
      <Text size="xs">Straight line. {verdict}</Text>
    </Alert>
  )
}
