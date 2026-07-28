# Reliability, diagnosis, and concurrency

## Contents

- [Credential pool](#credential-pool)
- [Failure classification](#failure-classification)
- [Retry policy](#retry-policy)
- [Asynchronous concurrency](#asynchronous-concurrency)
- [Model removal workflow](#model-removal-workflow)

## Credential pool

The bundled client loads keys in priority order from the protected local store, then from optional environment variables. It never prints secrets. `keys list` returns only SHA-256-derived 12-character fingerprints.

Add a key interactively with `keys add`. For automation, pipe it to `keys add --stdin`; never use a `--key` command-line argument. On Windows, the client encrypts stored keys with current-user DPAPI, so the file cannot be decrypted by another Windows account.

Default behavior is to use the first key. HTTP 401, 403, or 429 advances to the next key. Keep key selection automatic unless the user explicitly requests testing a particular fingerprint.

## Failure classification

| Evidence | Classification | Action |
|---|---|---|
| HTTP 400/422; body reports schema/field validation; model is listed | Request/code-format problem | Compare `--dry-run` JSON with the endpoint schema |
| Any status; body reports CUDA OOM, model actor, capacity, queue, or backend allocation failure | CSTCloud platform-resource problem | Back off; retry later or offer a user-selected compatible model |
| CSTCloud DNS fails | User DNS/network or route | Test another direct HTTPS host; do not enable Clash |
| CSTCloud direct API fails; another direct HTTPS host works | CSTCloud service or direct-route problem | Retry later and report the sanitized error |
| CSTCloud and another direct HTTPS host both fail | User direct-network problem | Ask the user to restore direct connectivity or define an approved bypass route |
| `/models` works; requested ID absent | Model removed or no longer authorized | Present current compatible alternatives and ask the user to choose |
| Model is listed; repeated 5xx | CSTCloud/model overload or service failure | Back off; offer user-selected compatible alternative |
| All keys return 401/403 | Credentials invalid or model/account unauthorized | Ask for a new key or account authorization check |
| All keys return 429 | Account/key rate limited | Reduce concurrency and retry later |

Run:

```powershell
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" diagnose --model <model-id> --operation <chat|embeddings|rerank>
```

The diagnostic connects directly and never consults proxy variables.

## Retry policy

- Retry network failures and HTTP 408, 425, 429, 500, 502, 503, and 504.
- Use exponential backoff with jitter, normally 1 s, 2 s, 4 s, capped near 20 s.
- Rotate keys immediately for 401/403 and when practical for 429.
- Do not retry deterministic 400/404/422 failures unchanged.
- Keep retries bounded. The bundled client defaults to two retries.
- A retried chat POST may consume inference twice if the connection failed after the server accepted it.

## Asynchronous concurrency

Use `batch-chat` for JSONL workloads. Each line may be a JSON string prompt or an object containing `prompt`, `system`, `messages`, `model`, and supported top-level request fields.

The batch runner:

- uses `asyncio` with blocking HTTPS calls delegated to worker threads;
- connects directly without proxies;
- assigns initial keys round-robin;
- enforces an independent semaphore per key;
- defaults to 4 concurrent calls per key;
- rejects per-key concurrency outside `1..10`;
- uses more than one key when available;
- persists each completed result immediately, then sorts the final JSONL by input index;
- records only non-secret key fingerprints.

For application code, reproduce the same shape: one semaphore per API key, an overall semaphore, round-robin assignment, bounded retries, exponential backoff, and `trust_env=False` if using `httpx.AsyncClient` or `aiohttp.ClientSession(trust_env=False)`.

## Model removal workflow

1. Capture the failed model ID and sanitized HTTP error.
2. Call `/models` directly.
3. If the ID is absent, filter the returned IDs by endpoint compatibility.
4. Show the compatible choices to the user and wait for a selection.
5. Do not silently replace the model: output behavior, cost, context length, and tool support can differ.
6. After selection, rebuild the request for the replacement model and retry.
