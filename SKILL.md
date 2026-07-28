---
name: cstcloud-model-api
description: Call the China Science and Technology Cloud (CSTCloud/中国科技云) OpenAI-compatible model API at uni-api.cstcloud.cn for model discovery, chat and multimodal inference, embeddings, reranking, and DeepSeek OCR. Use when the user mentions CSTCloud, 中国科技云大模型 API, uni-api.cstcloud.cn, the authorized CSTCloud models, or asks to invoke these services while bypassing a local Clash or other HTTP proxy.
---

# CSTCloud Model API

Use the bundled client for all calls unless the task specifically requires another HTTP library:

```powershell
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" <command> ...
```

## Mandatory rules

1. Bypass every configured network proxy. The bundled client opens a direct connection and ignores `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and the local Clash port `7897`. When using curl instead, add `--noproxy "*"`.
2. Read credentials from the protected local key pool. On Windows, stored keys are encrypted with current-user DPAPI at `%APPDATA%\CSTCloud\api-keys.json`. Environment variables remain supported as a fallback. Never print, log, embed in source, pass in command arguments, or return keys to the user.
3. Prefer the first key automatically. On HTTP 401, 403, or 429, try the next key without asking the user to resend credentials. Use exponential backoff for transient network errors and HTTP 408, 425, 429, 500, 502, 503, and 504.
   - When the user supplies a new key, add it to the protected pool through `keys add --stdin`, report only its fingerprint, and never repeat the key in the response.
4. Use only an authorized model unless the user explicitly says their authorization changed. Current authorized models:
   - Chat/multimodal: `gpt-oss-120b`, `qwen3.5`, `deepseek-v4-flash`, `minimax-m27`
   - Embeddings: `bge-large-zh:latest`, `gte-qwen2:7b`, `qwen3-embedding:8b`
   - Rerank: `bge-reranker-v2-m3`, `qwen3-reranker:8b`
   - OCR service: `deepseek-ocr`
5. Query `models` when exact availability matters; do not assume the returned model list is stable. If a requested model is absent, stop and ask the user to select from the currently returned, task-compatible alternatives.
6. Treat model reasoning fields as optional. Do not expose hidden reasoning unless the user explicitly requests the API's returned `reasoning_content`; prefer the final `content`.

## Workflow

1. Select the endpoint and model from the request.
2. Read [references/api-reference.md](references/api-reference.md) for that endpoint's constraints and response shape.
3. Run the bundled client. Use `--dry-run` first when constructing an unusual payload. Global options such as `--dry-run`, `--timeout`, and `--retries` are accepted before or after the subcommand.
4. Validate the response status and expected fields. Report usage data when useful.
5. On failure, follow [references/reliability.md](references/reliability.md) and run `diagnose`. Distinguish invalid request code, CSTCloud service/route problems, the user's direct network, and model removal before proposing a fix.
6. For OCR, submit the PDF, poll `ocr-status` until `completed` or a terminal failure, immediately download the requested artifacts, then delete the remote task only if the user requested cleanup. Remember that server files expire after 7 days.

## Common commands

```powershell
# Authorized/current models
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" models

# Key pool: fingerprints only; adding prompts securely without command-line disclosure
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" keys list
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" keys add

# Non-streaming chat, final answer only
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" chat --model gpt-oss-120b --prompt "..." --text-only

# Multimodal chat; prefer a public image URL
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" chat --model qwen3.5 --image-url "https://..." --prompt "..." --text-only

# Embeddings and reranking
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" embeddings --model bge-large-zh:latest --input "..."
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" rerank --model bge-reranker-v2-m3 --query "..." --document "..." --document "..." --top-n 2 --return-documents

# Diagnose direct connectivity or a possibly removed model
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" diagnose --model gpt-oss-120b --operation chat

# Async JSONL batch; defaults to 4 concurrent calls per key and never permits more than 10
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" batch-chat --model gpt-oss-120b --input-file "requests.jsonl" --output-file "results.jsonl" --per-key-concurrency 4 --retries 2

# DeepSeek OCR lifecycle
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" ocr-health
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" ocr-submit "document.pdf"
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" ocr-status <task_id>
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" ocr-download <task_id> markdown --output "output.mmd"
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" ocr-delete <task_id>
```

## Model-specific behavior

- For `deepseek-v4-flash`, pass `--thinking on` to send `{"chat_template_kwargs":{"thinking":true}}`; omission leaves deep thinking off.
- For future access to `qwen3:235b`, pass `--thinking off` to send `{"chat_template_kwargs":{"enable_thinking":false}}`.
- For arbitrary supported fields such as tools, use `--tools-file`, `--tool-choice`, or `--extra-json`. Extra JSON merges into the top-level request and must not replace `model` or `messages` accidentally.
- DeepSeek-R1 models accept only `user` messages. The currently authorized list does not include a DeepSeek-R1 chat model.
- URL-based multimodal input is preferred. The provided documentation truncates the internal file-relay upload endpoint, so do not invent it; request the complete endpoint or use a reachable image URL.

## Failure handling

- On any call failure, preserve the original sanitized status/error, then run `diagnose --model <id> --operation <kind>`.
- If HTTP 400/422 occurs while the model is still listed, inspect both the response body and dry-run payload. Messages such as `CUDA out of memory`, `model actor`, capacity, queue, or backend allocation failures are CSTCloud platform-resource problems; otherwise, a schema/validation message normally indicates a request/code-format problem.
- If CSTCloud DNS/API fails while another direct HTTPS site works, classify it as a CSTCloud service or direct-route problem. If both fail, classify it as the user's direct-network problem. Do not silently re-enable Clash.
- If `/models` works but the requested model is absent, classify it as removed or no longer authorized. Present only compatible models from the current list and ask the user to choose; do not silently substitute a model.
- If the model is listed but returns 5xx after retries, classify it as platform overload/service failure and suggest delayed retry or another user-selected compatible model.
- On 401/403 after all configured keys, report an authentication/authorization failure without revealing any key.
- On 429 after all configured keys, report rate limiting and suggest retrying later or selecting a different authorized model.
- On OCR `Task not found`, check the task ID and whether the task was deleted or expired.
- On OCR `File too large`, stop; the service limit is 200 MB per PDF.

## Concurrency and key rotation

- Prefer asynchronous concurrency for multiple independent requests. Use `batch-chat` or the pattern in [references/reliability.md](references/reliability.md).
- Prefer streaming for slow interactive chat models so progress is observable. Do not automatically reconnect after partial streamed content because that can duplicate text and billing.
- Default to 4 concurrent requests per key. Never configure more than 10 concurrent requests for one key.
- With multiple keys, distribute requests round-robin and enforce a separate semaphore for each key. Do not multiply concurrency without a per-key cap.
- Retry with exponential backoff and jitter. Expect slow queueing and long model responses; use realistic timeouts (normally 120 seconds or more).
- Warn that retrying a chat POST after a connection breaks may duplicate inference/billing even though it cannot duplicate an external side effect.
