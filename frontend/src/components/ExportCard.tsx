// Route export: send the selected mission's plan to its pilot's ATAK, or download it for the flight
// controller. The server exports its own current plan (HTTP /api/export/...), never the mock one.
import { Alert, Badge, Button, Card, Group, NumberInput, Stack, Text } from '@mantine/core'
import { useState } from 'react'
import { missionOf } from '../domain/view'
import { useSelectedMission, useStore } from '../store'

const DEFAULT_ALTITUDE_M = 100

interface AtakSent {
  route_uid: string
  pilot_callsign: string
  chat_sent: boolean
  revision: number
}

type Result = { mission: string } & ({ ok: true; sent: AtakSent } | { ok: false; reason: string })

async function reason(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    return typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

export function ExportCard() {
  const snapshot = useStore((s) => s.snapshot)
  const view = useSelectedMission()
  const [altitude, setAltitude] = useState(DEFAULT_ALTITUDE_M)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null)
  if (!snapshot || !view) return null
  const mission = missionOf(snapshot.scenario, view.constraints.mission)
  if (!mission) return null

  const { plan } = view
  const pilot = mission.pilot_callsign
  const ready = !!plan && plan.status !== 'preview'
  const query = `mission_id=${encodeURIComponent(mission.id)}&altitude_m=${altitude}`
  // A result only describes the mission it was sent for.
  const shown = result?.mission === mission.id ? result : null
  const stale = shown?.ok && plan && shown.sent.revision !== plan.revision

  const sendToAtak = async () => {
    const id = mission.id
    setBusy(true)
    try {
      const response = await fetch(`/api/export/atak?${query}`, { method: 'POST' })
      setResult(
        response.ok
          ? { mission: id, ok: true, sent: (await response.json()) as AtakSent }
          : { mission: id, ok: false, reason: await reason(response) },
      )
    } catch (e) {
      setResult({ mission: id, ok: false, reason: String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card withBorder padding="sm">
      <Group justify="space-between" mb="xs">
        <Text fw={700}>Export route {mission.id}</Text>
        {plan && <Badge variant="light">rev {plan.revision}</Badge>}
      </Group>
      <Stack gap="xs">
        <NumberInput
          size="xs"
          label="Altitude above launch (m)"
          min={1}
          max={1000}
          value={altitude}
          onChange={(v) => setAltitude(typeof v === 'number' && v > 0 ? v : DEFAULT_ALTITUDE_M)}
        />
        <Button size="xs" disabled={!ready} loading={busy} onClick={sendToAtak} title={ready ? undefined : 'Only a flyable plan is exported'}>
          Send to {pilot} on ATAK
        </Button>
        <Group gap="xs" grow>
          <Button size="xs" variant="light" component="a" href={`/api/export/inav?${query}`} download disabled={!ready}>
            INAV .mission
          </Button>
          <Button size="xs" variant="light" component="a" href={`/api/export/ardupilot?${query}`} download disabled={!ready}>
            ArduPilot .waypoints
          </Button>
          <Button size="xs" variant="subtle" component="a" href={`/api/export/briefing?${query}`} target="_blank" disabled={!ready}>
            Briefing
          </Button>
        </Group>
        {shown?.ok && (
          <Alert color={stale ? 'orange' : 'green'} variant="light" p="xs">
            <Text size="xs">
              Rev {shown.sent.revision} sent to {shown.sent.pilot_callsign}
              {shown.sent.chat_sent ? ' (route + briefing chat).' : ' (route only: pilot position not seen, no chat).'}
              {stale && ' The plan changed since: send again.'}
            </Text>
          </Alert>
        )}
        {shown && !shown.ok && (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs">{shown.reason}</Text>
          </Alert>
        )}
      </Stack>
    </Card>
  )
}
