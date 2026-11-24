# Prebuilt chat indexes for the hosted demo

The Dockerfile copies this folder into the image and serves it read-only, so
the hosted site never indexes anything (hosting disks are wiped on restart and
indexing takes minutes).

Fill it from your local indexes, after indexing the repositories in the chat
page (or with `python -m fluxdiff.rag.repos add github owner/repo`):

```bash
python -m fluxdiff.rag.repos list
python -m fluxdiff.rag.repos export deploy/index                       # all ready repos
python -m fluxdiff.rag.repos export deploy/index github-esp-rs-esp-rust-board
```

Then commit this folder. Everything in it becomes part of the deployment and of
your git repository, so only export repositories whose contents may be public.
The server must use the same embedding provider/model the indexes were built
with (for example `GEMINI_API_KEY` for indexes built with Gemini).
