# talkie webui & API

A Flask server (`webui.py`) that loads the GPTQ model once and exposes:

1. A browser chat UI at `/`
2. A small HTTP/SSE API for programmatic use
3. An append-only JSONL log (`chats.jsonl`) shared by both

A stdlib-only Python client (`talkie_client.py`) is provided.

The browser UI and any clients hit the **same** server, so an agent's
conversations appear in the sidebar instantly and you can branch / regenerate /
edit them from either side.

## Run

```
python webui.py
```

Listens on `http://127.0.0.1:8000`. The model takes ~30 s to load; the listener
opens once it's ready. Generation runs at ~10 tok/s (no KV cache).

## HTTP API

All bodies are JSON. Streaming endpoints return Server-Sent Events.

| Endpoint | Body | Returns |
|---|---|---|
| `GET /` | — | the browser UI (HTML) |
| `GET /history?conversation_id=<id>` | — | active thread (JSON list) |
| `GET /conversations` | — | sidebar listing (JSON list) |
| `POST /chat` | `{conversation_id, parent_id, message, temperature?, max_new_tokens?}` | SSE stream |
| `POST /regenerate` | `{conversation_id, assistant_id, temperature?, max_new_tokens?}` | SSE stream |
| `POST /select` | `{conversation_id, msg_id}` | empty 200 |
| `POST /stop` | `{request_id}` | empty 200 |

`parent_id` may be `null` for the first message in a new conversation.
Defaults: `temperature=0.7`, `max_new_tokens=400`.

### SSE event types

Each event is one `data: <json>` line followed by a blank line.

```
{"type":"start","request_id":"<hex>","assistant_id":"<hex>","parent_id":"<id>"}
{"type":"delta","text":"…"}
{"type":"done","assistant_id":"<hex>"}
```

`assistant_id` is reserved on `start`, so the client knows the new message id
before the row is committed. Use `request_id` for `/stop`.

### Active-thread rule

For any parent (including the synthetic `null` root), among its children the
**active** child is whichever has the most recent `select` event; if none has
any `select`, it falls back to the most recent child by `ts`. The active
thread is built by walking from `null` and following the active child at each
step.

### Curl examples

```bash
# Start a new conversation
curl -N -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"demo","parent_id":null,"message":"Greet me."}'

# See what's there
curl 'http://127.0.0.1:8000/history?conversation_id=demo'

# Re-roll the last assistant reply (creates a sibling, auto-selects it)
ASST=$(curl -s 'http://127.0.0.1:8000/history?conversation_id=demo' \
  | python -c 'import json,sys; print(json.load(sys.stdin)[-1]["id"])')
curl -N -X POST http://127.0.0.1:8000/regenerate \
  -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"demo\",\"assistant_id\":\"$ASST\"}"

# Swap back to the previous variant
PREV=$(curl -s 'http://127.0.0.1:8000/history?conversation_id=demo' \
  | python -c 'import json,sys; m=json.load(sys.stdin)[-1]; print(m["siblings"][0])')
curl -X POST http://127.0.0.1:8000/select \
  -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"demo\",\"msg_id\":\"$PREV\"}"
```

## Storage: `chats.jsonl`

Append-only. Two event types:

```jsonc
// Message
{"type":"msg","id":"<uuid>","conversation_id":"<id>","role":"user|assistant|system",
 "content":"…","parent_id":"<id|null>","ts":<float>}

// Sibling selection
{"type":"select","conversation_id":"<id>","msg_id":"<id>","ts":<float>}
```

Legacy rows (no `type` field, no `id` — written by an earlier version) are
migrated on read into a linear parent chain with synthetic ids
`legacy-<cid8>-<n>`, so old chats remain navigable as a single thread.

Editing the file by hand is supported:

- Append a `msg` row to splice content in.
- Append a `select` row to pin a sibling.
- To "delete" a branch, point the parent's `select` at a different sibling — the
  unused branch stays on disk but drops out of the active thread.

## Python client (`talkie_client.py`)

Stdlib only — no `pip install` required.

```python
from talkie_client import Talkie

t = Talkie()                                 # new conversation
print(t.send("Greet me formally."))          # blocks, returns full reply

for chunk in t.stream("Compose a sonnet about a typewriter."):
    print(chunk, end="", flush=True)         # token-by-token

t.regenerate()                               # re-roll the last reply
t.history()                                  # list of msgs on the active thread
t.conversations()                            # everything in the sidebar

# Resume something visible in the webui
existing = Talkie(conversation_id="abc123…")
existing.send("And now in French.")
```

`Talkie.stop()` is safe to call from another thread to interrupt an in-flight
`stream()` / `send()`. Whatever was generated so far is persisted as the
assistant message.

`stream(...)` and `send(...)` accept `temperature=…` and `max_new_tokens=…` kwargs.

### CLI

```bash
# One-shot
python talkie_client.py "Greet me formally."

# Interactive REPL — same conversation across turns
python talkie_client.py --repl

# Resume an existing conversation by id (find ids via --list or the sidebar)
python talkie_client.py --list
python talkie_client.py --cid <conversation_id> --repl

# Sampling knobs
python talkie_client.py --temperature 0.9 --max-new-tokens 800 "…"
```

REPL commands:

- `/regen` — re-roll the previous assistant reply
- `/history` — print the active thread
- `/stop` — interrupt (best-effort; works while a reply is streaming)
- Ctrl-D — quit

## Chat template

The server inserts these tokens; the client sends raw text. For reference:

```
<|system|>{system}<|end|>
<|user|>{user 1}<|end|>
<|assistant|>{reply 1}<|end|>
<|user|>{user 2}<|end|>
<|assistant|>
```

Generation stops at any of `<|end|>`, `<|user|>`, `<|assistant|>`, `<|system|>`,
`<|endoftext|>`. The SSE stream strips them; saved `content` does not contain them.
