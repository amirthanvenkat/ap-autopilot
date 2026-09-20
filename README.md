# ap-autopilot

TODO: written by the repository owner. CLAUDE.md section 8 reserves the
top-level README, ADRs and SQL commentary for a human author.

Points worth covering, from what spec 01 now implements:

- What the project is, and that it is a portfolio project.
- The deployment split and why the demo has to outlive the GCP trial.
- How to run it: `uv sync`, `REPLAY_FIXTURES=true`, `alembic upgrade head`,
  `uv run python -m uvicorn src.app:app`.
- That fixtures mode is the default demo path and needs no cloud account.
- Where the Postman collection is and what it exercises.
