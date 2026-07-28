# CSTCloud API reference

## Contents

- [Connection and authentication](#connection-and-authentication)
- [Models](#models)
- [Chat completions](#chat-completions)
- [Multimodal chat](#multimodal-chat)
- [Embeddings](#embeddings)
- [Rerank](#rerank)
- [DeepSeek OCR](#deepseek-ocr)

## Connection and authentication

- Base URL: `https://uni-api.cstcloud.cn/v1`
- Authentication header: `Authorization: Bearer <token>`
- JSON requests: `Content-Type: application/json`
- Always bypass system and Clash proxies. The bundled client connects directly. With curl, add `--noproxy "*"`.

## Models

`GET /models` returns an OpenAI-style object with `data[].id`, `object`, `created`, and `owned_by`.

## Chat completions

`POST /chat/completions`

Required fields:

- `model`: a model ID from `/models`.
- `messages`: list of `{role, content}` objects. Roles are `system`, `user`, and `assistant`; DeepSeek-R1 accepts only `user`.

The total message text must be greater than 0 and below 4 MB, and must also fit the model-specific input-token limit.

Optional fields:

| Field | Type/range | Default/notes |
|---|---|---|
| `stream` | boolean | `false`; streaming uses SSE |
| `presence_penalty` | number `[-2, 2]` | `0.0` |
| `frequency_penalty` | number `[-2, 2]` | `0.0` |
| `repetition_penalty` | number `(0, 2]` | `1.0` |
| `temperature` | number `[0, 2]` | `1.0`; DeepSeek-R1 recommends `0.6` |
| `top_p` | number `(0, 1]` | `1.0` |
| `top_k` | int32 `[0, 2147483647]` | backend model default |
| `seed` | uint64 | random when omitted |
| `stop` | list of int32 token IDs | `null` |
| `include_stop_str_in_output` | boolean | `false`; ignored without stop IDs |
| `skip_special_tokens` | boolean | `true` |
| `ignore_eos` | boolean | `false` |
| `max_length` | int32 | bounded by backend `maxIterTimes` |
| `tools` | array, max 128 functions | only effective on supported models |
| `tool_choice` | `auto`, `none`, `required` | `auto`; only effective on supported models |

Documented tool-capable models are `deepseek-v3:671b`, `qwq:32b`, and `qwen3:235b`; they are not in the current authorized list.

Model-specific template options:

- `qwen3:235b`: `{"chat_template_kwargs":{"enable_thinking":false}}` disables deep thinking. `true` or omission enables it.
- `deepseek-v4-flash`: `{"chat_template_kwargs":{"thinking":true}}` enables deep thinking. Omission defaults to off.

Non-stream response: read `choices[0].message.content`; `reasoning_content` may also be present. Token counts are under `usage`.

Streaming response: parse SSE `data:` records until `data: [DONE]`. Append any `choices[].delta.content`; `choices[].delta.reasoning_content` may arrive separately. A final event may contain an empty `choices` array and `usage`.

`spark-70b-x1` uses nonstandard monotonically changing `choices[].index` values and may end with a final usage-bearing chunk. Parse deltas by arrival order rather than assuming index `0`.

## Multimodal chat

Use `POST /chat/completions` with model `qwen3.5`. Set a user message's `content` to an array:

```json
[
  {"type":"image_url","image_url":{"url":"https://example.org/image.jpg"}},
  {"type":"text","text":"Describe this image."}
]
```

Prefer a URL over base64. The source documentation mentions an internal file-relay service but truncates its endpoint and request example; do not infer the missing path.

## Embeddings

`POST /embeddings`

Supported models: `bge-large-zh:latest`, `gte-qwen2:7b`, `qwen3-embedding:8b`.

- `model`: required.
- `input`: required non-empty string, token array, string array, or token-array array; maximum 8192 input tokens subject to model limits.
- `encoding_format`: optional `float` or `base64`; default `float`.

`bge-large-zh:latest` produces 1024-dimensional embeddings. Response vectors are in `data[].embedding`; usage is in `usage`.

## Rerank

`POST /rerank`

Supported models: `bge-reranker-v2-m3`, `qwen3-reranker:8b`.

- `model`: required.
- `query`: required string.
- `documents`: required list of candidate strings.
- `top_n`: optional positive integer, default `5`.
- `return_documents`: optional boolean, default `false`.

Results are in `results[]` with the source `index`, `relevance_score`, and optionally `document.text`.

## DeepSeek OCR

All endpoints require bearer authentication.

### Health

`GET /deepseek-ocr/health` returns `status` and `processor_loaded`.

### Submit PDF

`POST /deepseek-ocr/convert` as multipart form data:

- `file`: required PDF, maximum 200 MB.
- `prompt`: optional; default is `<image>\n<|grounding|>Convert the document to markdown.`
- `skip_repeat`: optional boolean, default `true`.
- `crop_mode`: optional boolean, default `true`.

The response contains a `task_id` and initial `status`.

### Status

`GET /deepseek-ocr/status/{task_id}`. A completed response includes `output_file`, `total_pages`, and `processing_time`.

### Download

`GET /deepseek-ocr/download/{task_id}/{type}` where `type` is one of:

- `markdown`
- `markdown_det`
- `pdf_layout`
- `images_zip`

### Delete

`DELETE /deepseek-ocr/task/{task_id}` deletes all files associated with the task.

Uploaded PDFs and generated artifacts are retained for only 7 days. Download promptly. Common errors include `File too large` and `Task not found`.
