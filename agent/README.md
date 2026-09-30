# Kerjakan Agent

`kerjakan-agent` verifies Kerjakan's short-lived workspace scope and runs a bounded PydanticAI tool
loop against an OpenRouter-compatible chat endpoint. Its registered `search_context` tool calls the
existing server-side retrieval wrapper, which reads Qdrant and the internal PostgreSQL database
directly. For non-greeting messages, the first model call must select an evidence tool; the model
chooses the tool and query, while the harness rejects a final answer unless a relevant evidence
item was returned. It may request one follow-up search. Each turn is capped at two tool calls and
three model requests. Kerjakan owns conversation history and sends only scope-eligible prior turns
to the agent; the agent does not persist sessions or tool-return text.

## Request flow

```text
browser --user JWT + conversation_id + message--> kerjakan
kerjakan --M2M + signed scope + eligible history + message--> agent
PydanticAI --required first search_context(query)--> wrapper --> embedding + Qdrant + PostgreSQL
PydanticAI --optional follow-up search_context(query)--> same wrapper and scope
agent --answer + source references--> kerjakan
kerjakan --persists turn and metadata only--> its primary PostgreSQL
kerjakan --private conversation response--> browser
```

Kerjakan computes `search_context` memberships when the request begins and signs those workspace
IDs with the user ID, auth version, issuer, audience, and expiry. It signs with a private RSA key;
the agent verifies with the corresponding public key. The agent accepts no workspace IDs from the
question or model. The tool schema accepts only a query; the verified workspace scope is injected
server-side into every retrieval.

The wrapper first filters Qdrant by the signed workspace IDs and embedding profile, and checks the
returned payload metadata against the scope. Qdrant contains identifiers/profile/hash metadata,
not chunk text. The wrapper fetches text from PostgreSQL only for candidate chunk IDs and verifies
workspace, non-deleted document, active processing run, indexed embedding profile/model/dimensions,
Qdrant point ID, and content hash. An out-of-scope Qdrant result is forbidden; any inconsistent
result aborts retrieval. Matches below `AGENT_MIN_RELEVANCE_SCORE` are treated as no evidence; tune
that cosine-score threshold against the indexed corpus. No rejected or low-relevance chunk reaches
the LLM.

Conversations and turns are private to their owning user and stored in Kerjakan's primary database.
The agent receives eligible prior user questions, workspace provenance IDs, and a safe marker for
prior document answers—not source references or old document-derived answer text. It does not persist
chat sessions or retrieved chunk text. Kerjakan stores final answers and source references, not tool-return
contents; a saved answer may quote retrieved material. On each message it computes a fresh workspace
scope and omits historical turns whose source workspaces are no longer accessible. Membership
changes apply to the **next turn**; an in-flight turn uses its request-time scope.

## Local Linux setup

The agent reads its ignored `.env` file. Do not commit either service's `.env` or generated keys.

1. Generate a separate RSA key pair. Keep the private key only in Kerjakan's environment and put
   only the public key in the agent's environment:

   ```bash
   umask 077
   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
     -out "$HOME/.kerjakan-agent-scope-private.pem"
   openssl pkey -in "$HOME/.kerjakan-agent-scope-private.pem" -pubout \
     -out "$HOME/.kerjakan-agent-scope-public.pem"
   base64 -w0 "$HOME/.kerjakan-agent-scope-private.pem"
   base64 -w0 "$HOME/.kerjakan-agent-scope-public.pem"
   ```

   Set the resulting values as `APP_AGENT_SCOPE_PRIVATE_KEY_B64` in `kerjakan/.env` and
   `AGENT_SCOPE_PUBLIC_KEY_B64` in this project's `.env`. They must come from the same pair.

2. Configure a PostgreSQL read-only login for the three retrieval tables. Run as the database
   administrator, replacing the password placeholder with a generated password:

   ```sql
   CREATE ROLE kerjakan_agent_readonly LOGIN PASSWORD 'replace-with-a-generated-password';
   GRANT CONNECT ON DATABASE kerjakan_internal TO kerjakan_agent_readonly;
   GRANT USAGE ON SCHEMA public TO kerjakan_agent_readonly;
   GRANT SELECT ON TABLE public.source_documents,
       public.document_chunks,
       public.chunk_embeddings TO kerjakan_agent_readonly;
   ALTER ROLE kerjakan_agent_readonly SET default_transaction_read_only = on;
   ```

   Use this account in `AGENT_DATABASE_URL` with the `kerjakan-internal` database. URL-encode any
   special characters in the password. Do not grant write privileges or access to other databases.
   Production agent connections require PostgreSQL TLS with certificate-chain and hostname
   verification; use a database certificate trusted by the agent container's system trust store
   and a matching hostname in `AGENT_DATABASE_URL`.

3. Configure independent service credentials and matching retrieval settings:

   - Set `INTERNAL_QDRANT_READ_ONLY_API_KEY` in `kerjakan-internal/.env` and restart Qdrant. Set the
     same value as `AGENT_QDRANT_API_KEY`. Qdrant's read-only key restricts writes; the wrapper's
     signed-scope filters and metadata checks enforce workspace authorization.
   - Set `AGENT_EMBEDDING_BASE_URL`, `AGENT_EMBEDDING_API_KEY`, `AGENT_EMBEDDING_MODEL`,
     `AGENT_EMBEDDING_PROFILE`, and `AGENT_EMBEDDING_DIMENSIONS` to match the indexed profile in
     `kerjakan-internal`.
   - Set `AGENT_QDRANT_URL` and `AGENT_QDRANT_COLLECTION` to the internal Qdrant instance and
     indexed collection.
    - Match `AGENT_API_TOKEN` here to `APP_AGENT_API_TOKEN` in `kerjakan/.env`. Set the OpenRouter
      chat base URL to `https://openrouter.ai/api/v1`, then configure the key and model using
      `AGENT_CHAT_BASE_URL`, `AGENT_CHAT_API_KEY`, and `AGENT_CHAT_MODEL`. Use a `provider/model` ID
      whose OpenRouter metadata lists both `tools` and `tool_choice`; the first model call requires
      an evidence tool. Check candidates with:

      ```bash
      curl 'https://openrouter.ai/api/v1/models?supported_parameters=tools'
      ```

4. Make sure the PostgreSQL, Qdrant, embedding, chat, and Kerjakan services are available, then
   start the agent:

   Set `AGENT_ENVIRONMENT=production` in production deployments. The environment mode is required;
   production enables PostgreSQL TLS verification and rejects placeholder credentials.

   ```bash
   make run
   ```

   The local Compose file uses Linux host networking; the agent binds to `127.0.0.1:8090` and
   connects to the host-published PostgreSQL and Qdrant ports. For other deployment platforms, use
   private service networking and configure the corresponding internal URLs.

5. Open `http://127.0.0.1:5173/ask-context` in the dashboard. A document must finish extraction
   and indexing before it can appear in an answer.

## Checks

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy agent tests
```
