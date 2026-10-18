# AnimeRecap Router

A self-hosted AI router in the style of OmniRoute. It pools **free model providers**
behind one **OpenAI-compatible API**, so Cline, OpenCode, and later the
**AnimeRecap Studio** can all use a single endpoint and key.

```
 Cline / OpenCode / AnimeRecap Studio
              │  OpenAI API  (http://localhost:20128/v1)
              ▼
     ┌──────────────────────┐
     │   AnimeRecap Router  │  combos + fallback, key rotation,
     │   dashboard  :20128  │  rate-limit cooldown, usage logs
     └──────────────────────┘
       │      │      │      │      │
    Gemini  Groq  OpenRouter Cerebras Mistral  GitHub Models  NVIDIA  Ollama  (+ custom)
```

## Features

- **OpenAI-compatible**: `POST /v1/chat/completions` (streaming and non-streaming) and `GET /v1/models`.
- **Free providers built in**: Google Gemini, Groq, OpenRouter (`:free` models), Cerebras, Mistral,
  GitHub Models, NVIDIA NIM, and local Ollama. You can also add any other OpenAI-compatible endpoint.
- **Combos**: model aliases with automatic fallback. For example, `anime-recap` tries Gemini, then
  OpenRouter, then Groq, and so on. It moves to the next model on rate limits, outages, or a missing key.
- **Key rotation**: add several keys per provider. Keys are used round-robin. A key that returns 429 is
  cooled down (honouring `Retry-After`), and a key that returns 401/403 is parked for 10 minutes.
- **Dashboard**: manage keys, fetch each provider's live model list (with a "free only" filter for OpenRouter),
  test providers, edit combos, try prompts in a playground, and view request logs and token usage.
- **No dependencies**: plain Node.js 18.17 or newer.

## Quick start

```bash
git clone https://github.com/priyanshu-dev525/animerecap.git
cd animerecap
npm start
```

Open http://localhost:20128. Then:

1. **Providers** tab: paste a free API key for at least one provider. Gemini and Groq are the easiest
   (follow the "Get key ↗" links). Click **Test** to check it works.
2. **Connect** tab: copy the base URL and router key.
3. Connect your tools as shown below.

Optional: copy `.env.example` to `.env` to set the port, an admin password, or provider keys through
environment variables. Separate several keys with commas to rotate between them.

## Connect Cline

Settings → **API Provider: OpenAI Compatible**

| Field    | Value                                                   |
| -------- | ------------------------------------------------------- |
| Base URL | `http://localhost:20128/v1`                             |
| API Key  | your router key (`sk-anime-…`)                          |
| Model ID | `free-coder` (or `anime-recap`, `groq/llama-3.3-70b-versatile`, …) |

## Connect OpenCode

`opencode.json` in your project, or `~/.config/opencode/opencode.json`. The dashboard generates this
for you with your real key:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "animerouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AnimeRecap Router",
      "options": { "baseURL": "http://localhost:20128/v1", "apiKey": "sk-anime-..." },
      "models": {
        "free-coder": { "name": "free-coder (combo)" },
        "anime-recap": { "name": "anime-recap (combo)" }
      }
    }
  },
  "model": "animerouter/free-coder"
}
```

## Model names

| You send                               | Router does                                                   |
| -------------------------------------- | ------------------------------------------------------------- |
| `anime-recap` (a combo)                | tries each target in order until one succeeds                 |
| `groq/llama-3.3-70b-versatile`         | that provider and model (text after the first `/` is the model) |
| `openrouter/qwen/qwen3-coder:free`     | same; OpenRouter IDs that contain a `/` work too              |
| `llama-3.3-70b-versatile`              | any provider that lists that model                            |

The response headers `x-router-provider` and `x-router-model` tell you which model actually answered.

Default combos:

- `anime-recap`: long-context models that write well, for episode recaps.
- `free-coder`: coding models for Cline and OpenCode.
- `free-fast`: the lowest-latency models.

> Free-tier model catalogs change often. If a default model ID stops working, use **Fetch models** in
> the Providers tab and update the combo.

## Security

- By default the server listens on `127.0.0.1` only.
- If you set `HOST=0.0.0.0` to expose it on your network, also set `ADMIN_PASSWORD`. Otherwise anyone
  who can reach the port can read and edit your configuration.
- Config, including provider keys, is stored in `data/config.json` (git-ignored, file mode 600).
  Keys from environment variables are never written to disk.

## Configuration

| Env var          | Default     | Purpose                                   |
| ---------------- | ----------- | ----------------------------------------- |
| `PORT`           | `20128`     | listen port                               |
| `HOST`           | `127.0.0.1` | listen address                            |
| `DATA_DIR`       | `./data`    | where `config.json` lives                 |
| `ADMIN_PASSWORD` | (none)      | protects the dashboard and `/api/*`       |
| `ROUTER_API_KEY` | (none)      | extra client key accepted by `/v1/*`      |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `GITHUB_TOKEN`, `NVIDIA_API_KEY` | (none) | provider keys (comma-separated to rotate) |

## Development

```bash
npm run dev   # restart on file changes
npm test      # tests against a mock upstream provider
```

Layout:

- `src/server.js`: HTTP server, admin API, static dashboard
- `src/router.js`: model resolution, fallback, key rotation, streaming
- `src/store.js`: config persistence
- `src/providers.js`: provider presets and default combos
- `public/`: dashboard (vanilla JS)

## Roadmap

- [x] Router: OpenAI-compatible gateway, free providers, combos, key rotation, dashboard
- [ ] AnimeRecap Studio: upload an episode or subtitles → transcript → scene summary → recap script → voice-over and video, all calling this router
