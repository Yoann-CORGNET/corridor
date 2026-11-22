// Pieces shared by the sidebar panels.
import { ActionIcon, Badge, Button, CloseButton, Group, Text, TextInput, Tooltip } from '@mantine/core'
import { useState, type ReactNode } from 'react'
import { send } from '../../api/client'
import type { Cut, Position } from '../../api/protocol'
import { clockLabel, formatPosition, parsePosition } from '../../domain/view'
import { useStore } from '../../store'
import { CrosshairIcon } from '../Icons'

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

/** One cut, with Confirm (its jammer's operator only) and Cancel. */
export function CutRow({ cut, showJammer }: { readonly cut: Cut; readonly showJammer: boolean }) {
  const snapshot = useStore((s) => s.snapshot)
  const actor = useStore((s) => s.callsign)
  if (!snapshot) return null
  const { start_time, friendly_jammers } = snapshot.scenario
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
        <Button size="compact-xs" variant="subtle" color="red" onClick={() => send({ type: 'cut.cancel', cut: cut.id })}>
          Cancel
        </Button>
      </Group>
    </Group>
  )
}

export interface PointFieldProps {
  readonly label: string
  readonly hint?: string
  readonly pickingHint?: string
  readonly value: Position | null
  /** The next map click sets this point. */
  readonly picking: boolean
  readonly color?: string
  readonly onChange: (p: Position | null) => void
  readonly onPick: () => void
}

/** "lat, lon" text field kept in sync with the draft: typed, pasted or picked on the map. */
export function PointField({
  label,
  hint,
  pickingHint = 'Click on the map…',
  value,
  picking,
  color,
  onChange,
  onPick,
}: PointFieldProps) {
  const [text, setText] = useState(value ? formatPosition(value) : '')
  const [shown, setShown] = useState(value)
  // Picked on the map (or reset): show the new point. Typing keeps the text as typed.
  if (value !== shown) {
    setShown(value)
    const typed = parsePosition(text)
    if (!(typed && value && typed.lat === value.lat && typed.lon === value.lon)) {
      setText(value ? formatPosition(value) : '')
    }
  }
  const invalid = text.trim() !== '' && parsePosition(text) === null

  return (
    <TextInput
      label={label}
      description={picking ? pickingHint : hint}
      placeholder="48.14500, 11.51300"
      value={text}
      onChange={(e) => {
        setText(e.currentTarget.value)
        onChange(parsePosition(e.currentTarget.value))
      }}
      error={invalid ? 'Type "lat, lon" in decimal degrees' : undefined}
      rightSection={
        <Tooltip label={picking ? 'Stop picking' : 'Pick on the map'} openDelay={300}>
          <ActionIcon
            variant={picking ? 'filled' : 'light'}
            color={color ?? (picking ? 'blue' : undefined)}
            aria-label={`Pick ${label.toLowerCase()} on the map`}
            onClick={onPick}
          >
            <CrosshairIcon size={14} />
          </ActionIcon>
        </Tooltip>
      }
    />
  )
}
