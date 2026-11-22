// Pieces shared by the sidebar panels.
import { Badge, Button, CloseButton, Group, Text } from '@mantine/core'
import type { ReactNode } from 'react'
import { send } from '../../api/client'
import type { Cut } from '../../api/protocol'
import { clockLabel } from '../../domain/view'
import { useLiveSimTime } from '../../hooks/useSimNow'
import { useStore } from '../../store'

interface PanelHeaderProps {
  readonly title: ReactNode
  readonly badge?: ReactNode
  /** Back arrow (detail views) or close cross (forms). */
  readonly onBack?: () => void
  readonly onClose?: () => void
}

export function PanelHeader({ title, badge, onBack, onClose }: PanelHeaderProps) {
  return (
    <Group justify="space-between" wrap="nowrap" gap="xs">
      <Group gap={6} wrap="nowrap">
        {onBack && (
          <Button size="compact-xs" variant="subtle" color="gray" onClick={onBack} aria-label="Back to the lists">
            ←
          </Button>
        )}
        <Text fw={700}>{title}</Text>
      </Group>
      <Group gap={6} wrap="nowrap">
        {badge}
        {onClose && <CloseButton size="sm" aria-label="Close the form" onClick={onClose} />}
      </Group>
    </Group>
  )
}

export function SectionTitle({ children }: { readonly children: ReactNode }) {
  return (
    <Text size="xs" fw={700} c="dimmed" tt="uppercase" mt="xs">
      {children}
    </Text>
  )
}

/** One cut, with Confirm (its jammer's operator only), Back on now (while it runs: its
 * operator or the command) and Cancel. */
export function CutRow({ cut, showJammer }: { readonly cut: Cut; readonly showJammer: boolean }) {
  const snapshot = useStore((s) => s.snapshot)
  const actor = useStore((s) => s.callsign)
  const now = useLiveSimTime(2)
  if (!snapshot) return null
  const { start_time, friendly_jammers, command_callsign } = snapshot.scenario
  const running = cut.start_s <= now && now < cut.end_s
  const operator = friendly_jammers.find((j) => j.id === cut.jammer)?.operator_callsign
  const label = (t: number) => clockLabel(start_time, t)

  return (
    <Group justify="space-between" wrap="nowrap">
      <div>
        <Text size="sm" fw={600}>
          {showJammer && `${cut.jammer} `}
          {label(cut.start_s)}–{label(cut.end_s)}
        </Text>
        <Text size="xs" c="dimmed">
          {cut.id} · by {cut.requested_by}
          {cut.requested_for && ` for ${cut.requested_for}`}
        </Text>
      </div>
      <Group gap={4} wrap="nowrap">
        <Badge color={cut.status === 'confirmed' ? 'green' : 'orange'}>{cut.status}</Badge>
        {cut.status === 'scheduled' && (
          <Button
            size="compact-xs"
            disabled={actor !== operator}
            title={actor === operator ? undefined : `Only ${operator} can confirm`}
            onClick={() => send({ type: 'cut.confirm', cut: cut.id })}
          >
            Confirm
          </Button>
        )}
        {running && (actor === operator || actor === command_callsign) && (
          <Button
            size="compact-xs"
            color="red"
            title="The jammer is back on now: drones relying on this cut are rerouted"
            onClick={() => send({ type: 'jammer.restart', jammer: cut.jammer })}
          >
            Back on now
          </Button>
        )}
        <Button size="compact-xs" variant="subtle" color="red" onClick={() => send({ type: 'cut.cancel', cut: cut.id })}>
          Cancel
        </Button>
      </Group>
    </Group>
  )
}
