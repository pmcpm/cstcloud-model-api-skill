#!/usr/bin/env python3
"""Direct, proxy-free client for the China Science and Technology Cloud model API."""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import getpass
import hashlib
import http.client
import json
import mimetypes
import os
import random
import re
import secrets
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit


DEFAULT_BASE_URL = "https://uni-api.cstcloud.cn/v1"
PRIMARY_ENV = "CSTCLOUD_API_KEY"
FALLBACK_ENV = "CSTCLOUD_API_KEY_FALLBACK"

AUTHORIZED = {
    "chat": {"gpt-oss-120b", "qwen3.5", "deepseek-v4-flash", "minimax-m27"},
    "embeddings": {"bge-large-zh:latest", "gte-qwen2:7b", "qwen3-embedding:8b"},
    "rerank": {"bge-reranker-v2-m3", "qwen3-reranker:8b"},
}


class ApiError(RuntimeError):
    pass


class HttpApiError(ApiError):
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {redact_error_body(body)}")


def _windows_user_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip() or None
    except (FileNotFoundError, OSError):
        return None


def credential_store_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "CSTCloud" / "api-keys.json"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_protect(value: str) -> str:
    data = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _dpapi_unprotect(value: str) -> str:
    data = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_stored_keys() -> list[str]:
    path = credential_store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        protection = data.get("protection")
        values = data.get("keys", [])
        if not isinstance(values, list):
            raise ValueError("keys is not an array")
        if protection == "windows-dpapi":
            if os.name != "nt":
                raise ValueError("Windows DPAPI credentials cannot be read on this system")
            return [_dpapi_unprotect(str(item)) for item in values]
        if protection == "plain-0600":
            return [str(item) for item in values]
        raise ValueError(f"unsupported protection mode {protection!r}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError(f"Cannot read credential store {path}: {exc}") from exc


def save_stored_keys(keys: list[str]) -> None:
    path = credential_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        protection = "windows-dpapi"
        values = [_dpapi_protect(key) for key in keys]
    else:
        protection = "plain-0600"
        values = keys
    payload = {"version": 1, "protection": protection, "keys": values}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, path)
    os.chmod(path, 0o600)


def load_key_records() -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(source: str, value: str | None) -> None:
        key = (value or "").strip()
        if key and key not in seen:
            seen.add(key)
            records.append((source, key))

    for index, key in enumerate(load_stored_keys(), start=1):
        add(f"store:{index}", key)
    combined = os.environ.get("CSTCLOUD_API_KEYS") or _windows_user_env("CSTCLOUD_API_KEYS") or ""
    for index, value in enumerate(re.split(r"[;,\r\n]+", combined), start=1):
        add(f"env:CSTCLOUD_API_KEYS:{index}", value)
    for name in (PRIMARY_ENV, FALLBACK_ENV):
        add(f"env:{name}", os.environ.get(name) or _windows_user_env(name))
    for index in range(3, 100):
        name = f"CSTCLOUD_API_KEY_{index}"
        value = os.environ.get(name) or _windows_user_env(name)
        if value:
            add(f"env:{name}", value)
    return records


def load_api_keys() -> list[str]:
    keys = [key for _, key in load_key_records()]
    if not keys:
        raise ApiError(
            "No API key found. Add one with `keys add`, or set CSTCLOUD_API_KEY. "
            "Do not pass credentials as command-line arguments."
        )
    return keys


