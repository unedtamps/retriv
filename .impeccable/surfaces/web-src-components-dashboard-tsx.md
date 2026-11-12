---
version: 2
slug: "web-src-components-dashboard-tsx"
primary_target: "web/src/components/Dashboard.tsx"
related_targets: ["web/src/components/AppShell.tsx","web/src/components/AgentChatPage.tsx","web/src/components/WorkspaceOverview.tsx","web/src/components/HelpView.tsx"]
---

## Direction contract

THESIS: Every surface is an opaque pane laid on a backlit table — retriv as a light table for document work. The category default (SaaS dashboard of translucent cards on white) is refused; panes sit on a glowing bed, states are stamped chips, and structure is carried by ruled tables and tick rails, not shadows.

OWN-WORLD: backlit bed (radial glow #F2F6FA→#E4EAF1 light, #15202E→#0B1017 dark); opaque panes #FFFFFF / #131B24 with hairline borders and 2–3px corners; ink #1B2733; vermilion #B3402A as the single urgent/action accent; bottle green #2E6B4F for ready; slate blue for in-progress. Barlow for interface, Barlow Semi Condensed for caps labels and titles, Overpass Mono for codes and counts. Reversed-plate active nav (ink plate, pane text). No warm cream, no glass, no illustration.

STORY: a member reads a document's state as a chip, verifies an answer by its mono code chips and citations; an admin runs the platform from the same plates without the register voice breaking.

FIRST VIEWPORT: chat as default view — sessions pane and transcript pane floating on the bed, composer strip pinned under a hairline, tool trail folded to one mono line; ported live into web/src and verified in the browser (light + dark) against the retriv-option-c mock set.

FORM: assigned direction — the light table, Option C of the mock round (A sheet-register declined after C won; B kept as reference only). The port kept every token NAME and the ARIA contract; only values, fonts and component classes changed.
SEED REFERENCE: seed:dc14c36c73cc20a9

FINISH: shipped 2026-11-07 — token layer in web/src/index.css, Help reference view added, npm check green (80/80), browser-verified in both themes against the live API.
