# follow-up-engine

## LLM drafts

Drafts call Baseten's OpenAI-compatible API when `BASETEN_API_KEY` is set. Without a key they use a local template.

1. Create a Baseten account and add billing / payment.
2. Create an API key.
3. `export BASETEN_API_KEY=...` (or gitignored `.env`).
4. Model: `deepseek-ai/DeepSeek-V4.1-Flash-1017`. Base URL: `https://inference.baseten.co/v1`.
5. Without a key, drafts use the template.
