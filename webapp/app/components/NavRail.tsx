"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/today", label: "Today" },
  { href: "/", label: "Ask" },
  { href: "/library", label: "Library" },
  { href: "/graph", label: "Graph" },
  { href: "/sessions", label: "Sessions" },
  { href: "/files", label: "Files" },
  { href: "/connectors", label: "Connectors" },
  { href: "/agents", label: "Agents" },
  { href: "/models", label: "Models" },
  { href: "/status", label: "Status" },
];

export function NavRail() {
  const pathname = usePathname();
  return (
    <nav style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {LINKS.map(({ href, label }) => (
        <Link
          key={href}
          href={href}
          className={`nav-link${pathname === href ? " active" : ""}`}
        >
          {label}
        </Link>
      ))}
    </nav>
  );
}
