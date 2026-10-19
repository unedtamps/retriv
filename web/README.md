# Retriv Control Plane

Vite, React, and TypeScript dashboard for Retriv identity and workspace administration.
The browser routes `/dashboard` and `/upload-context` share the same login session. Open
`/upload-context` directly to sign in and submit a PDF or DOCX to a workspace where you are an
owner or editor.

## Requirements

- Node.js `^20.19.0` or `>=22.12.0`
- The Retriv API running on `http://127.0.0.1:8000`
- An admin user provisioned in the Retriv database

## Local Development

The development server proxies `/v1` to the local API, so no frontend environment variable is
required:

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:5173/dashboard`. The API must allow that exact origin. The default backend
example configuration already contains:

```dotenv
APP_CORS_ALLOWED_ORIGINS='["http://127.0.0.1:5173","http://localhost:5173"]'
```

## Production API

Set `VITE_API_BASE_URL` at build time when the API is not served from the same origin:

```bash
VITE_API_BASE_URL=https://api.example.com npm run build
```

The URL must not end in `/v1`; requests append `/v1/...` themselves. Configure the backend CORS
allowlist with the dashboard's exact HTTPS origin. The static host must rewrite `/dashboard` and
`/upload-context` to `/index.html` so direct browser visits can boot the client.

## Commands

```bash
npm run dev
npm run test
npm run lint
npm run build
npm run check
npm run preview
```

## Design system

The interface is described once, in `src/index.css`, and everything else is Tailwind utilities
over those tokens.

**Tokens** (`@theme` in `src/index.css`): three surfaces (`paper`, `surface`, `sunken`), two
lines (`line`, `line-strong`), three inks (`ink`, `ink-muted`, `ink-faint`), one accent in two
tones (`accent` for fills, `accent-ink` for text, borders and focus rings), and the status
colours (`success`, `danger`, `warn`), which are semantic rather than brand. The type scale is
two steps: `text-eyebrow` for the small mono labels and `text-meta` for counts and timestamps.

**Rules**

- One accent. Lime is the brand colour and the only interactive highlight; a second green used
  for focus rings and active states was competing with it.
- No raw colour values in a component. If a colour is needed, it is a token.
- No arbitrary values (`text-[0.68rem]`, `bg-[#17231f]`). The scale is in the theme, so it can
  be changed in one place.
- The dark surface is the sign-in page and nothing else. Hand-painted dark panels meant one
  colour meant two things depending on where it was used.

**Classes** (`@layer components`): `btn` and its variants, `label`, `field`, `panel`, `table`,
`eyebrow`, `badge`, `error-note`, `empty`, `toast`, `skip-link`, `nav-link`, `choice`. Twelve
classes carry the whole interface.

**Components** (`src/components/ui/`): `Panel` and `PanelHeader`, `DataTable` (a table that
becomes a list of cards on a narrow screen), `SearchField`, `EmptyState`, `SecretWarning`,
`CopyButton`, `BrandMark`. `src/hooks/`: `useApiResource` (one fetch with the abort and the
sign-out handling, instead of seven copies of it), `useProcessingRuns`, `useIsDesktop`.

The navigation is one element: a permanent rail from `lg` up, an off-canvas drawer below it.
Which one it is decides whether it is a navigation landmark or a modal dialog, and that is
decided by `useIsDesktop` rather than by CSS.

## Access Model

- Platform admins can create users and workspaces and assign owner, editor, or viewer roles.
- A platform admin does not receive workspace document access without a workspace membership.
- Workspace owners can add, change, and remove editors and viewers, but cannot modify owners.
- Editors can upload, download, and delete documents. Viewers can read them but cannot download
  or delete. Unassigned users see catalog metadata only.
- Uploading stores the file in the source service and queues background context processing. The
  success message confirms acceptance, not completion of extraction and vector indexing.
- The API remains authoritative for every permission check; hiding a frontend control is not an
  authorization boundary.

The bearer token is held in `sessionStorage`, sent only in the `Authorization` header, and removed
on sign-out or a `401` response. API responses and requests use `no-store` caching. Generated user
passwords are shown once and are not persisted by the frontend.
