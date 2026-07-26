# NOCTURNE — Token Sheet v0 (motion.so feed)

**Source of truth**: HEYDEY-BUILD-DOC-FINAL-2026-07-14.md §4.8 (L33) + HEYDEY-VISION-v2-DEMO.md §6.
**Status**: LOCKED items are from the build doc verbatim. Values marked *(proposed v0)* are
concrete numbers supplied for motion.so production — creative-director/CEO may re-tune them;
the locked principles may not change without a Fable amendment.

**One line**: *"Linear meets an observatory."* Dark instrument. Light is data.

---

## 1. Color

| Token | Value | Role | Status |
|---|---|---|---|
| `field` | `#0A0F1E` | the page/desktop field — deep navy, never pure black | **LOCKED** |
| `plate` | `#0D1426` | squircle plate background | *(proposed v0 — in use on the live S0 page)* |
| `plate-border` | `#1C2436` | 1px hairline plate edge | *(proposed v0)* |
| `text` | `#E6EAF2` | primary text | *(proposed v0)* |
| `text-muted` | `#8A94A8` | secondary text, receipt meta | *(proposed v0)* |
| `text-faint` | `#4A5468` | tertiary/disabled | *(proposed v0)* |

### Confidence = light temperature (teal → amber) — LOCKED principle
Confidence renders as **light temperature**, never as red/green status colors.

| Token | Value | Meaning | Status |
|---|---|---|---|
| `conf-validated` | `#4FD8C4` | validator-passed · high confidence | *(proposed v0)* |
| `conf-high` | `#6FCFA9` | 0.80–0.94 | *(proposed v0)* |
| `conf-mid` | `#9CC487` | 0.65–0.79 | *(proposed v0)* |
| `conf-low` | `#C9B45F` | 0.50–0.64 | *(proposed v0)* |
| `conf-warn` | `#E8A13C` | <0.50 · `validator-degraded` · `UNVALIDATED — offline` | *(proposed v0)* |

Badges: `validator-degraded` and `UNVALIDATED — offline` always render in `conf-warn` with the
label spelled out — the labeled badge is the ONLY permitted gate bypass (Executor Contract A).

## 2. Typography

| Use | Spec | Status |
|---|---|---|
| UI | SF Pro / system-ui; Inter fallback | *(proposed v0)* |
| **Receipts / breadcrumbs** | **mono (SF Mono / ui-monospace), 11px** | **LOCKED** |
| Titles | −0.022em letterspacing at large sizes | *(proposed v0, from the BLOS design-brief lineage)* |
| Body / mini | −0.010em to −0.013em | *(proposed v0)* |

Receipt line format (render verbatim): `[KB: <file> · chunk <n> · <date> · score <s>]`

## 3. Shape & layers

- **Squircle plates** — radius 16px *(proposed v0)*, `plate` fill, 1px `plate-border`. **LOCKED**: no flat tiles, no sharp corners, no visible gutters.
- **Z-layer drill-down** — depth, not navigation: click = the plate lifts and its detail layer rises beneath it (shadow deepens, field dims 6%). **LOCKED** principle.
- **No drag handles. Pin is the only manual act. Reflow only at session boundaries.** **LOCKED**.
- **Glass is an accent, not a field** (macOS 27 legibility lesson): vibrancy only on the Summon slab and momentary overlays. **LOCKED**.

## 4. The three signature motions — LOCKED (these carry the premium feel)

| Motion | Spec | Where in film |
|---|---|---|
| **Summon** | ⌘⇧H → a 200ms slab drops in with native vibrancy; content settles in one beat, no bounce | F1 cold open |
| **Reveal** | answer sentences illuminate their receipts in a stagger (~60ms/receipt *(proposed)*); ends with the **validator-seal stamp** (executor→validator badge lands with a single 120ms scale-settle) — the trust claim rendered as motion | F1, F3 |
| **Breath** | ≤1% idle luminance glow on the field, ~6s cycle *(proposed)*, pure CSS — the system is alive, not busy | F4 graph, idle beats |

Motion grammar elsewhere: 150–250ms, ease-out, one movement per beat. Nothing loops except Breath.

## 5. Kill list — LOCKED
Flat tiles · sharp corners · visible gutters · the word "Lumia" · red/green confidence ·
drag handles · glass fields · any silent state (every surface ships loaded / empty-with-CTA /
error-with-next-step / ingesting).
