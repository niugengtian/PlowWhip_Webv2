# Design QA · Task 工作台 Revision 5

## Source and implementation

- Source reference: `/var/folders/h6/v4h175gn2r93rvzvym11x_5m0000gn/T/codex-clipboard-9ee2b7b3-2458-4b91-bb74-64c55146338d.png`
- Source dimensions: 1230 × 1236
- Implementation screenshot: `docs/screenshots/task-workbench-r5-1280x720.jpg`
- Implementation viewport: 1280 × 720, device pixel ratio 2
- Normalized comparison source: `docs/screenshots/task-reference-comparison-1230x720.png`
- Normalized comparison implementation: `docs/screenshots/task-workbench-r5-comparison-1230x720.jpg`
- Comparison dimensions: 1230 × 720
- URL: `http://127.0.0.1:8750/`
- State: project `plowwhip-unattended-probe`, populated Goal, selected `NeedsDecision` Task, four public lanes visible.

## Interaction and runtime checks

- Selecting a project on all seven navigation pages kept the active page unchanged.
- The explicit “进入项目” button is enabled only when a project is selected.
- Goal selection filters the board and switches the right inspector to Goal details.
- Task selection switches the right inspector to Task details.
- The `NeedsDecision` input is enabled for the selected waiting Task.
- Four lanes are visible without horizontal page or board overflow at 1280 × 720.
- Browser console warning/error count: 0.
- Container health: healthy on `127.0.0.1:8750`.

## Findings and fix history

1. P1: the project scope selector navigated to the project page. Fixed by making scope changes refresh the current page and adding a separate explicit navigation button.
2. P1: the Task page was a swimlane panel stacked over unrelated detail panels. Replaced the whole page with top metrics, Goal navigation, four-lane board, and one Goal/Task inspector.
3. P1: `div[hidden]` content remained visible because CSS only covered `section[hidden]`. Fixed with a global, authoritative hidden rule.
4. P2: the right inspector dropped below the board at a 1280-pixel viewport. Moved the two-column breakpoint to 1120 pixels and reduced only the board minimum width.
5. P2: four canonical lanes required horizontal board scrolling. Reduced the lane minimum to keep all four visible while preserving rich cards.
6. P2: the reference used richer cards and stronger lane separation than the first implementation. Matched the dark palette, fine borders, status treatments, card hierarchy, and dense operational metadata while retaining the V1 four-state model.

## Result

No open P0, P1, or P2 visual/interaction findings remain for the tested desktop state.

final result: passed
