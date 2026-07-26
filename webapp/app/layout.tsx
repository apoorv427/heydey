import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { NavRail } from "./components/NavRail";

export const metadata: Metadata = {
  title: "Heydey",
  description: "Local-first, proof-grade AI OS — does the work, shows its receipts.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="breath-field" />
        <div style={{ display: "flex", minHeight: "100vh", position: "relative", zIndex: 1 }}>
          <aside
            style={{
              width: 190,
              flexShrink: 0,
              padding: "28px 16px",
              display: "flex",
              flexDirection: "column",
              gap: 3,
            }}
          >
            <div style={{ padding: "0 12px 18px" }}>
              <div style={{ fontSize: 15.5, fontWeight: 650, letterSpacing: "-0.022em" }}>
                Heydey
              </div>
              <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
                does the work · shows its receipts
              </div>
            </div>
            <NavRail />
          </aside>
          <main style={{ flex: 1, padding: "40px 48px 80px", minWidth: 0 }}>
            <div style={{ maxWidth: 780, margin: "0 auto" }}>{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
