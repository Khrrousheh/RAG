# AI Contract

This document defines the behavioral contract for the policy assistant. Changes
to retrieval, prompts, fallback behavior, model configuration, or UI streaming
should preserve this contract unless the contract itself is intentionally
updated.

## Purpose

The assistant helps users answer questions about company policies using only the
policy passages retrieved from Qdrant. It should be practical, cite sources, and
make uncertainty clear.

## Non-Goals

- It is not a legal, HR, security, or compliance authority.
- It must not invent policy names, section numbers, approvals, owners, or dates.
- It must not answer from general world knowledge when retrieved policy context
  is missing or insufficient.
- It must not expose secrets, credentials, private system details, or unrelated
  local runtime data.

## Inputs

### Chat Request

`POST /chat` and `POST /chat/stream` accept `ChatRequest`:

| Field | Contract |
| --- | --- |
| `session_id` | Optional authenticated chat session. If omitted, the backend creates or reuses an active session. |
| `message` | Required user question, 1 to 4000 chars. |
| `policy` | Optional policy name or alias filter. |
| `department` | Optional exact department filter. |
| `version` | Optional exact version filter. |
| `effective_date_from` | Optional lower effective-date bound. |
| `effective_date_to` | Optional upper effective-date bound. |
| `top_k` | 1 to 10; backend caps values at `MAX_TOP_K`. |
| `use_llm` | When `false`, return deterministic fallback output. |
| `history` | Up to 8 turns; prompt includes the most recent 6 turns. |

### Search Request

`POST /search` accepts `SearchRequest` with the same filter fields plus a
required `query`.

## Retrieval Contract

Retrieval must:

1. Use the configured embedding model for semantic search.
2. Use normalized policy aliases to resolve named policies when possible.
3. Preserve exact metadata filters for policy file name, department, version,
   and effective-date range.
4. Return `Source` records with stable per-response IDs starting at 1.
5. Include source metadata when available: policy name, file name, page,
   department, version, effective date, policy title, score, and text.
6. Avoid returning vectors to clients.
7. Cap source count at effective `top_k`.

For public-sharing questions, retrieval may expand the query with related terms
such as disclosure, confidentiality, source code, credentials, personal data,
architecture, approval, and acceptable use.

## Source Contract

Every policy claim in an LLM answer should cite a retrieved source ID such as
`[1]`. Source IDs are valid only within a single response.

Source display text should be the document stem when `file_name` is present,
otherwise the policy title.

If no sources are retrieved, the assistant must say that matching policy
passages were not found and suggest removing filters or asking with more
specific policy terms.

## Prompt Contract

The prompt should include:

- recent conversation history;
- selected policy context;
- the user question;
- instructions to answer only from supplied context;
- instructions to cite claims with source bracket numbers.

Prompt construction should:

- deduplicate identical or near-duplicate source passages;
- include at least `PROMPT_MIN_SOURCES` when available;
- include at most `PROMPT_MAX_SOURCES`;
- respect `PROMPT_CONTEXT_MAX_CHARS`;
- prefer concise, practical answers.

The current target answer length is 180 to 220 words unless the question is
simpler.

## Answer Contract

Answers should:

- directly address the user's question;
- be grounded in retrieved policy context;
- cite policy-based recommendations with source IDs;
- include policy title, department, version, or effective date when relevant;
- distinguish allowed actions, not-allowed actions, approvals, and escalation
  steps when useful;
- say what is missing when context is insufficient.

Answers should not:

- fabricate policy details;
- cite sources that were not returned;
- bury uncertainty;
- provide instructions that bypass policy, security, or approval requirements.

## Fallback Contract

Fallback is used when:

- `use_llm=false`;
- the model service is unavailable;
- model generation returns an empty answer;
- streaming fails before any useful generated answer is available.

Fallback output should:

- still return sources;
- summarize the strongest retrieved passages when possible;
- clearly warn that model generation was unavailable;
- never invent missing policy interpretation.

The public-sharing fallback may provide a practical sanitized-post checklist,
but it must still cite available policy-like sources when they are retrieved.

## Streaming Contract

`POST /chat/stream` returns newline-delimited JSON with media type
`application/x-ndjson`.

Each line is one JSON object with an `event` field.

### `session`

Emitted before sources when authenticated chat creates or selects a persisted
session.

```json
{
  "event": "session",
  "session": {
    "id": "...",
    "title": "...",
    "status": "active"
  }
}
```

### `sources`

Emitted after retrieval and before model tokens.

```json
{
  "event": "sources",
  "sources": [],
  "warnings": []
}
```

### `token`

Emitted for generated text chunks or fallback text.

```json
{
  "event": "token",
  "content": "..."
}
```

### `warning`

Emitted for non-fatal warnings.

```json
{
  "event": "warning",
  "message": "..."
}
```

### `metrics`

Emitted near the end of a stream.

```json
{
  "event": "metrics",
  "metrics": {
    "retrieval_ms": 0,
    "prompt_build_ms": 0,
    "embedding_ms": 0,
    "embedding_cache_hit": true,
    "qdrant_ms": 0,
    "resolve_policy_ms": 0,
    "post_filter_ms": 0,
    "llm_ttft_ms": 0,
    "llm_total_ms": 0,
    "total_ms": 0,
    "source_count": 0,
    "prompt_source_count": 0,
    "context_chars": 0,
    "prompt_chars": 0,
    "answer_chars": 0,
    "stream": true,
    "fallback": false
  }
}
```

### `done`

Emitted when the stream is complete.

```json
{
  "event": "done"
}
```

### `error`

Emitted when chat preparation fails before normal output can continue.

```json
{
  "event": "error",
  "message": "Chat preparation failed."
}
```

Clients should tolerate missing metrics fields because fallback paths and older
backends may not populate every metric.

## Safety And Privacy Contract

- Treat policy PDFs, Qdrant payloads, retrieved chunks, and generated answers as
  private local company data.
- Treat user accounts, refresh tokens, chat turns, Redis short-term memory, and
  Qdrant `user_memories` as private user data.
- Do not log full prompts, full policy text, secrets, credentials, tokens, or
  private user data unless explicitly sanitized.
- Protected chat and session APIs must enforce `user_id` isolation. Long-term
  memory retrieval must filter by `user_id` and future `tenant_id` before
  ranking or prompt inclusion.
- Do not commit `policies/`, `qdrant_data/`, `.env`, model files, or private
  benchmark outputs.
- For public-sharing questions, default to sanitized, high-level guidance and
  approval/escalation steps.
- If a user asks for disclosure of sensitive operational details, answer only
  with policy-grounded safe handling guidance.

## Metrics Contract

Latency metrics are diagnostic. They should not change assistant semantics.

Expected metric categories:

- retrieval total;
- policy alias resolution;
- embedding latency and cache hit/miss;
- Qdrant latency;
- post-filter latency;
- prompt build latency;
- LLM time to first token;
- LLM total time;
- answer and prompt character counts.

The benchmark script may compare these fields across P0/P1 optimization work.

## Change Control

Before changing prompt behavior, retrieval filters, source formatting, or stream
event names, update this contract and the README/API documentation together.

Any model swap should be benchmarked against:

- `/metadata`;
- `/search`;
- cached `/search`;
- `/chat` with `use_llm=false`;
- `/chat/stream`;
- direct model streaming.
