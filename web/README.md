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
allowlist with the dashboard's exact HTTPS origin. The static host must rewrite `/dashboard`,
`/upload-context`, and `/ask-context` to `/index.html` so direct browser visits can boot the client.

## Commands

```bash
npm run dev
npm run test
npm run lint
npm run build
npm run check
npm run preview
```

## Access Model

- Platform admins can create users and workspaces and assign owner, editor, or viewer roles.
- A platform admin does not receive workspace document access without a workspace membership.
- Workspace owners can add, change, and remove editors and viewers, but cannot modify owners.
- Editors can upload and use context. Viewers can use context. Unassigned users see catalog
  metadata only.
- Uploading stores the file in the source service and queues background context processing. The
  success message confirms acceptance, not completion of extraction and vector indexing.
- The API remains authoritative for every permission check; hiding a frontend control is not an
  authorization boundary.

The bearer token is held in `sessionStorage`, sent only in the `Authorization` header, and removed
on sign-out or a `401` response. API responses and requests use `no-store` caching. Generated user
passwords are shown once and are not persisted by the frontend.
