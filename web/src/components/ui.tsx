import type { ButtonHTMLAttributes, ReactNode } from "react";
import { TONE_BG, TONE_HEX, TONE_TEXT, type Tone } from "../lib/labels";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

export function Panel({
  title,
  aside,
  children,
  className,
  bodyClass,
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={cx("border border-line bg-panel", className)}>
      {(title || aside) && (
        <header className="flex min-h-10 items-center justify-between gap-3 border-b border-line px-4 py-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-muted">{title}</h2>
          {aside && <div className="flex items-center gap-2 text-xs text-muted">{aside}</div>}
        </header>
      )}
      <div className={bodyClass ?? "p-4"}>{children}</div>
    </section>
  );
}

export function Dot({ tone, live, className }: { tone: Tone; live?: boolean; className?: string }) {
  return <span className={cx("inline-block size-2 shrink-0", TONE_BG[tone], live && "blink", className)} />;
}

export function Badge({ tone = "muted", children, live }: { tone?: Tone; children: ReactNode; live?: boolean }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 border px-1.5 py-px text-[11px] font-medium whitespace-nowrap",
        TONE_TEXT[tone],
        "border-current/35",
      )}
    >
      <Dot tone={tone} live={live} className="size-1.5" />
      {children}
    </span>
  );
}

type Variant = "primary" | "default" | "danger" | "ghost" | "go";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-ink text-bg hover:bg-white font-semibold",
  go: "bg-ok text-bg hover:brightness-110 font-semibold",
  default: "border border-line-strong bg-raised text-ink hover:bg-hover",
  danger: "border border-bad/50 text-bad hover:bg-bad/10",
  ghost: "text-muted hover:text-ink hover:bg-hover",
};

export function Button({
  variant = "default",
  size = "md",
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" | "lg" }) {
  return (
    <button
      {...rest}
      className={cx(
        "inline-flex items-center justify-center gap-2 transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        size === "sm" && "h-7 px-2.5 text-xs",
        size === "md" && "h-8 px-3 text-[13px]",
        size === "lg" && "h-10 px-4 text-sm",
        VARIANTS[variant],
        className,
      )}
    />
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-[0.09em] text-muted">{label}</div>
      <div className={cx("num mt-1 text-[22px] leading-none font-semibold", tone ? TONE_TEXT[tone] : "text-ink")}>
        {value}
      </div>
      {sub && <div className="mt-1.5 truncate text-xs text-muted">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-8 text-center text-[13px] text-muted">{children}</div>;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: ReactNode; count?: number }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="flex gap-0 overflow-x-auto border-b border-line px-2">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={cx(
            "-mb-px flex items-center gap-2 border-b-2 px-3 py-2.5 text-[13px] whitespace-nowrap",
            value === t.id ? "border-brass text-ink" : "border-transparent text-muted hover:text-ink",
          )}
        >
          {t.label}
          {t.count != null && <span className="num text-xs text-faint">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Note({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div className="border-l-2 bg-raised px-3 py-2 text-[13px] text-ink-2" style={{ borderColor: TONE_HEX[tone] }}>
      {children}
    </div>
  );
}

export function Meter({ value, tone = "brass" }: { value: number; tone?: Tone }) {
  const segments = 20;
  const lit = Math.round(Math.max(0, Math.min(1, value)) * segments);
  return (
    <div className="flex gap-[2px]" aria-label={`${Math.round(value * 100)}%`}>
      {Array.from({ length: segments }, (_, i) => (
        <span key={i} className={cx("h-2.5 flex-1", i < lit ? TONE_BG[tone] : "bg-line")} />
      ))}
    </div>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("font-mono text-[12.5px]", className)}>{children}</span>;
}

export function Chip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center border border-line bg-raised px-1.5 py-px font-mono text-[11.5px] text-ink-2", className)}>
      {children}
    </span>
  );
}