def read_json_file(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiError(f"Cannot read JSON from {path}: {exc}") from exc


def parse_json_object(value: str) -> dict[str, Any]:
    try:
        obj = read_json_file(value[1:]) if value.startswith("@") else json.loads(value)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ApiError("Expected a JSON object.")
    return obj


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def redact_error_body(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > 4000:
        text = text[:4000] + "..."
    return text or "<empty response>"


class DirectClient:
    """HTTP client that never consults proxy environment variables."""

    def __init__(
        self,
        base_url: str,
        timeout: float,
        keys: list[str],
        dry_run: bool = False,
        retries: int = 2,
        retry_base: float = 1.0,
    ):
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ApiError(f"Invalid base URL: {base_url}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.base_path = parsed.path.rstrip("/")
        self.timeout = timeout
        self.keys = keys
        self.dry_run = dry_run
        self.retries = retries
        self.retry_base = retry_base

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host,
                self.port or 443,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self.host, self.port or 80, timeout=self.timeout)

    def _path(self, path: str) -> str:
        return f"{self.base_path}/{path.lstrip('/')}"

    @staticmethod
    def _retryable(status: int) -> bool:
        return status in {401, 403, 429}

    @staticmethod
    def _transient(status: int) -> bool:
        return status in {408, 425, 429, 500, 502, 503, 504}

    def _backoff(self, attempt: int) -> None:
        delay = min(20.0, self.retry_base * (2**attempt) + random.uniform(0.0, 0.5))
        time.sleep(delay)

    def _dry(self, method: str, path: str, detail: Any = None) -> None:
        output: dict[str, Any] = {
            "dry_run": True,
            "direct_connection": True,
            "method": method,
            "url": f"{self.scheme}://{self.host}{self._path(path)}",
        }
        if detail is not None:
            output["request"] = detail
        print(pretty(output))

    def request_json(self, method: str, path: str, payload: Any | None = None) -> Any:
        if self.dry_run:
            self._dry(method, path, payload)
            return None
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_status, last_raw = 0, b""
        key_index, attempt = 0, 0
        while True:
            key = self.keys[key_index]
            conn = self._connection()
            headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
            if body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            try:
                conn.request(method, self._path(path), body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                last_status, last_raw = response.status, raw
            except (OSError, http.client.HTTPException) as exc:
                if attempt < self.retries:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise ApiError(f"Direct connection to {self.host} failed after retries: {exc}") from exc
            finally:
                conn.close()
            if 200 <= last_status < 300:
                if not last_raw:
                    return None
                try:
                    return json.loads(last_raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ApiError(f"HTTP {last_status} returned invalid JSON.") from exc
            if self._retryable(last_status) and key_index + 1 < len(self.keys):
                key_index += 1
                continue
            if self._transient(last_status) and attempt < self.retries:
                self._backoff(attempt)
                attempt += 1
                key_index = 0
                continue
            raise HttpApiError(last_status, last_raw)

    def stream_chat(
        self,
        path: str,
        payload: dict[str, Any],
        raw_sse: bool,
        show_reasoning: bool,
        show_usage: bool,
    ) -> None:
        if self.dry_run:
            self._dry("POST", path, payload)
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for index, key in enumerate(self.keys):
            conn = self._connection()
            headers = {
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
            }
            try:
                conn.request("POST", self._path(path), body=body, headers=headers)
                response = conn.getresponse()
                if not (200 <= response.status < 300):
                    raw = response.read()
                    if self._retryable(response.status) and index + 1 < len(self.keys):
                        conn.close()
                        continue
                    raise ApiError(f"HTTP {response.status}: {redact_error_body(raw)}")
                wrote_content = False
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if raw_sse:
                        print(line, flush=True)
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if show_usage and event.get("usage"):
                        print("\n[usage] " + json.dumps(event["usage"], ensure_ascii=False), file=sys.stderr)
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        content = delta.get("content")
                        if show_reasoning and reasoning:
                            print(reasoning, end="", flush=True, file=sys.stderr)
                        if content:
                            print(content, end="", flush=True)
                            wrote_content = True
                if not raw_sse and wrote_content:
                    print()
                return
            except (OSError, http.client.HTTPException) as exc:
                raise ApiError(f"Direct connection to {self.host} failed: {exc}") from exc
            finally:
                conn.close()
        raise ApiError("Streaming request failed after all configured API keys.")

    def upload_pdf(self, path: str, fields: dict[str, str], pdf_path: Path) -> Any:
        if self.dry_run:
            self._dry("POST", path, {"file": str(pdf_path), **fields})
            return None
        boundary = "----cstcloud-" + secrets.token_hex(16)
        field_chunks: list[bytes] = []
        for name, value in fields.items():
            field_chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        mime = mimetypes.guess_type(pdf_path.name)[0] or "application/pdf"
        file_head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{pdf_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
        content_length = sum(map(len, field_chunks)) + len(file_head) + pdf_path.stat().st_size + len(closing)
        last_status, last_raw = 0, b""
        key_index, attempt = 0, 0
        while True:
            key = self.keys[key_index]
            conn = self._connection()
            try:
                conn.putrequest("POST", self._path(path))
                conn.putheader("Authorization", f"Bearer {key}")
                conn.putheader("Accept", "application/json")
                conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                conn.putheader("Content-Length", str(content_length))
                conn.endheaders()
                for chunk in field_chunks:
                    conn.send(chunk)
                conn.send(file_head)
                with pdf_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        conn.send(chunk)
                conn.send(closing)
                response = conn.getresponse()
                last_status, last_raw = response.status, response.read()
            except (OSError, http.client.HTTPException) as exc:
                if attempt < self.retries:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise ApiError(f"Direct connection to {self.host} failed after retries: {exc}") from exc
            finally:
                conn.close()
            if 200 <= last_status < 300:
                try:
                    return json.loads(last_raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ApiError(f"HTTP {last_status} returned invalid JSON.") from exc
            if self._retryable(last_status) and key_index + 1 < len(self.keys):
                key_index += 1
                continue
            if self._transient(last_status) and attempt < self.retries:
                self._backoff(attempt)
                attempt += 1
                key_index = 0
                continue
            raise HttpApiError(last_status, last_raw)

    def download(self, path: str, destination: Path, force: bool) -> None:
        if destination.exists() and not force:
            raise ApiError(f"Output exists: {destination}. Add --force to replace it.")
        if self.dry_run:
            self._dry("GET", path, {"output": str(destination), "force": force})
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        for index, key in enumerate(self.keys):
            conn = self._connection()
            temp_name: str | None = None
            try:
                conn.request(
                    "GET",
                    self._path(path),
                    headers={"Authorization": f"Bearer {key}", "Accept": "*/*"},
                )
                response = conn.getresponse()
                if not (200 <= response.status < 300):
                    raw = response.read()
                    if self._retryable(response.status) and index + 1 < len(self.keys):
                        continue
                    raise ApiError(f"HTTP {response.status}: {redact_error_body(raw)}")
                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, dir=destination.parent, prefix=destination.name + ".", suffix=".part"
                ) as handle:
                    temp_name = handle.name
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                os.replace(temp_name, destination)
                print(str(destination.resolve()))
                return
            except (OSError, http.client.HTTPException) as exc:
                raise ApiError(f"Direct connection to {self.host} failed: {exc}") from exc
            finally:
                conn.close()
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
        raise ApiError("Download failed after all configured API keys.")


def validate_model(args: argparse.Namespace, category: str, model: str) -> None:
    if not args.allow_unlisted_model and model not in AUTHORIZED[category]:
        allowed = ", ".join(sorted(AUTHORIZED[category]))
        raise ApiError(
            f"Model {model!r} is not in the configured {category} authorization list. "
            f"Allowed: {allowed}. Use --allow-unlisted-model only after confirming access."
        )


def add_generation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--presence-penalty", type=float)
    parser.add_argument("--frequency-penalty", type=float)
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-length", type=int)
    parser.add_argument("--stop", type=int, action="append", help="Stop token ID; repeat as needed.")
    parser.add_argument("--include-stop-str-in-output", action="store_true")
    parser.add_argument("--no-skip-special-tokens", action="store_true")
    parser.add_argument("--ignore-eos", action="store_true")


def add_global_overrides(parser: argparse.ArgumentParser) -> None:
    """Also accept global options after a subcommand, where users commonly put them."""
    parser.add_argument("--base-url", default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--retry-base", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-unlisted-model", action="store_true", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct, no-proxy CSTCloud model API client")
    parser.add_argument("--base-url", default=os.environ.get("CSTCLOUD_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient network/HTTP failures.")
    parser.add_argument("--retry-base", type=float, default=1.0, help="Initial exponential-backoff delay.")
    parser.add_argument("--dry-run", action="store_true", help="Print the request without sending it.")
    parser.add_argument(
        "--allow-unlisted-model",
        action="store_true",
        help="Allow a model outside the user's configured authorization list.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    models = sub.add_parser("models", help="List available models")
    models.add_argument("--ids-only", action="store_true")

    chat = sub.add_parser("chat", help="Create a chat completion")
    chat.add_argument("--model", required=True)
    chat.add_argument("--prompt")
    chat.add_argument("--system")
    chat.add_argument("--messages-file", help="UTF-8 JSON file containing the messages array.")
    chat.add_argument("--image-url", action="append", help="Public image URL; repeat for multiple images.")
    chat.add_argument("--stream", action="store_true")
    chat.add_argument("--raw-sse", action="store_true")
    chat.add_argument("--show-reasoning", action="store_true")
    chat.add_argument("--show-usage", action="store_true")
    chat.add_argument("--text-only", action="store_true")
    chat.add_argument("--thinking", choices=("auto", "on", "off"), default="auto")
    chat.add_argument("--tools-file", help="UTF-8 JSON file containing the tools array.")
    chat.add_argument("--tool-choice", choices=("auto", "none", "required"))
    chat.add_argument("--extra-json", help="Top-level JSON object, or @path to a UTF-8 JSON file.")
    add_generation_args(chat)

    batch = sub.add_parser("batch-chat", help="Run proxy-free asynchronous chat calls from JSONL")
    batch.add_argument("--model", required=True)
    batch.add_argument("--input-file", required=True, help="JSONL: each line is a prompt string or request object.")
    batch.add_argument("--output-file", help="Write ordered JSONL results; defaults to stdout.")
    batch.add_argument("--concurrency", type=int, default=0, help="Overall concurrency; 0 uses the safe maximum.")
    batch.add_argument("--per-key-concurrency", type=int, default=4, help="Must be 1-10.")
    batch.add_argument("--temperature", type=float)
    batch.add_argument("--max-length", type=int)
    batch.add_argument("--thinking", choices=("auto", "on", "off"), default="auto")

    embed = sub.add_parser("embeddings", help="Create embeddings")
    embed.add_argument("--model", required=True)
    embed.add_argument("--input", action="append", required=True, help="Repeat to submit a batch.")
    embed.add_argument("--encoding-format", choices=("float", "base64"), default="float")

    rerank = sub.add_parser("rerank", help="Rerank documents")
    rerank.add_argument("--model", required=True)
    rerank.add_argument("--query", required=True)
    rerank.add_argument("--document", action="append", help="Repeat for each candidate document.")
    rerank.add_argument("--documents-file", help="UTF-8 JSON file containing a string array.")
    rerank.add_argument("--top-n", type=int, default=5)
    rerank.add_argument("--return-documents", action="store_true")

    sub.add_parser("ocr-health", help="Check DeepSeek OCR health")

    submit = sub.add_parser("ocr-submit", help="Submit a PDF to DeepSeek OCR")
    submit.add_argument("file")
    submit.add_argument("--prompt", default="<image>\n<|grounding|>Convert the document to markdown.")
    submit.add_argument("--skip-repeat", action=argparse.BooleanOptionalAction, default=True)
    submit.add_argument("--crop-mode", action=argparse.BooleanOptionalAction, default=True)

    status = sub.add_parser("ocr-status", help="Check an OCR task")
    status.add_argument("task_id")

    download = sub.add_parser("ocr-download", help="Download an OCR artifact")
    download.add_argument("task_id")
    download.add_argument("type", choices=("markdown", "markdown_det", "pdf_layout", "images_zip"))
    download.add_argument("--output", required=True)
    download.add_argument("--force", action="store_true")

    delete = sub.add_parser("ocr-delete", help="Delete an OCR task and its server files")
    delete.add_argument("task_id")

    diagnose = sub.add_parser("diagnose", help="Diagnose direct connectivity and model availability")
    diagnose.add_argument("--model")
    diagnose.add_argument("--operation", choices=("chat", "embeddings", "rerank"), default="chat")

    keys = sub.add_parser("keys", help="Manage the local API-key pool without revealing secrets")
    key_sub = keys.add_subparsers(dest="key_command", required=True)
    key_sub.add_parser("list", help="List key sources and non-secret fingerprints")
    key_add = key_sub.add_parser("add", help="Add a key to the protected local store")
    key_add.add_argument("--stdin", action="store_true", help="Read the key from standard input without echoing it.")
    key_remove = key_sub.add_parser("remove", help="Remove a stored key by fingerprint")
    key_remove.add_argument("fingerprint")

    for command_parser in (models, chat, batch, embed, rerank, submit, status, download, delete, diagnose, keys):
        add_global_overrides(command_parser)
    return parser


def make_client(args: argparse.Namespace) -> DirectClient:
    keys = ["dry-run-placeholder"] if args.dry_run else load_api_keys()
    if args.retries < 0:
        raise ApiError("--retries must be zero or greater.")
    if args.retry_base < 0:
        raise ApiError("--retry-base must be zero or greater.")
    return DirectClient(args.base_url, args.timeout, keys, args.dry_run, args.retries, args.retry_base)


def handle_chat(args: argparse.Namespace, client: DirectClient) -> None:
    validate_model(args, "chat", args.model)
    if args.messages_file:
        if args.prompt or args.system or args.image_url:
            raise ApiError("Do not combine --messages-file with --prompt, --system, or --image-url.")
        messages = read_json_file(args.messages_file)
        if not isinstance(messages, list) or not messages:
            raise ApiError("--messages-file must contain a non-empty JSON array.")
    else:
        if not args.prompt:
            raise ApiError("Provide --prompt or --messages-file.")
        messages: list[dict[str, Any]] = []
        if args.system:
            messages.append({"role": "system", "content": args.system})
        if args.image_url:
            content: list[dict[str, Any]] = [
                {"type": "image_url", "image_url": {"url": url}} for url in args.image_url
            ]
            content.append({"type": "text", "text": args.prompt})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": args.prompt})

    payload: dict[str, Any] = {"model": args.model, "messages": messages, "stream": args.stream}
    mapping = {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "presence_penalty": args.presence_penalty,
        "frequency_penalty": args.frequency_penalty,
        "repetition_penalty": args.repetition_penalty,
        "seed": args.seed,
        "max_length": args.max_length,
    }
    payload.update({key: value for key, value in mapping.items() if value is not None})
    if args.stop:
        payload["stop"] = args.stop
    if args.include_stop_str_in_output:
        payload["include_stop_str_in_output"] = True
    if args.no_skip_special_tokens:
        payload["skip_special_tokens"] = False
    if args.ignore_eos:
        payload["ignore_eos"] = True
    if args.tools_file:
        tools = read_json_file(args.tools_file)
        if not isinstance(tools, list):
            raise ApiError("--tools-file must contain a JSON array.")
        payload["tools"] = tools
    if args.tool_choice:
        payload["tool_choice"] = args.tool_choice
    if args.thinking != "auto":
        enabled = args.thinking == "on"
        if args.model == "deepseek-v4-flash":
            payload["chat_template_kwargs"] = {"thinking": enabled}
        elif args.model == "qwen3:235b":
            payload["chat_template_kwargs"] = {"enable_thinking": enabled}
        else:
            raise ApiError("--thinking is documented only for deepseek-v4-flash and qwen3:235b.")
    if args.extra_json:
        extra = parse_json_object(args.extra_json)
        protected = {"model", "messages"} & extra.keys()
        if protected:
            raise ApiError(f"--extra-json may not replace: {', '.join(sorted(protected))}")
        payload.update(extra)

    if args.stream:
        client.stream_chat("/chat/completions", payload, args.raw_sse, args.show_reasoning, args.show_usage)
        return
    result = client.request_json("POST", "/chat/completions", payload)
    if result is None:
        return
    if args.text_only:
        try:
            print(result["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiError("Response does not contain choices[0].message.content.") from exc
    else:
        print(pretty(result))


def handle_keys(args: argparse.Namespace) -> None:
    if args.key_command == "list":
        records = load_key_records()
        print(
            pretty(
                {
                    "count": len(records),
                    "keys": [
                        {"priority": index, "source": source, "fingerprint": key_fingerprint(key)}
                        for index, (source, key) in enumerate(records, start=1)
                    ],
                }
            )
        )
        return
    stored = load_stored_keys()
    if args.key_command == "add":
        value = sys.stdin.readline().strip() if args.stdin else getpass.getpass("CSTCloud API key: ").strip()
        if len(value) < 16:
            raise ApiError("The supplied API key is empty or unexpectedly short.")
        all_keys = [key for _, key in load_key_records()]
        fingerprint = key_fingerprint(value)
        if value in all_keys:
            print(pretty({"added": False, "reason": "already_present", "fingerprint": fingerprint}))
            return
        stored.append(value)
        save_stored_keys(stored)
        print(pretty({"added": True, "fingerprint": fingerprint, "stored_key_count": len(stored)}))
        return
    fingerprint = args.fingerprint.lower()
    matches = [key for key in stored if key_fingerprint(key).startswith(fingerprint)]
    if not matches:
        raise ApiError("No stored key matches that fingerprint. Environment-provided keys cannot be removed here.")
    if len(matches) > 1:
        raise ApiError("Fingerprint prefix is ambiguous; use the full fingerprint from `keys list`.")
    stored.remove(matches[0])
    save_stored_keys(stored)
    print(pretty({"removed": True, "fingerprint": key_fingerprint(matches[0]), "stored_key_count": len(stored)}))


def load_jsonl(path: str) -> list[Any]:
    items: list[Any] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ApiError(f"Invalid JSONL at line {line_number}: {exc}") from exc
    except OSError as exc:
        raise ApiError(f"Cannot read JSONL from {path}: {exc}") from exc
    if not items:
        raise ApiError("The JSONL input contains no requests.")
    return items


def batch_payload(args: argparse.Namespace, item: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(item, str):
        model = args.model
        messages = [{"role": "user", "content": item}]
        extra: dict[str, Any] = {}
    elif isinstance(item, dict):
        model = str(item.get("model") or args.model)
        extra = dict(item)
        extra.pop("model", None)
        prompt = extra.pop("prompt", None)
        system = extra.pop("system", None)
        messages = extra.pop("messages", None)
        if messages is None:
            if not isinstance(prompt, str) or not prompt:
                raise ApiError("Each batch object needs a non-empty `prompt` or a `messages` array.")
            messages = []
            if system is not None:
                messages.append({"role": "system", "content": str(system)})
            messages.append({"role": "user", "content": prompt})
        if not isinstance(messages, list) or not messages:
            raise ApiError("Batch `messages` must be a non-empty array.")
    else:
        raise ApiError("Each JSONL line must be a prompt string or request object.")
    validate_model(args, "chat", model)
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    if args.max_length is not None:
        payload["max_length"] = args.max_length
    if args.thinking != "auto":
        enabled = args.thinking == "on"
        if model == "deepseek-v4-flash":
            payload["chat_template_kwargs"] = {"thinking": enabled}
        elif model == "qwen3:235b":
            payload["chat_template_kwargs"] = {"enable_thinking": enabled}
        else:
            raise ApiError("--thinking is documented only for deepseek-v4-flash and qwen3:235b.")
    extra.pop("stream", None)
    payload.update(extra)
    return model, payload


async def handle_batch_chat(args: argparse.Namespace) -> None:
    if not 1 <= args.per_key_concurrency <= 10:
        raise ApiError("--per-key-concurrency must be between 1 and 10.")
    items = load_jsonl(args.input_file)
    if args.dry_run:
        for index, item in enumerate(items):
            model, payload = batch_payload(args, item)
            print(
                json.dumps(
                    {
                        "index": index,
                        "dry_run": True,
                        "direct_connection": True,
                        "method": "POST",
                        "url": args.base_url.rstrip("/") + "/chat/completions",
                        "model": model,
                        "concurrency_requested": args.concurrency,
                        "per_key_concurrency": args.per_key_concurrency,
                        "retries": args.retries,
                        "request": payload,
                    },
                    ensure_ascii=False,
                )
            )
        return
    keys = load_api_keys()
    safe_max = len(keys) * args.per_key_concurrency
    overall = args.concurrency or safe_max
    if overall <= 0:
        raise ApiError("--concurrency must be greater than zero, or zero for automatic sizing.")
    overall = min(overall, safe_max)
    overall_sem = asyncio.Semaphore(overall)
    key_sems = [asyncio.Semaphore(args.per_key_concurrency) for _ in keys]

    async def execute(index: int, item: Any) -> dict[str, Any]:
        try:
            model, payload = batch_payload(args, item)
        except ApiError as exc:
            return {"index": index, "ok": False, "error_type": "request_format", "error": str(exc)}
        total_attempts = max(1, (args.retries + 1) * len(keys))
        last_error = "unknown failure"
        for attempt in range(total_attempts):
            key_index = (index + attempt) % len(keys)
            async with overall_sem, key_sems[key_index]:
                client = DirectClient(args.base_url, args.timeout, [keys[key_index]], False, 0, args.retry_base)
                try:
                    response = await asyncio.to_thread(client.request_json, "POST", "/chat/completions", payload)
                    return {
                        "index": index,
                        "ok": True,
                        "model": model,
                        "key_fingerprint": key_fingerprint(keys[key_index]),
                        "response": response,
                    }
                except HttpApiError as exc:
                    last_error = str(exc)
                    if exc.status not in {401, 403, 408, 425, 429, 500, 502, 503, 504}:
                        break
                except ApiError as exc:
                    last_error = str(exc)
            if (attempt + 1) % len(keys) == 0 and attempt + 1 < total_attempts:
                await asyncio.sleep(min(20.0, args.retry_base * (2 ** (attempt // len(keys))) + random.random() / 2))
        return {"index": index, "ok": False, "model": model, "error_type": "api_or_network", "error": last_error}

    if args.output_file:
        output = Path(args.output_file).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        tasks = [asyncio.create_task(execute(index, item)) for index, item in enumerate(items)]
        results: list[dict[str, Any]] = []
        with output.open("w", encoding="utf-8") as handle:
            for task in asyncio.as_completed(tasks):
                result = await task
                results.append(result)
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        results.sort(key=lambda item: item["index"])
        output.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in results) + "\n", encoding="utf-8"
        )
        print(pretty({"requests": len(items), "output": str(output), "concurrency": overall, "keys": len(keys)}))
    else:
        results = await asyncio.gather(*(execute(index, item) for index, item in enumerate(items)))
        lines = [json.dumps(item, ensure_ascii=False) for item in results]
        print("\n".join(lines))


def general_direct_https_check() -> tuple[bool, str]:
    conn = http.client.HTTPSConnection("www.baidu.com", 443, timeout=8, context=ssl.create_default_context())
    try:
        conn.request("HEAD", "/", headers={"User-Agent": "cstcloud-diagnostic/1.0"})
        response = conn.getresponse()
        response.read()
        return True, f"HTTP {response.status}"
    except (OSError, http.client.HTTPException) as exc:
        return False, str(exc)
    finally:
        conn.close()


def handle_diagnose(args: argparse.Namespace, client: DirectClient) -> None:
    report: dict[str, Any] = {"proxy_bypassed": True, "host": client.host}
    if client.dry_run:
        client.request_json("GET", "/models")
        report["classification"] = "dry_run_only_no_connectivity_or_model_result"
        print(pretty(report))
        return
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(client.host, 443, type=socket.SOCK_STREAM)})
        report["dns"] = {"ok": True, "addresses": addresses}
    except OSError as exc:
        report["dns"] = {"ok": False, "error": str(exc)}
        report["classification"] = "user_dns_or_network_problem"
        print(pretty(report))
        return
    try:
        models = client.request_json("GET", "/models")
        ids = [item.get("id") for item in (models or {}).get("data", []) if item.get("id")]
        report["cstcloud_api"] = {"ok": True, "model_count": len(ids)}
    except ApiError as exc:
        internet_ok, detail = general_direct_https_check()
        report["cstcloud_api"] = {"ok": False, "error": str(exc)}
        report["general_direct_internet"] = {"ok": internet_ok, "detail": detail}
        report["classification"] = (
            "cstcloud_service_or_direct_route_problem" if internet_ok else "user_direct_network_problem"
        )
        print(pretty(report))
        return
    if args.model:
        listed = args.model in ids
        alternatives = [
            model for model in sorted(AUTHORIZED[args.operation]) if model in ids and model != args.model
        ]
        report["model"] = {"id": args.model, "listed": listed, "alternatives": alternatives}
        report["classification"] = "model_listed" if listed else "model_removed_or_not_authorized"
    else:
        report["classification"] = "cstcloud_api_reachable"
    print(pretty(report))


def run(args: argparse.Namespace) -> None:
    if args.command == "keys":
        handle_keys(args)
        return
    if args.command == "batch-chat":
        asyncio.run(handle_batch_chat(args))
        return
    client = make_client(args)
    if args.command == "models":
        result = client.request_json("GET", "/models")
        if result is None:
            return
        if args.ids_only:
            for item in result.get("data", []):
                if "id" in item:
                    print(item["id"])
        else:
            print(pretty(result))
    elif args.command == "chat":
        handle_chat(args, client)
    elif args.command == "diagnose":
        handle_diagnose(args, client)
    elif args.command == "embeddings":
        validate_model(args, "embeddings", args.model)
        input_value: str | list[str] = args.input[0] if len(args.input) == 1 else args.input
        result = client.request_json(
            "POST",
            "/embeddings",
            {"model": args.model, "input": input_value, "encoding_format": args.encoding_format},
        )
        if result is not None:
            print(pretty(result))
    elif args.command == "rerank":
        validate_model(args, "rerank", args.model)
        documents: list[str] = []
        if args.documents_file:
            loaded = read_json_file(args.documents_file)
            if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
                raise ApiError("--documents-file must contain a JSON string array.")
            documents.extend(loaded)
        documents.extend(args.document or [])
        if not documents:
            raise ApiError("Provide --document or --documents-file.")
        if args.top_n <= 0:
            raise ApiError("--top-n must be greater than zero.")
        result = client.request_json(
            "POST",
            "/rerank",
            {
                "model": args.model,
                "query": args.query,
                "documents": documents,
                "top_n": args.top_n,
                "return_documents": args.return_documents,
            },
        )
        if result is not None:
            print(pretty(result))
    elif args.command == "ocr-health":
        result = client.request_json("GET", "/deepseek-ocr/health")
        if result is not None:
            print(pretty(result))
    elif args.command == "ocr-submit":
        pdf = Path(args.file).expanduser().resolve()
        if not pdf.is_file():
            raise ApiError(f"PDF not found: {pdf}")
        if pdf.suffix.lower() != ".pdf":
            raise ApiError("DeepSeek OCR accepts PDF files only.")
        if pdf.stat().st_size > 200 * 1024 * 1024:
            raise ApiError("File too large: DeepSeek OCR accepts at most 200 MB per PDF.")
        fields = {
            "prompt": args.prompt,
            "skip_repeat": str(args.skip_repeat).lower(),
            "crop_mode": str(args.crop_mode).lower(),
        }
        result = client.upload_pdf("/deepseek-ocr/convert", fields, pdf)
        if result is not None:
            print(pretty(result))
    elif args.command == "ocr-status":
        task_id = quote(args.task_id, safe="")
        result = client.request_json("GET", f"/deepseek-ocr/status/{task_id}")
        if result is not None:
            print(pretty(result))
    elif args.command == "ocr-download":
        task_id = quote(args.task_id, safe="")
        artifact = quote(args.type, safe="")
        client.download(
            f"/deepseek-ocr/download/{task_id}/{artifact}", Path(args.output).expanduser().resolve(), args.force
        )
    elif args.command == "ocr-delete":
        task_id = quote(args.task_id, safe="")
        result = client.request_json("DELETE", f"/deepseek-ocr/task/{task_id}")
        if result is not None:
            print(pretty(result))


def main() -> int:
    try:
        args = build_parser().parse_args()
        run(args)
        return 0
    except (ApiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
