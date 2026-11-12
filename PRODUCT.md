# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Workspace members at an organization: people at a desk doing repeated small retrieval jobs — find the document, check the number, cite the source — and occasional deeper agent runs over the corpus. Admins (a second, smaller audience) manage users, workspaces, and the agent model. Confirmed by the owner 2026-11-07: internal enterprise tool, not consumer.

## Product Purpose

Retriv is a self-hosted enterprise document-retrieval platform. It ingests documents (PDF, DOCX, CSV, XLSX, PPTX), extracts and indexes them into Postgres (pgvector for semantic search, document_rows for tabular SQL search), serves citation-backed retrieval, and provides an agent chat whose tool calls search that corpus with per-workspace isolation. Success: a member gets a trustworthy answer with its source in seconds, and can verify it by opening the document.

## Positioning

Answer-and-provenance as one motion: retrieval runs inside Postgres with row-level security per workspace, and every answer traces back to the exact document, row, or extracted image that produced it — the citation is the contract, not a garnish. A neighboring product could copy the chat; it could not truthfully copy the isolation-and-provenance pipeline behind it.

## Operating Context

Desktop web first (dev: API :9000, web :9500). Global document search on ⌘K from the shell. An upload pipeline runs asynchronously — states progress from fetching through extraction to ready, surfaced as notifications. Documents preview in place: PDF pages, XLSX sheets (SheetJS), DOCX, extracted images. The agent chat is a full page and the default view after login; it streams answers, shows the tool trail (which search/SQL/document tools ran), and renders markdown. Admins get users/workspaces/agent-model panels; admin mode is a toggle over the same shell. Light and dark themes both ship, driven by one token layer (`web/src/index.css`).

## Capabilities and Constraints

- Confirmed scope 2026-11-07: redesign covers the **whole app** (shell, chat, documents/previews, admin panels, login) as one coherent world.
- Confirmed ambition 2026-11-07: **new visual world** — the steel-navy/paper identity may be fully replaced; product truth, behavior, and working features stay.
- Standing constraints re-confirmed 2026-11-07: keep **light + dark themes**; keep a **formal, enterprise register** (not playful SaaS); **no warm cream backgrounds** (cold/neutral grounds).
- Multi-workspace model: a user may belong to several workspaces with roles (owner/member/viewer); admins see all workspaces in an admin mode.
- Extraction is honest about states (fetching / awaiting parser / extracting / ready); the UI must keep representing processing truthfully.

## Brand Commitments

- Name: **Retriv**.
- Register: formal enterprise instrument — calm, precise; brand lives in precise details, not decoration.
- Ground rule: cold/neutral surfaces; warm cream page backgrounds are rejected by the owner.

## Evidence on Hand

Real UI in `web/src/` (AppShell, AgentChatPage, WorkspaceDocuments, admin panels, token layer in `web/src/index.css`). Real product content types: documents with names/types/statuses, workspace member rosters, processing runs, chat transcripts with tool trails, extracted-image galleries. No marketing site, no testimonials, no press — nothing fabricated.

## Product Principles

1. Provenance is the interface — answers, previews, and citations are first-class, never subordinate chrome.
2. Calm instrument — the UI should feel like precise equipment; states are truthful, never theatrical.
3. Density where work happens, air where decisions happen — tables and tool trails compact, chat and reading generous.
4. One world, both modes — light and dark are the same system re-grounded, not two designs.
