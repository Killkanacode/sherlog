import { useEffect, useState } from "react";

// Hash routes (#/incidents/12) so the static build needs no server rewrites.

export function useRoute(): string[] {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => {
      setHash(window.location.hash);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#\/?/, "").split("/").filter(Boolean);
}

export const href = (path: string) => `#/${path.replace(/^\//, "")}`;

export function go(path: string) {
  window.location.hash = href(path);
}
