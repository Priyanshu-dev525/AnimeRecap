// Built-in provider presets. Every provider here speaks the OpenAI
// /chat/completions protocol, so one generic adapter covers all of them.
// Model lists are suggestions only: free-tier catalogs change often, so use
// "Fetch models" in the dashboard to pull the current list from the provider.

export const PRESETS = [
  {
    id: 'gemini',
    name: 'Google Gemini',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    env: 'GEMINI_API_KEY',
    signup: 'https://aistudio.google.com/apikey',
    note: 'Generous free tier and a very long context window, which suits episode transcripts.',
    models: ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-pro'],
  },
  {
    id: 'groq',
    name: 'Groq',
    baseUrl: 'https://api.groq.com/openai/v1',
    env: 'GROQ_API_KEY',
    signup: 'https://console.groq.com/keys',
    note: 'Very fast inference with a free tier (rate limited per minute and per day).',
    models: ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'openai/gpt-oss-120b'],
  },
  {
    id: 'openrouter',
    name: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    env: 'OPENROUTER_API_KEY',
    signup: 'https://openrouter.ai/keys',
    note: 'Model IDs ending in ":free" cost nothing (rate limited).',
    headers: { 'HTTP-Referer': 'https://github.com/priyanshu-dev525/animerecap', 'X-Title': 'AnimeRecap Router' },
    models: [
      'deepseek/deepseek-chat-v3-0324:free',
      'meta-llama/llama-3.3-70b-instruct:free',
      'qwen/qwen3-coder:free',
      'google/gemma-3-27b-it:free',
    ],
  },
  {
    id: 'cerebras',
    name: 'Cerebras',
    baseUrl: 'https://api.cerebras.ai/v1',
    env: 'CEREBRAS_API_KEY',
    signup: 'https://cloud.cerebras.ai',
    note: 'Free tier with very high tokens per second.',
    models: ['llama-3.3-70b', 'qwen-3-32b', 'gpt-oss-120b'],
  },
  {
    id: 'mistral',
    name: 'Mistral',
    baseUrl: 'https://api.mistral.ai/v1',
    env: 'MISTRAL_API_KEY',
    signup: 'https://console.mistral.ai/api-keys',
    note: 'Free "Experiment" plan.',
    models: ['mistral-small-latest', 'open-mistral-nemo', 'codestral-latest'],
  },
  {
    id: 'github',
    name: 'GitHub Models',
    baseUrl: 'https://models.github.ai/inference',
    env: 'GITHUB_TOKEN',
    signup: 'https://github.com/settings/tokens',
    note: 'Use a GitHub token with the "models" permission. Free with low daily limits.',
    models: ['openai/gpt-4.1-mini', 'openai/gpt-4o-mini', 'meta/Llama-3.3-70B-Instruct'],
  },
  {
    id: 'nvidia',
    name: 'NVIDIA NIM',
    baseUrl: 'https://integrate.api.nvidia.com/v1',
    env: 'NVIDIA_API_KEY',
    signup: 'https://build.nvidia.com',
    note: 'Free developer credits.',
    models: ['meta/llama-3.3-70b-instruct', 'deepseek-ai/deepseek-r1'],
  },
  {
    id: 'ollama',
    name: 'Ollama (local)',
    baseUrl: 'http://localhost:11434/v1',
    noKey: true,
    signup: 'https://ollama.com',
    note: 'Runs models on your own machine. No key needed.',
    models: [],
  },
];

export const DEFAULT_COMBOS = {
  // Long-context, good-quality writers first; fast fallbacks after.
  'anime-recap': [
    'gemini/gemini-2.5-flash',
    'openrouter/deepseek/deepseek-chat-v3-0324:free',
    'groq/llama-3.3-70b-versatile',
    'cerebras/llama-3.3-70b',
    'mistral/mistral-small-latest',
    'github/openai/gpt-4.1-mini',
  ],
  // Coding agents (Cline / OpenCode).
  'free-coder': [
    'openrouter/qwen/qwen3-coder:free',
    'gemini/gemini-2.5-flash',
    'groq/openai/gpt-oss-120b',
    'mistral/codestral-latest',
    'cerebras/gpt-oss-120b',
  ],
  'free-fast': [
    'groq/llama-3.1-8b-instant',
    'cerebras/llama-3.3-70b',
    'gemini/gemini-2.5-flash-lite',
  ],
};

export function presetFor(id) {
  return PRESETS.find((p) => p.id === id);
}
