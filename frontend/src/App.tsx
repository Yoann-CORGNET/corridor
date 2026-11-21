import { Alert, ScrollArea, Stack } from '@mantine/core'
import { CutsCard } from './components/CutsCard'
import { ExportCard } from './components/ExportCard'
import { FeedCard } from './components/FeedCard'
import { MapPanel } from './components/MapPanel'
import { MissionCard } from './components/MissionCard'
import { MissionsCard } from './components/MissionsCard'
import { SignIn } from './components/SignIn'
import { TimelinePanel } from './components/TimelinePanel'
import { TopBar } from './components/TopBar'
import { useStore } from './store'

export default function App() {
  const callsign = useStore((s) => s.callsign)
  const lastError = useStore((s) => s.lastError)
  const setError = useStore((s) => s.setError)
  const hasSnapshot = useStore((s) => s.snapshot !== null)

  if (!callsign) return <SignIn />

  return (
    <div className="layout">
      <TopBar />
      <div className="main">
        <MapPanel />
        <ScrollArea className="side">
          <Stack gap="sm" p="sm">
            {lastError && (
              <Alert color="red" withCloseButton onClose={() => setError(null)} title="Server">
                {lastError}
              </Alert>
            )}
            {!hasSnapshot && <Alert color="yellow">Waiting for corridor-server on /ws…</Alert>}
            <MissionsCard />
            <MissionCard />
            <ExportCard />
            <CutsCard />
            <FeedCard />
          </Stack>
        </ScrollArea>
      </div>
      <TimelinePanel />
    </div>
  )
}
