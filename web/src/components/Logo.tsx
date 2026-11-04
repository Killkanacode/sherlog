export function Mark({ size = 22, color = "#d4a84b" }: { size?: number; color?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="10" cy="10" r="7" />
      <path d="M15.2 15.2 21 21" />
      <path d="M5.5 10h2l1.3-2.6 2.2 5.2 1.3-2.6h2" />
    </svg>
  );
}
