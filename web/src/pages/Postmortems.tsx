import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button, cx, Empty, Panel } from "../components/ui";
import { API, get } from "../lib/api";
import { between, dateTime, duration } from "../lib/format";
import { usePoll } from "../lib/hooks";
import { statusOf } from "../lib/labels";
import { go, href } from "../lib/router";
import { useStream } from "../lib/stream";
import type { PostmortemRow } from "../lib/types";

export function Postmortems({ id }: { id: number | null }) {
  const list = usePoll(() => get<PostmortemRow[]>(`${API}/postmortems?limit=100`), 0);
  useStream((m) => m.type === "postmortem" && list.refresh());
  const rows = list.data ?? [];
  const selected = id ?? rows[0]?.id ?? null;

  return (
    <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
      <Panel title="Postmortems" aside={<span>{rows.length}</span>} bodyClass="">
        {rows.length === 0 ? (
          <Empty>{list.error ?? "Postmortems are written when an incident closes."}</Empty>
        ) : (
          <ul className="max-h-[calc(100vh-150px)] overflow-y-auto">
            {rows.map((r) => {
              const status = statusOf(r.status);
              return (
                <li key={r.id}>
                  <a
                    href={href(`postmortems/${r.id}`)}
                    className={cx(
                      "block border-b border-l-2 border-b-line px-4 py-2.5 hover:bg-hover",
                      selected === r.id ? "border-l-brass bg-raised" : "border-l-transparent",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-muted">#{r.id}</span>
                      <span className="text-xs text-faint">{dateTime(r.created_at)}</span>
                      <span className={cx("ml-auto text-xs", status.tone === "ok" ? "text-ok" : "text-warn")}>{status.label}</span>
                    </div>
                    <div className="mt-0.5 truncate text-[13px] text-ink-2">{r.title}</div>
                    {r.resolved_at && <div className="text-xs text-faint">lasted {duration(between(r.created_at, r.resolved_at))}</div>}
                  </a>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
      {selected != null ? <Document id={selected} /> : <div />}
    </div>
  );
}

function Document({ id }: { id: number }) {
  const doc = usePoll(() => get<string>(`${API}/incidents/${id}/postmortem`), 0, [id]);
  const [copied, setCopied] = useState(false);
  const text = doc.data;

  const copy = async () => {
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  const download = () => {
    if (!text) return;
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `postmortem-${id}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Panel
      title={`Incident #${id}`}
      aside={
        <>
          <Button size="sm" variant="ghost" onClick={() => go(`incidents/${id}`)}>
            Open case
          </Button>
          <Button size="sm" onClick={copy} disabled={!text}>
            {copied ? "Copied" : "Copy Markdown"}
          </Button>
          <Button size="sm" onClick={download} disabled={!text}>
            Download .md
          </Button>
        </>
      }
      bodyClass="px-8 py-7"
    >
      {text ? (
        <article className="doc">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </article>
      ) : (
        <Empty>{doc.error ?? "Loading…"}</Empty>
      )}
    </Panel>
  );
}
