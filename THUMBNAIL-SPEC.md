# Fendrisk Security Preflight — Thumbnail / OG image spec (Forged Signal)

Rendered file: `og-image.png` — 1280 x 720, PNG, sRGB.
Source: `tools/thumb.html` (Space Grotesk + Space Mono embedded as base64
woff2, so it re-renders with no network access).

## Canvas
- 1280 x 720 (16:9). Safe area: 84px left/right, 72px top/bottom.
- Background: charcoal `#1a1a1a` (the product's own `--bg`).
- Top edge: 5px amber `#e89b3e` full-bleed rule.
- Amber radial glow, 780px, top-right, `rgba(232,155,62,.16)` → transparent at 68%.
- Second glow, 560px, bottom-left, `rgba(232,155,62,.08)` → transparent at 70%.
- Grid overlay: 64px squares, amber 1px lines, opacity .05.

## Type
- Headline — Space Grotesk 700, 86px, line-height 1, tracking -.034em.
  "Check your AI-built app / **before** you ship it." — *before* in amber
  `#e89b3e`, remainder in `#e8e4de`.
- Subhead — Space Grotesk 500, 24px, line-height 1.42, `#9a9590`, max-width 760px.
- Eyebrow — Space Mono 400, 16px, tracking .3em, `#9a9590`, uppercase.
- Brand lockup — Space Mono 700, 15px, tracking .24em, amber.
- Stat — Space Grotesk 700, 80px amber "29"; Space Mono 13px `#9a9590` "CHECKS".

## Components
- Logo mark: 46px rounded square (11px radius), amber fill, charcoal shield glyph.
- Chips: Space Mono 14px, tracking .1em, 1px `#4a4038` border, 999px radius,
  11px/18px padding, `rgba(232,155,62,.05)` fill.
  Copy: SINGLE HTML FILE · RUNS OFFLINE · NOTHING LEAVES YOUR DEVICE.

## Rules
- Amber is the accent only — never the background, never body copy.
- The stat must match the real check count. It is 29. If checks are added or
  removed, update the thumbnail, the `<title>`/meta, the header copy and
  README together.
- No stock photography, no faces, no invented badges, no "certified" or
  "audited" language — the product is explicitly not an audit.

## Re-render
    node tools/render-thumb.mjs     # writes og-image.png at 1280x720
