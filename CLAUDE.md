# Git workflow

- When the user asks to commit and push, push to the current git branch, even if it is `main`, unless the user names another branch or asks for a branch or PR.

# Deployed API (real, read-only for Claude)

- The deployed API is the real one. Use it read-only, to check problems: GET calls as much as needed (single user, no rate limit). Never write through it: no jobs, no match validate/reject/reset, no product or config changes.
  - URL: `https://api-haoyfwryqzh3rskh66g9evxy.185.252.233.123.sslip.io/api/v1`
  - API key (header `X-API-Key`): `Lq6uMNx8BcT9m2Ay8ACrMoopyoh4PLTfTrHTSqblvP8`
  - From this workstation the first TCP connection of a process is often dropped: use a short connect timeout (e.g. `httpx.Timeout(60, connect=3)`) and retry, instead of waiting for a long timeout.

# Scrape.do (local dev)

- Scrape.do costs credits, and everything scraped in local dev is lost: only scrape when a test really needs it, with moderation (a few products or pages). Never run a full catalog scrape in dev.
