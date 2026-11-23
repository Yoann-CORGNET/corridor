// WebSocket client: auto-reconnect, one JSON message per frame, commands wrapped with a req_id.
// The backend resends a full snapshot on every (re)connect, so no resync logic lives here.
// The server knows who connects from `?callsign=`; it closes an unknown callsign with 4003,
// which stops the reconnect loop (retrying would fail the same way).
import type { Command, ServerMessage } from './protocol'

export type ConnStatus = 'idle' | 'connecting' | 'open' | 'closed'

/** Close code the server sends for a callsign the scenario does not know. */
export const UNKNOWN_CALLSIGN = 4003

/** Scenario callsigns look like CMD, PILOT-1, EW-J3. The callsign comes from the URL or browser
 *  storage, so anything else is refused before it is stored or sent. */
const CALLSIGN_PATTERN = /^[A-Za-z0-9_-]{1,32}$/

export function isValidCallsign(value: string): boolean {
  return CALLSIGN_PATTERN.test(value)
}

interface Handlers {
  onMessage: (msg: ServerMessage) => void
  onStatus: (status: ConnStatus) => void
  /** The server refused the callsign; no reconnect follows. */
  onRejected: (reason: string) => void
}

export class CorridorSocket {
  private readonly baseUrl: string
  private readonly handlers: Handlers
  private url = ''
  private ws: WebSocket | null = null
  private reqCounter = 0
  private retries = 0
  private stopped = true
  private timer: number | undefined

  constructor(baseUrl: string, handlers: Handlers) {
    this.baseUrl = baseUrl
    this.handlers = handlers
  }

  /** Connect as `callsign`, dropping any current connection. */
  start(callsign: string): void {
    if (!isValidCallsign(callsign)) throw new Error(`invalid callsign ${callsign}`)
    this.stop()
    this.url = `${this.baseUrl}?callsign=${encodeURIComponent(callsign)}`
    this.stopped = false
    this.retries = 0
    this.open()
  }

  stop(): void {
    this.stopped = true
    window.clearTimeout(this.timer)
    const ws = this.ws
    this.ws = null
    ws?.close()
    this.handlers.onStatus('idle')
  }

  /** Returns the req_id, or null when not connected (the command is dropped). */
  send(command: Command): string | null {
    if (this.ws?.readyState !== WebSocket.OPEN) return null
    const req_id = `r${++this.reqCounter}`
    this.ws.send(JSON.stringify({ req_id, command }))
    return req_id
  }

  private open(): void {
    this.handlers.onStatus('connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws
    ws.onopen = () => {
      this.retries = 0
      this.handlers.onStatus('open')
    }
    ws.onmessage = (event) => {
      try {
        this.handlers.onMessage(JSON.parse(event.data as string) as ServerMessage)
      } catch (err) {
        console.error('unparsable server message', err)
      }
    }
    ws.onclose = (event) => {
      if (this.ws !== ws) return
      if (event.code === UNKNOWN_CALLSIGN) {
        this.stopped = true
        this.ws = null
        this.handlers.onStatus('idle')
        this.handlers.onRejected(event.reason || 'unknown callsign')
        return
      }
      this.handlers.onStatus('closed')
      if (this.stopped) return
      const delay = Math.min(5000, 500 * 2 ** this.retries++)
      this.timer = window.setTimeout(() => this.open(), delay)
    }
  }
}

/** Same origin by default: Vite proxies /ws in dev, the backend serves both in the demo build. */
export function defaultWsUrl(): string {
  const fromEnv = import.meta.env.VITE_WS_URL as string | undefined
  if (fromEnv) return fromEnv
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}
