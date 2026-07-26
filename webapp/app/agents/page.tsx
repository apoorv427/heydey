import { FoundryFlow } from "../components/FoundryFlow";

// D5(a) — nav tab "Agents"; the flow inside is titled "Foundry — Architect
// onboarding". The page is a thin shell around FoundryFlow; every scene, every
// answer, every stopwatch number lives in the flow component (single source of
// truth is /foundry/status).

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Agents</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 640 }}>
        Foundry — Architect onboarding. Five deterministic questions instantiate
        a 3–6 agent fleet from a pre-authored playbook shelf. Every agent is a
        validated config row, hydrated into the same pipeline every Ask uses —
        no generated code, no freeform loop, no free-form prompt.
      </p>
      <FoundryFlow />
    </div>
  );
}
