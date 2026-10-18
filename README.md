# AnimeRecap Router

A self-hosted AI router in the style of OmniRoute. It pools **free models** from
**OpenCode Zen**, **Cline**, Gemini, Groq, OpenRouter and more behind one
**OpenAI-compatible API**, which the **AnimeRecap Studio** (and any other app) can call
with a single endpoint and key.

```
 Cline / OpenCode / AnimeRecap Studio
              │  OpenAI API  (http://localhost:20128/v1)
              ▼
     ┌──────────────────────┐
     │   AnimeRecap Router  │  combos + fallback, key rotation,
     │   dashboard  :20128  │  rate-limit cooldown, usage logs
     └──────────────────────┘
       │      │      │      │      │
  OpenCode Zen  Cline  Gemini  Groq  OpenRouter  Cerebras  Mistral  GitHub  NVIDIA  Ollama  (+ custom)
```

## Features

- **OpenAI-compatible**: `POST /v1/chat/completions` (streaming and non-streaming) and `GET /v1/models`.
- **OpenCode Zen and Cline as model sources**: use their free models through the router.
  OpenCode Zen's free models work with **no key at all**, and if you are logged in to OpenCode
  (`opencode auth login`) the router picks up that key automatically. Cline uses your Cline account API key.
- **Other free providers built in**: Google Gemini, Groq, OpenRouter (`:free` models), Cerebras, Mistral,
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

1. **Providers** tab: **OpenCode Zen** is ready straight away (free, no key). Click **Test** on it.
   For **Cline**, paste your key from app.cline.bot → Settings → API Keys. Optionally add more free keys
   (Gemini, Groq, …) for extra capacity. Use **Fetch models** to see each provider's current free models.
2. **Connect** tab: copy the base URL and router key.
3. Connect your tools as shown below.

Optional: copy `.env.example` to `.env` to set the port, an admin password, or provider keys through
environment variables. Separate several keys with commas to rotate between them.

## Using OpenCode and Cline models

| Provider     | Key                                                       | Model name in the router            |
| ------------ | --------------------------------------------------------- | ----------------------------------- |
| OpenCode Zen | none needed for free models (or your OpenCode login/Zen key) | `opencode/big-pickle`, `opencode/<model>-free` |
| Cline        | app.cline.bot → Settings → API Keys                       | `cline/<provider>/<model>`          |

Free model lineups change often. Click **Fetch models** on the provider card. Free ones are marked,
then tick them and **Save**.

## Also: point Cline / OpenCode at the router (optional)

The router speaks the OpenAI API, so the Cline and OpenCode apps can also *use* it. That gives them
fallback across every provider above.

### Connect Cline

Settings → **API Provider: OpenAI Compatible**

| Field    | Value                                                   |
| -------- | ------------------------------------------------------- |
| Base URL | `http://localhost:20128/v1`                             |
| API Key  | your router key (`sk-anime-…`)                          |
| Model ID | `free-coder` (or `anime-recap`, `groq/llama-3.3-70b-versatile`, …) |

### Connect OpenCode

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
| `OPENCODE_API_KEY`, `CLINE_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `GITHUB_TOKEN`, `NVIDIA_API_KEY` | (none) | provider keys (comma-separated to rotate) |

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
