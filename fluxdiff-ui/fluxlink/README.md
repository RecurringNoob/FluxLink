# FluxLink frontend

React + Vite app with two routes:

- `/` — board diff viewer (reads `/api/*` from the Flask server on :5000)
- `/chat` — FluxLink Chat (talks to the FastAPI RAG server, default `http://localhost:8000`;
  override with `VITE_API_URL`)

```bash
npm install
npm run dev     # http://localhost:5173, proxies /api and /images to :5000
npm run build   # writes dist/, which the Flask viewer serves on :5000
```

The chat page uses Tailwind v4 (utilities only, with a reset scoped to the chat
page in `src/components/chat/chat.css`) so it does not restyle the board viewer.
