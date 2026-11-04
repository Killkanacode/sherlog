import { createContext, useContext } from "react";

export type Role = "viewer" | "operator" | "admin";
export type Me = { name: string; role: Role };

const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

export const SessionContext = createContext<Me | null>(null);

export function useSession() {
  const me = useContext(SessionContext);
  return { me, can: (role: Role) => !!me && RANK[me.role] >= RANK[role] };
}
