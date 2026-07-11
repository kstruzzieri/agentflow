# agentflow — brand assets

Direction: **Gate / Ledger**. The mark is what the product *does* — ledger rails
(plan steps) passing through a validation gate, changing state from pending to
verified. Agentflow's visual language centers on workflow **gates and ledgers**.
Tagline: **Gate the flow. Prove the work.**

The primary accent is **teal `#00D4AA`**. Slate grounds keep the proof output
readable in light and dark contexts. Mint is reserved for runtime
**PASS/success** states rather than general brand use.

## Files

| file | use |
|------|-----|
| `favicon.svg` | browser tab / any ≤16px context. Reduced one-color teal gate (split + arrows get noisy small). |
| `agentflow-mark.svg` | primary mark, **dark** backgrounds. Gray rails in → teal gate → teal rails + arrows out. |
| `agentflow-mark-light.svg` | mark for **light / paper / README** (slate-600 rails + deep-teal gate). |
| `agentflow-mark-mono.svg` | one-color mark (whole glyph in `currentColor`; the state split is dropped). |
| `agentflow-lockup-dark.svg` | mark + wordmark, dark bg. |
| `agentflow-lockup-light.svg` | mark + wordmark, light bg. |
| `agentflow-badge.svg` | README status badge — `agentflow ▏ proof`. |
| `agentflow-social.svg` | 1280×640 open-graph / repo social card. |

## Color

| role | hex | meaning |
|------|-----|---------|
| brand / gate / verified | `#00D4AA` | primary accent, gate, and passed rails |
| deep teal (light bg) | `#0D9488` | teal darkened for contrast on paper |
| rail · pending | `#94A3B8` | slate-gray inbound rails (steps not yet validated) |
| status: PASS | `#86EFAC` | mint — runtime success/PASS only, **never** the brand |
| gate: pending | `#FCD34D` | amber |
| drift / fail | `#FDA4AF` | rose |
| ground (dark) | `#0B1120` / `#111827` | slate ink / surface |
| ground (light) | `#F1F5F9` | slate-50 |

The mark is **one-color-first** — the mono glyph must read before any color or
the gray→teal split is applied.

## Type

- Wordmark + UI: **Archivo** (800 for the wordmark). Not Martian Mono — its lowercase
  `l` reads as `1` (`agentf1ow`). The wordmark's vertical bar `agent│flow` is the
  same teal gate bar as the mark — one shared primitive.
- Commands / artifacts / proof output: a squarish mono (Martian Mono / JetBrains Mono).

## Asset notes

- **Outline the text** in the font-dependent SVGs (`agentflow-lockup-*.svg`,
  `agentflow-social.svg`; `Path > Object to Path` in a vector tool). They
  reference Archivo by name. The marks + favicon are pure geometry — no font
  dependency.
- **Two glyphs on purpose.** `agentflow-mark.svg` carries the gray→teal state
  change (primary, ≥24px). At ≤16px use `favicon.svg`, the reduced one-color gate.
- `agentflow-badge.svg` uses a Verdana/DejaVu stack for predictable metrics (like
  shields.io) and renders standalone; GitHub markdown sanitises inline SVG, so embed
  it as `<img src="…/agentflow-badge.svg">`, not pasted inline.

## Facts (keep canonical)

- repo: `github.com/kstruzzieri/agentflow`
- artifacts: `.agent/proof-pack.json`, `.agent/proof-pack.md`
- requires Python **3.11+**; install editable from source (not published to PyPI):
  `uv tool install --editable /path/to/agentflow`
