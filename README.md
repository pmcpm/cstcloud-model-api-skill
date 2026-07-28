# CSTCloud Model API Skill

面向 Codex 的中国科技云大模型 API Skill，提供模型查询、对话与多模态推理、文本向量、文档重排和 DeepSeek OCR 调用能力。

本 Skill 使用 `https://uni-api.cstcloud.cn/v1`，并强制采用直连方式，忽略 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY`，适用于本机运行 Clash 等代理软件、但科技云接口必须绕过代理的环境。

> 这是非官方社区集成。模型、参数和服务状态可能随中国科技云平台调整，请以 `/v1/models` 的实时结果和平台最新文档为准。

## 功能

- 查询账户当前可用模型
- OpenAI 风格的 Chat Completions
- `qwen3.5` URL 图片多模态输入
- Embeddings 与 Rerank
- DeepSeek OCR：健康检查、PDF 提交、状态查询、结果下载和任务删除
- 多 API Key 自动轮换
- Windows DPAPI 加密凭据池
- 指数退避、限流切换和有限重试
- 异步 JSONL 批处理，每枚 Key 独立限制并发
- 模型下架、请求格式、平台服务和用户直连网络诊断

## 目录结构

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── api-reference.md
│   └── reliability.md
└── scripts/
    └── cstcloud_api.py
```

## 安装

要求 Python 3.9 或更高版本，仅使用 Python 标准库。

### Windows PowerShell

```powershell
git clone https://github.com/pmcpm/cstcloud-model-api-skill.git "$env:USERPROFILE\.codex\skills\cstcloud-model-api"
```

### macOS / Linux

```bash
git clone https://github.com/pmcpm/cstcloud-model-api-skill.git ~/.codex/skills/cstcloud-model-api
```

安装后，可在 Codex 中直接引用：

```text
使用 $cstcloud-model-api 调用 qwen3.5 完成这个任务。
```

## 配置 API Key

不要把 API Key 写入仓库、脚本、命令行参数或提示词模板。

交互式添加：

```powershell
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" keys add
```

查看 Key 池，只显示不可逆指纹：

```powershell
python "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py" keys list
```

添加更多 Key 时重复执行 `keys add`。自动化环境可通过标准输入使用 `keys add --stdin`，不要设计 `--key` 形式的命令行参数。

在 Windows 上，Key 使用当前用户的 DPAPI 加密并保存在 `%APPDATA%\CSTCloud\api-keys.json`。其他系统使用权限为 `0600` 的本地配置文件。也兼容 `CSTCLOUD_API_KEY`、`CSTCLOUD_API_KEY_FALLBACK` 和 `CSTCLOUD_API_KEYS` 环境变量。

## 快速使用

以下示例先定义脚本路径：

```powershell
$client = "$env:USERPROFILE\.codex\skills\cstcloud-model-api\scripts\cstcloud_api.py"
```

### 查询模型

```powershell
python $client models --ids-only
```

### 对话

```powershell
python $client chat --model qwen3.5 --prompt "用一句话介绍中国科学院。" --text-only
```

流式调用更适合响应较慢的模型：

```powershell
python $client chat --model qwen3.5 --prompt "请简要解释量子计算。" --stream
```

### 多模态

优先使用可以公开访问的图片 URL：

```powershell
python $client chat --model qwen3.5 `
  --image-url "https://example.org/image.jpg" `
  --prompt "请描述这张图片。" `
  --text-only
```

### Embeddings

```powershell
python $client embeddings --model bge-large-zh:latest --input "中国科学院"
```

### Rerank

```powershell
python $client rerank `
  --model qwen3-reranker:8b `
  --query "人工智能在医疗中的应用" `
  --document "机器学习辅助医学影像诊断" `
  --document "古典音乐的发展历史" `
  --top-n 1 `
  --return-documents
```

### DeepSeek OCR

```powershell
python $client ocr-health
python $client ocr-submit "document.pdf"
python $client ocr-status <task_id>
python $client ocr-download <task_id> markdown --output "output.mmd"
python $client ocr-delete <task_id>
```

OCR 仅接受 PDF，单文件最大 200 MB。服务端文件只保存 7 天，请及时下载。

## 异步批处理

输入为 JSONL，每行可以是字符串提示词，也可以是包含 `prompt`、`system`、`messages`、`model` 等字段的 JSON 对象：

```jsonl
"请只回答 A"
{"model":"qwen3.5","prompt":"请只回答 B","temperature":0}
```

运行：

```powershell
python $client batch-chat `
  --model qwen3.5 `
  --input-file "requests.jsonl" `
  --output-file "results.jsonl" `
  --per-key-concurrency 4 `
  --retries 2
```

默认每枚 Key 并发 4，程序强制限制在 `1..10`。有多个 Key 时，请求会分散到不同 Key，并分别使用信号量控制并发。

## 重试与故障诊断

默认对网络错误及 HTTP `408`、`425`、`429`、`500`、`502`、`503`、`504` 进行有限重试，并使用指数退避和随机抖动。HTTP `401`、`403` 或 `429` 会尝试下一枚 Key。

诊断模型与直连状态：

```powershell
python $client diagnose --model qwen3.5 --operation chat
```

诊断原则：

- 模型仍在 `/models`，响应正文为字段校验错误：检查请求格式。
- 返回 CUDA OOM、model actor、容量或队列错误：平台资源问题。
- `/models` 可用但目标模型不存在：模型可能已下架或账户不再有权限，应让用户从兼容模型中重新选择。
- 中国科技云直连失败、其他直连 HTTPS 正常：平台或直连路由问题。
- 所有直连 HTTPS 均失败：用户本地网络问题。

不要在模型下架后静默替换模型，因为不同模型的输出行为、上下文长度和工具能力可能不同。

## 代理绕过

内置客户端直接使用 `http.client` 建立连接，不读取系统代理环境变量，因此不会经过 Clash 的常用端口（如 `7897`）。

如果自行使用 curl，请显式添加：

```bash
curl --noproxy "*" https://uni-api.cstcloud.cn/v1/models \
  -H "Authorization: Bearer ${CSTCLOUD_API_KEY}"
```

## 安全建议

- 不要提交 `.env`、`api-keys.json`、凭据文件或测试输出。
- 不要在日志、Issue、聊天记录或截图中展示 API Key。
- 不要把 API Key 作为命令行参数，因为它可能进入 Shell 历史或进程列表。
- 如果 Key 曾被公开，立即在平台侧撤销并生成新 Key；仅从 Git 历史删除并不安全。
- 在公开仓库中提交前，建议使用 secret scanner 再检查一次。

## 详细文档

- [`SKILL.md`](SKILL.md)：Codex 执行规则与常用工作流
- [`references/api-reference.md`](references/api-reference.md)：接口与参数参考
- [`references/reliability.md`](references/reliability.md)：Key 池、重试、并发和故障分类
