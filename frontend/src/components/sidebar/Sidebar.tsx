// The sidebar: actions on top, then one panel (lists, a mission, an object) or a form over it,
// and the notifications docked at the bottom.
import { Alert, Button, Group, ScrollArea, Stack } from '@mantine/core'
import { missionOf } from '../../domain/view'
import { useStore } from '../../store'
import { PlusIcon, RadioIcon } from '../Icons'
import { CutForm } from './CutForm'
import { ListView } from './ListView'
import { MissionDetail } from './MissionDetail'
import { Notifications } from './Notifications'
import { ObjectDetail } from './ObjectDetail'
import { ThreatForm } from './ThreatForm'

export function Sidebar() {
  const lastError = useStore((s) => s.lastError)
  const setError = useStore((s) => s.setError)
  const hasSnapshot = useStore((s) => s.snapshot !== null)

  return (
    <aside className="side">
      <ActionBar />
      <ScrollArea className="side-body">
        <Stack gap="sm" p="sm">
          {lastError && (
            <Alert color="red" withCloseButton onClose={() => setError(null)} title="Server">
              {lastError}
            </Alert>
          )}
          {hasSnapshot ? <Body /> : <Alert color="yellow">Waiting for corridor-server on /ws…</Alert>}
        </Stack>
      </ScrollArea>
      <Notifications />
    </aside>
  )
}

/** The cut form when open, else the panel. A panel whose mission or object left the view
 * falls back to the lists. The mission form floats on the map (``MapPanel``). */
function Body() {
  const panel = useStore((s) => s.panel)
  const cutOpen = useStore((s) => s.cutDraft !== null)
  const threatOpen = useStore((s) => s.threatDraft.open)
  if (threatOpen) return <ThreatForm />
  if (cutOpen) return <CutForm />
  if (panel.view === 'mission') return <MissionDetail id={panel.id} />
  if (panel.view === 'object') return <ObjectDetail kind={panel.kind} id={panel.id} />
  return <ListView />
}

function ActionBar() {
  const snapshot = useStore((s) => s.snapshot)
  const callsign = useStore((s) => s.callsign)
  const panel = useStore((s) => s.panel)
  const requestOpen = useStore((s) => s.requestDraft.open)
  const cutOpen = useStore((s) => s.cutDraft !== null)
  const threatOpen = useStore((s) => s.threatDraft.open)
  const openRequest = useStore((s) => s.openRequest)
  const openCut = useStore((s) => s.openCut)
  const openThreat = useStore((s) => s.openThreat)
  const closeThreat = useStore((s) => s.closeThreat)
  if (!snapshot) return null
  const { scenario } = snapshot
  const isPilot = snapshot.viewer.role === 'pilot'
  // The command only answers cut requests: it does not schedule cuts of its own.
  const isCommand = snapshot.viewer.role === 'command'

  // The pilot stands where its selected (else last) mission took off: a good guess for the next.
  const lastLaunch = () => {
    const own = scenario.missions.filter((m) => m.pilot_callsign === callsign)
    const selected = useStore.getState().selectedMission
    const pick = (selected ? missionOf(scenario, selected) : undefined) ?? own.at(-1)
    return pick?.pilot_callsign === callsign ? pick.launch : null
  }
  // A cut asked from a jammer or a mission starts on it.
  const cutFrom = () => {
    if (panel.view === 'object' && panel.kind === 'jammer') return { jammer: panel.id, mission: null }
    if (panel.view === 'mission') return { jammer: null, mission: panel.id }
    return { jammer: null, mission: null }
  }

  return (
    <Group className="side-actions" gap="xs" px="sm" wrap="nowrap">
      {isPilot && (
        <Button
          size="xs"
          leftSection={<PlusIcon size={14} />}
          variant={requestOpen ? 'filled' : 'light'}
          onClick={() => openRequest(lastLaunch())}
        >
          New mission
        </Button>
      )}
      {isCommand && (
        <Button
          size="xs"
          color="red"
          leftSection={<PlusIcon size={14} />}
          variant={threatOpen ? 'filled' : 'light'}
          onClick={() => (threatOpen ? closeThreat() : openThreat())}
        >
          New threat
        </Button>
      )}
      {!isCommand && (
        <Button
          size="xs"
          color="orange"
          leftSection={<RadioIcon size={14} />}
          variant={cutOpen ? 'filled' : 'light'}
          disabled={scenario.friendly_jammers.length === 0}
          onClick={() => openCut(cutFrom())}
        >
          EW cut
        </Button>
      )}
    </Group>
  )
}
