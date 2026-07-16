# Design QA

**Source visual truth path**

`/private/tmp/edict-reference/docs/screenshots/01-kanban-main.png`

**Implementation screenshot path**

`/private/tmp/plow-whip-board-s8-viewport.png`

**Viewport**

1280 × 720 desktop, dark theme.

**State**

Plow Whip empty global task board with live Docker health, embedded Crontab and Provider status loaded. The Edict source contains seeded tasks, so task-card content is not treated as a fidelity requirement; shell, density, hierarchy and board structure are compared.

**Full-view comparison evidence**

`/private/tmp/plow-whip-design-compare-s8.png`

- Both use a near-black canvas, narrow product header, horizontal compact tab navigation, bounded central workspace and high-density panels.
- The Plow Whip metrics strip and four-column task state board preserve the source's control-console rhythm while using product-specific states.
- No horizontal overflow, clipped persistent controls or hidden primary actions were visible at 1280 × 720.

**Focused region comparison evidence**

`/private/tmp/plow-whip-design-focus-s8.png`

- Header, status indicators, tab density, primary action placement, panel borders and kanban column proportions were compared in the shared 1280 × 400 crop.
- Phosphor icons replace emoji/text glyphs and remain visually consistent across the navigation and cards.

**Required fidelity surfaces**

- Fonts and typography: system sans fallbacks match the compact neutral source; UI labels use 10–13px optical sizes, clear weights and no unintended wrapping.
- Spacing and layout rhythm: 58px header, 43px tabs, 9–14px panel gaps and four equal board tracks create the intended dense console rhythm.
- Colors and visual tokens: source-aligned `#07090f`, `#0f1219`, `#141824`, `#1c2236`, blue/violet accents and restrained semantic colors are applied consistently.
- Image quality and assets: this screen has no raster imagery. All visible icons come from the installed Phosphor icon library; no emoji, inline SVG art, CSS illustration or placeholder asset is used.
- Copy and content: the interface is Chinese-first. Technical identifiers such as Worker, Provider, Token, CLI and Crontab remain where they are product terms.

**Findings**

- No actionable P0, P1 or P2 mismatch remains.

**Open Questions**

- None blocking. Seeded real tasks will naturally make the board visually denser than this empty-state capture.

**Primary interactions tested**

- Switched between task board, Provider and Settings tabs.
- Opened and closed the new-task drawer.
- Confirmed live Codex/Cursor Provider status and the truthful unavailable simple-worker state.
- Confirmed Crontab settings and Convention refinement controls render with correct disabled/active states.
- Browser warning/error console checked: 0 entries.

**Comparison history**

- Pass 1: no actionable P0/P1/P2 differences were found in the full-view or focused comparison, so no visual correction loop was required.

**Follow-up polish**

- P3: add optional seeded demo tasks only for marketing screenshots; do not add them to production runtime data.

final result: passed
