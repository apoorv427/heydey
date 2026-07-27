# Use cases — how a non-technical team actually operates a system like this

This page is for the reader trying to understand what Heydey is *for*, past the
architecture. Two things below: the flow that ships in Heydey today, and a
real-world operating pattern from outside our own studio that shows the same
shape working under daily, non-technical use.

## The hero flow — an AI Product Manager's setup (ships today)

This is real, shipped, gate-checked — not a mockup of intent.

1. Point `~/.heydey/corpus.json` at your PRDs, user-interview transcripts, and
   competitor notes, then ingest the workspace.
2. Ask a real question — *"what did users say about onboarding friction in the
   last ten interviews?"* — and get either a **cited answer**, every sentence
   carrying a receipt (source · chunk · score · a different-family model's
   PASS · cost) with a breadcrumb that opens the exact source, or **silence**.
   Never a confident, synthesized-but-wrong summary of your own users.
3. Run the Foundry's 5-question onboarding and it stands up a small agent
   fleet from validated specs (competitor-watch · user-voice · roadmap-risk).
   The Morning Brief surfaces overnight deltas with citations, and any
   produced artifact — a PRD section draft — arrives as a **prepared action**:
   receipt attached, approval required, nothing fires silently.

No AI client is required to run any of this — it's a web app and a 5-question
form. (Installing Heydey itself is a separate step, and today that still means
a terminal — see [Known Issues](KNOWN-ISSUES.md).)

## A real-world operating pattern

An independent partner longevity-medicine firm runs a sibling build on the
same foundation as Heydey, daily — their own clinical team, no engineer,
operating it for patient-journey content, research syntheses, and digital
assets from their own knowledge base. It's an unpaid pilot, not a customer
engagement, and we're naming neither the firm nor the person who runs it here.
The pattern is worth publishing because it's the clearest outside evidence we
have that a non-technical team can run an AI system like this one day to day
— not a case study, just what happened.

**How it's structured:**

- **14 agents, each defined as one markdown file.** No hidden orchestration
  logic to read — the whole behavior of an agent is the file.
- **Agents never call each other.** They hand off work by writing to named
  markdown files, and the human operator opens those files directly between
  steps. Nothing chains automatically out of sight.
- **A routing-only orchestrator, contractually barred from drafting or
  deciding.** It moves work to the right agent; it does not write content or
  make judgment calls itself.
- **Cite-or-silent pushed into each specialist's own contract** — the same
  rule Heydey enforces at the pipeline level is written into every individual
  agent, not just the top of the stack.
- **"Engine computes, model narrates only."** Anything that's arithmetic or
  deterministic runs as code; the model's job is limited to language, never
  to numbers it could get wrong.
- **An explicit "you do not need the AI for this" carve-out list.** Some
  tasks are named up front as things the operator should just do directly —
  the system doesn't try to insert itself everywhere.

**What running it that way produced**, measured, not estimated: 67 protocols
digitized (~85,600 words) in a single day, 109 pages captured, a test suite
carried from 86 to 90 green, and a 9/9 cross-language parity lock on the
build underneath. We don't have a measurement of hours or cost saved, and
we're not claiming one — this is what was produced, not what it saved.

## Why we show both

The AI-PM flow is Heydey's own shipped surface; the operating pattern above
is a different, private build on the same foundation, run by people who
aren't us. Together they're the honest version of "who is this for": a
non-technical operator who wants cited answers and prepared actions instead
of a confident guess, working through a web app or hand-off markdown files —
never a terminal, once someone has installed it for them.
