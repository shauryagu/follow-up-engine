# follow-up-engine

## LLM drafts

Drafts call Baseten's OpenAI-compatible API when `BASETEN_API_KEY` is set. The client **streams** tokens; the draft row is saved only after the full reply arrives. Without a key they use a local template. If the model call fails, no draft is stored — wait a moment and generate again.

The prompt uses first name, tech, reason, and open quote amounts. It does not send phone numbers or last names. If we already contacted the homeowner, the model is told to acknowledge that; otherwise it writes a first follow-up.

1. Create a Baseten account and add billing / payment.
2. Create an API key.
3. Put `BASETEN_API_KEY` and `BASETEN_BASE_URL=https://inference.baseten.co/v1` in a gitignored `.env` (see `.env.example`), or `export` them.
4. Model: `deepseek-ai/DeepSeek-V4.1-Flash-1017`.
5. Without a key, drafts use the template.
