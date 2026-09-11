# Fantasy Football Manager

Claude-powered advisor for one Sleeper fantasy football team. It analyses the league, posts
proposed roster moves to Discord with Approve / Reject buttons, and executes approved moves
on Sleeper through the same GraphQL API the Sleeper web app uses.

## Layout

- `ffm/config.py` – settings from `.env` (`Config.load()`).
- `ffm/sleeper/public.py` – read-only Sleeper REST client plus the projections/stats feeds.
- `ffm/sleeper/auth.py` – authenticated GraphQL client (add/drop, waivers, lineup, IR, taxi, trades).
- `ffm/players.py` – `PlayerDB` (name lookup, IDP position groups, slot eligibility, depth charts).
- `ffm/news.py` – ESPN public injury report + headlines, matched to Sleeper ids by name/team.
- `ffm/scoring.py` – apply league scoring settings to a stat line.
- `ffm/lineup.py` – optimal-lineup assignment (Hungarian) and the win-probability heuristic.
- `ffm/league.py` – `LeagueContext`: one-pass snapshot of league, roster, free agents, weekly + season projections, injury report, locks; proposal validation; markdown rendering for the model.
- `ffm/proposals.py` – `Proposal` pydantic model (the unit of approval).
- `ffm/store.py` – SQLite store for runs and proposals (`data/ffm.sqlite3`).
- `ffm/agent.py` – `Advisor`: Claude tool-runner loop; tools read the context and `propose_move` records validated proposals.
- `ffm/executor.py` – maps an approved `Proposal` onto Sleeper mutations; dry-run support.
- `ffm/evaluate.py` – weekly self-evaluation from actual scores: lineup efficiency, realized gain of every proposal (rejections included), missed starts, projection accuracy. No model. Proposals carry `week` and a projection `snapshot` (`LeagueContext.stamp`) for this.
- Lessons (stage 2): `Advisor.review_week` turns a finished card into ≤3 lessons + ≤2 engine suggestions (one Sonnet call); lessons are stored in the `lessons` table with Keep/Dismiss buttons in Discord; `Advisor.lessons_block` appends the kept ones (max 10) to the system prompt. Never auto-keep lessons; the manager curates.
- `ffm/discord_bot.py` – discord.py bot: proposal embeds with persistent buttons, slash commands.
- `ffm/scheduler.py` – APScheduler cron jobs: Monday free-agent sweep, Tuesday market review, Wednesday post-waiver sweep, Saturday replacements + lineup. Standard cron day numbering (0 = Sunday) is translated for APScheduler.
- `ffm/__main__.py` – CLI: `run`, `analyze [--trigger weekly_waivers|fa_sweep|post_waivers|late_week|lineup|trades] [--post]`, `ask`, `matchup`, `snapshot`, `claims`, `whoami`, `pending`, `execute`, `reject`, `lock`/`unlock`/`locks`.

Command roles (keep them separate): `/analyze` = roster moves on three horizons, never a lineup; `/sweep` = quick direct adds only; `/lineup` = this week's starters only, never an add; `/matchup` = computed numbers, no model; `/trades` = advice only; `/claims` = waiver claim status. `RUN_PROFILES` in `agent.py` sets effort, web-search budget and iteration caps per run; sweeps are deliberately cheap.

## Commands

```bash
.venv/Scripts/python.exe -m pytest -q          # unit tests (no network)
.venv/Scripts/python.exe -m ffm whoami         # check Sleeper config and token
.venv/Scripts/python.exe -m ffm matchup        # optimal lineup + win probability (read-only)
.venv/Scripts/python.exe -m ffm snapshot       # print the full briefing the model receives
.venv/Scripts/python.exe -m ffm analyze        # one advisor run in the terminal (add --post to send to Discord)
.venv/Scripts/python.exe -m ffm run            # Discord bot + schedule
```

## Conventions

- Python 3.13, async throughout (httpx, discord.py, anthropic AsyncAnthropic).
- Never log or print `SLEEPER_TOKEN`, Discord or Anthropic keys.
- Every roster change goes through `Proposal` -> `Store` -> explicit approval -> `Executor`. Do not add code paths that call `SleeperAuth` mutations without an approved proposal.
- The Sleeper GraphQL mutations are unofficial; keep them isolated in `ffm/sleeper/auth.py`.
- Model calls follow the `claude-api` skill: `claude-opus-5` by default, adaptive thinking, `output_config.effort`, server-side refusal fallbacks.
