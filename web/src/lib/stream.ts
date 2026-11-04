import { useEffect, useRef, useState } from "react";
import { API } from "./api";

// One EventSource for the whole app, fanned out to whoever subscribes.

export type StreamMessage = { type: string; incident_id?: number; [key: string]: any };
type Listener = (message: StreamMessage) => void;

const TYPES = ["signal", "incident", "investigation", "action", "verification", "postmortem", "settings", "change"];
const listeners = new Set<Listener>();
const statusListeners = new Set<(connected: boolean) => void>();
let source: EventSource | null = null;
let connected = false;

function setConnected(value: boolean) {
  connected = value;
  statusListeners.forEach((l) => l(value));
}

function open() {
  source = new EventSource(`${API}/stream`);
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false); // EventSource reconnects on its own
  for (const type of TYPES) {
    source.addEventListener(type, (event) => {
      const message = JSON.parse((event as MessageEvent).data) as StreamMessage;
      listeners.forEach((l) => l(message));
    });
  }
}

export function subscribe(listener: Listener): () => void {
  if (!source) open();
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Calls handler for every stream message; the latest handler is always used. */
export function useStream(handler: Listener) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((m) => ref.current(m)), []);
}

export function useStreamStatus(): boolean {
  const [value, setValue] = useState(connected);
  useEffect(() => {
    if (!source) open();
    statusListeners.add(setValue);
    return () => {
      statusListeners.delete(setValue);
    };
  }, []);
  return value;
}
