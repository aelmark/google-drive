# google-drive — Agent Guidelines

## Where design decisions live (Obsidian Mind)

Design rationale and durable lessons are recorded outside this repo, reachable
through the `om` MCP server.

- **`search`** reaches the written record: why a choice was made, what was
  rejected, what a constraint was set against. **Start here.**
- **`expand`** shows a known note's links and backlinks, which is cheaper than
  searching again for its neighbourhood.
- **`recall`** returns short durable lessons scoped to this project. It is empty
  until sessions put things in it, so early on it returns nothing, and that is
  *not* evidence the record is missing.
- **`remember`** records durable lessons into the vault's memory so they persist.
- **`record_work`** logs what happened in this repo, filed where it belongs.
- **`health`** when something that should be there cannot be found. Every failure
  in this layer looks identical from outside (no results), and this tells them
  apart.

