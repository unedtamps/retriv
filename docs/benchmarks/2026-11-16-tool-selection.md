# Agent tool selection: use supplied document ids directly

**Date:** 2026-11-16
**Harness:** `scripts/eval_tool_selection.py`
**Model:** `deepseek-v4.1-flash` (`opencode-go`), temperature 0, one repeat
**Prompts:** 61 (55 clear, 6 ambiguous)

```
uv run python scripts/eval_tool_selection.py --repeats 1 --temperature 0
```

| Metric | Before | After |
|---|---:|---:|
| First-call accuracy | 56.4% | **92.7%** |
| Coverage (any call) | 96.4% | **100.0%** |
| Wrong-tool rate | 46.9% | **8.2%** |
| Leading orientation | 22.4% | **2.0%** |
| Calls per turn | 1.48 | **1.20** |
| Ambiguous accuracy | 66.7% | **100.0%** |
| Average latency | 3723 ms | **2711 ms** |

## What changed

1. **`workspace_id` is optional in the harness tool schema** for
   `read_document`, `get_document_outline`, `describe_document`,
   `list_images`, `get_image`, and `get_document_link`. This matches the MCP
   surface and the toolkit, both of which already accept `workspace_id=None`
   and resolve the document through every workspace the caller can read.
2. **Dispatch accepts the missing workspace** and passes `None` to the
   toolkit instead of rejecting the call.
3. **The system prompt** tells the model to use a supplied
   `document_id`/`workspace_id` directly, not to resolve it again with
   `find_documents` or `list_workspaces`. It also names questions about
   Retriv itself and requests to draft text as direct answers.
4. **The eval fixture** expected the removed tool name `download_document`;
   it now expects `get_document_link`.

The first two changes are the large win: most misses were prompts that
already contained a `document_id` but not a `workspace_id`, so the model
called `find_documents` first only to discover the workspace. An intermediate
prompt-only run reached 63.6% first-call accuracy; making the workspace
optional raised it to 90.9%, and the meta-answer clause raised it to 92.7%.

## Remaining misses

- `query_rows` first-call is 25%. `list_queryable_files` still tells the model
  to call it before `query_rows`, and that call is a deliberate prerequisite
  when the column names are unknown. Coverage is 100%.
- `find_documents` first-call is 80% for a question that names a workspace;
  `list_workspaces` is a reasonable first step there. Coverage is 100%.

## Caveats

- One repeat per prompt. The model is not deterministic even at
  temperature 0, so treat the exact percentages as a direction, not a
  laboratory measurement.
- The eval measures selection only, not execution. The execution path is
  covered by `tests/test_agent.py` and `tests/test_mcp.py`.
