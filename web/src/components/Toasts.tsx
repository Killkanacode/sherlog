import { useEffect, useRef, useState } from "react";
import { TONE_HEX, type Tone } from "../lib/labels";
import { href } from "../lib/router";
import { useStream } from "../lib/stream";

type Toast = { id: number; tone: Tone; title: string; text: string; link?: string; linkLabel?: string };

// Surfaces what needs a human from any screen: a new incident, an action
// waiting for approval, a verified recovery, a postmortem.
export function Toasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seen = useRef(new Set<string>());
  const counter = useRef(0);

  const push = (key: string, toast: Omit<Toast, "id">) => {
    if (seen.current.has(key)) return;
    seen.current.add(key);
    const id = ++counter.current;
    setToasts((list) => [...list.slice(-3), { ...toast, id }]);
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 14000);
  };

  useStream((m) => {
    const id = m.incident_id ?? m.incident?.id;
    if (m.type === "incident" && m.incident?.status === "detected") {
      push(`detected-${id}`, {
        tone: "bad",
        title: `Incident #${id} detected`,
        text: m.incident.title,
        link: `incidents/${id}`,
        linkLabel: "Open case",
      });
    }
    if (m.type === "action" && m.action?.state === "awaiting_approval") {
      push(`approve-${id}-${m.action.id}`, {
        tone: "brass",
        title: "Approval needed",
        text: `Sherlog wants to ${m.action.description}`,
        link: `incidents/${id}`,
        linkLabel: "Review",
      });
    }
    if (m.type === "action" && m.action?.state === "recovered") {
      push(`recovered-${id}-${m.action.id}`, {
        tone: "ok",
        title: `Incident #${id} recovered`,
        text: `${m.action.description}, verified`,
        link: `incidents/${id}`,
        linkLabel: "View",
      });
    }
    if (m.type === "postmortem") {
      push(`postmortem-${id}`, {
        tone: "info",
        title: `Postmortem #${id} ready`,
        text: "Impact, root cause, timeline and follow-ups",
        link: `postmortems/${id}`,
        linkLabel: "Read",
      });
    }
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setToasts([]);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="fixed right-5 bottom-5 z-50 flex w-[360px] flex-col gap-2">
      {toasts.map((t) => (
        <div key={t.id} className="border border-line-strong border-l-[3px] bg-raised px-4 py-3 shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={{ borderLeftColor: TONE_HEX[t.tone] }}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-ink">{t.title}</div>
              <div className="mt-0.5 truncate text-[13px] text-ink-2">{t.text}</div>
            </div>
            <button className="text-faint hover:text-ink" onClick={() => setToasts((l) => l.filter((x) => x.id !== t.id))} aria-label="Dismiss">
              ✕
            </button>
          </div>
          {t.link && (
            <a
              href={href(t.link)}
              onClick={() => setToasts((l) => l.filter((x) => x.id !== t.id))}
              className="mt-2 inline-block text-[13px] font-medium text-brass hover:underline"
            >
              {t.linkLabel} →
            </a>
          )}
        </div>
      ))}
    </div>
  );
}
