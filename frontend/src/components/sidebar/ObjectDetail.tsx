// One object in detail: a friendly jammer (its cuts, the missions it concerns) or an enemy threat.
import { Badge, Button, Stack, Text, UnstyledButton } from '@mantine/core'
import { send } from '../../api/client'
import { formatPosition, friendlyZoneState } from '../../domain/view'
import { useLiveSimTime } from '../../hooks/useSimNow'
import { useStore } from '../../store'
import { THREAT_COLOR } from '../colors'
import { RadioIcon, TrashIcon } from '../Icons'
import { CutRow, PanelHeader, SectionTitle } from './parts'
import { threatSummary } from './threat'

interface Props {
  readonly kind: 'jammer' | 'threat'
  readonly id: string
}

export function ObjectDetail({ kind, id }: Props) {
  return kind === 'jammer' ? <JammerDetail id={id} /> : <ThreatDetail id={id} />
}

const STATE_TEXT = {
  on: { color: 'red', label: 'jamming' },
  cut: { color: 'green', label: 'cut' },
  pending: { color: 'orange', label: 'cut pending' },
} as const

function JammerDetail({ id }: { readonly id: string }) {
  const scenario = useStore((s) => s.snapshot?.scenario)
  const cuts = useStore((s) => s.cuts)
  const openList = useStore((s) => s.openList)
  const openCut = useStore((s) => s.openCut)
  const now = useLiveSimTime(1)
  const jammer = scenario?.friendly_jammers.find((j) => j.id === id)
  if (!jammer) return <Gone onBack={openList} />
  const list = Object.values(cuts)
    .filter((c) => c.jammer === id)
    .sort((a, b) => a.start_s - b.start_s)
  const state = STATE_TEXT[friendlyZoneState(id, list, now)]
  const { antenna } = jammer

  return (
    <Stack gap="xs">
      <PanelHeader
        title={`Friendly jammer ${id}`}
        badge={<Badge color={state.color}>{state.label}</Badge>}
        onBack={openList}
      />
      <Text size="xs" c="dimmed">
        Operator {jammer.operator_callsign} · {jammer.bands.join(', ')} · {jammer.eirp_w} W EIRP
      </Text>
      <Text size="xs" c="dimmed">
        {formatPosition(jammer.pos)} ·{' '}
        {antenna
          ? `directional, azimuth ${antenna.azimuth_deg}°, beam ${antenna.beamwidth_deg}°`
          : 'omnidirectional'}
      </Text>

      <SectionTitle>Cuts</SectionTitle>
      {list.length === 0 && <Text size="sm" c="dimmed">No cut scheduled.</Text>}
      {list.map((c) => (
        <CutRow key={c.id} cut={c} showJammer={false} />
      ))}
      <Button
        size="xs"
        variant="light"
        color="orange"
        leftSection={<RadioIcon size={14} />}
        onClick={() => openCut({ jammer: id, mission: null })}
      >
        Schedule a cut
      </Button>

      <SectionTitle>Missions it concerns</SectionTitle>
      <ConcernedMissions match={(jammers) => jammers.includes(id)} />
    </Stack>
  )
}

function ThreatDetail({ id }: { readonly id: string }) {
  const scenario = useStore((s) => s.snapshot?.scenario)
  const isCommand = useStore((s) => s.snapshot?.viewer.role === 'command')
  const openList = useStore((s) => s.openList)
  const threat = scenario?.enemy_threats.find((t) => t.id === id)
  if (!threat) return <Gone onBack={openList} />

  const remove = () => {
    send({ type: 'threat.remove', threat: id })
    openList()
  }

  return (
    <Stack gap="xs">
      <PanelHeader
        title={threat.type === 'avoid' ? `Zone to avoid ${id}` : `Enemy ${threat.type} ${id}`}
        badge={<Badge color={THREAT_COLOR[threat.type]}>{threat.type}</Badge>}
        onBack={openList}
      />
      <Text size="xs" c="dimmed">{threatSummary(threat)}</Text>
      <Text size="xs" c="dimmed">{formatPosition(threat.pos)}</Text>

      {isCommand && (
        <Button
          size="xs"
          variant="light"
          color="red"
          leftSection={<TrashIcon size={14} />}
          onClick={remove}
        >
          Remove threat
        </Button>
      )}

      <SectionTitle>Missions it concerns</SectionTitle>
      <ConcernedMissions match={(_, threats) => threats.includes(id)} />
    </Stack>
  )
}

/** Missions in view whose constraints match, each one opening its detail. */
function ConcernedMissions({ match }: { readonly match: (jammers: string[], threats: string[]) => boolean }) {
  const missions = useStore((s) => s.missions)
  const openMission = useStore((s) => s.openMission)
  const list = Object.values(missions).filter((m) => match(m.constraints.jammers, m.constraints.threats))
  if (list.length === 0) return <Text size="sm" c="dimmed">None in view.</Text>
  return (
    <Stack gap={2}>
      {list.map((m) => (
        <UnstyledButton key={m.constraints.mission} className="side-row" onClick={() => openMission(m.constraints.mission)}>
          <Text size="sm">
            {m.constraints.mission} · <Text span c="dimmed">{m.status}</Text>
          </Text>
        </UnstyledButton>
      ))}
    </Stack>
  )
}

function Gone({ onBack }: { readonly onBack: () => void }) {
  return (
    <Stack gap="xs">
      <PanelHeader title="Not in view" onBack={onBack} />
      <Text size="sm" c="dimmed">This object is no longer in your view.</Text>
    </Stack>
  )
}
