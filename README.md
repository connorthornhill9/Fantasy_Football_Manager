# Fantasy Football Manager

An AI assistant GM for your Sleeper fantasy football team. On a schedule (or on demand) it
studies your league, works out which adds, drops, waiver claims, lineup changes, IR moves
and trades are worth making, and posts each one to Discord with **Approve** / **Reject**
buttons. When you approve, it makes the move on Sleeper for you.

Nothing touches your roster without a click from you.

## How it works

1. **Snapshot.** The app pulls your league's rules and scoring, every roster, this week's
   matchup, weekly and season-long projections (scored with *your* league's scoring settings,
   IDP included), the last three weeks of results, Sleeper injury statuses, ESPN's injury
   report and headlines, trending adds/drops, recent league transactions and the free-agent
   pool. Free agents are ranked by a blend of this week's and season projections and show the
   projected gain if they replaced your weakest eligible starter.
2. **Analysis.** Claude (default `claude-opus-5`) reads that snapshot and uses tools to dig
   deeper: player details and news, a team's depth chart, other rosters, more free agents, and
   a web search limited to a short list of sports sites for late-breaking news. Each move it
   wants to make goes through `propose_move`, which validates it against the real roster (is
   the player a free agent, is the drop on your team, is the lineup legal, can you afford the
   FAAB bid, is the player locked).
3. **Approval.** Each valid proposal becomes a Discord message with buttons. Only your Discord
   user can approve. Buttons keep working after the bot restarts.
4. **Execution.** On approval the move is re-validated against fresh league data and then sent
   to Sleeper. Results (or Sleeper's error) are written back into the same Discord message.
   Without a Sleeper token, or with `FFM_DRY_RUN=true`, approvals are logged instead of sent.

Sleeper's public API is read-only, so moves use the same GraphQL calls the Sleeper website
makes, authenticated as the account owner. That API is unofficial and could change; if it
does, the bot reports the error rather than guessing.

## Setup

Requires Python 3.11+ (developed on 3.13) and Windows, macOS or Linux.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
copy .env.example .env
```

Then fill in `.env`. Each value is explained below.

### 1. Sleeper (read side)

- `SLEEPER_USERNAME`: your Sleeper username.
- `SLEEPER_LEAGUE_ID`: from the league URL on sleeper.com. Leave blank if you are in only one
  league this season. `python -m ffm whoami` lists your leagues and their ids.

Check it:

```bash
.venv\Scripts\python -m ffm whoami
.venv\Scripts\python -m ffm matchup
```

### 2. Claude

- `ANTHROPIC_API_KEY`: from <https://console.anthropic.com>.
- `FFM_MODEL` / `FFM_EFFORT`: defaults are `claude-opus-5` and `high`. A full review is
  roughly 40-80k input tokens and a few thousand output tokens.

Try a dry analysis in the terminal (no Discord, no Sleeper token needed):

```bash
.venv\Scripts\python -m ffm analyze
```

Proposals it records are stored as *pending*. You can execute or reject them from the
terminal too (`python -m ffm pending`, `python -m ffm execute <id>`), which is handy for testing
before wiring up Discord.

### 3. Sleeper session (write side)

Executing moves requires the account's own Sleeper session token in `SLEEPER_TOKEN`. It gives
full control of the Sleeper account, so it is treated like a password: it lives only in `.env`
(git-ignored) and is never logged. `python -m ffm whoami` shows which user it belongs to and
when it expires; `/status` in Discord shows the expiry too.

Without it, or with `FFM_DRY_RUN=true`, approvals are logged instead of sent, which is the
safe way to evaluate the recommendations first.

### 4. Discord bot

1. Go to <https://discord.com/developers/applications>, **New Application**, name it.
2. **Bot** tab: **Reset Token**, copy it into `.env` as `DISCORD_BOT_TOKEN`. No privileged
   intents are needed.
3. **OAuth2** tab, URL Generator: scopes `bot` and `applications.commands`; bot permissions
   *Send Messages*, *Embed Links*, *Read Message History*. Open the generated URL and add the
   bot to your server.
4. In Discord, enable **Settings > Advanced > Developer Mode**. Right-click the channel the bot
   should post in and **Copy Channel ID** into `DISCORD_CHANNEL_ID`. Right-click your own name
   and **Copy User ID** into `DISCORD_OWNER_ID`. Optionally right-click the server and copy its
   id into `DISCORD_GUILD_ID` so slash commands appear immediately instead of within an hour.

Start the bot:

```bash
.venv\Scripts\python -m ffm run
```

Leave it running (a terminal window, or a scheduled task / service that starts it at login).
It needs to be up for the schedule to fire and for buttons to respond.

## Using it

Slash commands in Discord:

| Command | What it does |
|---|---|
| `/analyze [focus]` | Market review: adds, drops, claims, IR and stashes, each tagged this week / next 3-4 weeks / season. Never proposes a lineup. Optional focus such as `find me a TE`. |
| `/sweep` | Quick free-agent sweep: direct adds worth making today, no claims, no lineup. Cheap. |
| `/lineup` | This week's lineup only: the computed optimum adjusted for injuries, practice notes, suspensions, kickoff times, weather and trends. Never proposes adds. |
| `/matchup` | Optimal lineup by projection plus a win-probability estimate. Instant, no AI call. |
| `/trades` | Realistic trade ideas based on every team's positional strengths. Advice only. |
| `/ask question` | Ask anything about your team or league; it can propose a move if that is the answer. |
| `/claims` | Your waiver claims: pending ones and how recent ones resolved. |
| `/introduce [persona]` | The bot introduces itself to the league, posted publicly. Invents its own character, or imitates one you name. |
| `/roast [team] [persona]` | Smack talk built from the other teams' actual rosters: holes, injured starters, records. Whole league or one team. |
| `/recap [persona]` | Grades every team's waiver, free-agent and trade moves from the last two weeks, with a move of the week. Posted publicly. |
| `/pending` | Proposals awaiting a decision. |
| `/approve id`, `/reject id` | Same as the buttons. |
| `/lock player`, `/unlock player`, `/locks` | Protect players (see below). |
| `/status` | Model, token expiry, next scheduled runs. |

Scheduled runs (configurable in `.env`, cron syntax, 0 = Sunday; set to `off` to disable). The
defaults are built around a league whose waiver claims process Wednesday at noon and where
free agents can be picked up on the other days:

| Setting | Default | Run |
|---|---|---|
| `FFM_CRON_FA_SWEEP` | Monday 08:00 | Free-agent sweep after the week's games: direct adds of breakouts, no priority spent. |
| `FFM_CRON_MARKET` | Tuesday 09:00 | Market review: claims for contested players, drops, IR, season stashes. |
| `FFM_CRON_POST_WAIVERS` | Thursday 12:15 | After the week's claims have processed: reports how yours resolved, picks up worthwhile leftovers as direct adds, and adds an "around the league" paragraph on what everyone else did. |
| `FFM_CRON_LATE_WEEK` | Saturday 18:00 | After the final injury reports: replaces a ruled-out starter from the pool if needed, then sets the lineup. |

The sweeps run with lower reasoning effort and fewer web searches than the Tuesday review to
keep model costs down; a typical week is four runs for well under a dollar. Trade ideas and
lineup checks are on demand (`/trades`, `/lineup`). `FFM_LEAGUE_NOTES` is free text about your
league's pickup rules (which days are locked, when claims process) that goes into the
advisor's briefing verbatim.

Pending proposals expire after 7 days.

### Optimal lineup and win probability

The app computes the lineup that maximises projected points under your league's slot rules
(players marked Out, Doubtful, IR or suspended are excluded) and compares it with your current
starters. It also estimates your chance of winning this week by treating both lineups' totals
as normal distributions built from per-player projections and typical week-to-week variance by
position. It is a heuristic, not a forecast: about 50% means a coin flip, 70% means a clear
favourite. The model sees both numbers in its briefing, and `/matchup` shows them without
spending anything on the model.

### Trade ideas

`/trades` builds a table of every team's positional strength
relative to the league and asks the advisor for one to three realistic offers. They are posted
as advice with no buttons; if you like one, send it yourself in Sleeper. The advisor never
offers locked players.

### Locked players

`/lock Travis Kelce` marks a player as untouchable: the advisor can never propose dropping
or trading him, and the validator rejects any such proposal even if the model tries. Locked
players can still be started or benched. The model is told who is locked and may mention in
its summary what it would do if you unlocked someone, so you still get the recommendation
without the risk. `/unlock` removes the lock; `/locks` lists them. The same commands exist in
the terminal (`python -m ffm lock <name>`).

### Data sources

- Sleeper: rosters, league rules, weekly and season projections (Rotowire), trending adds
  and drops, transactions, injury status and depth-chart order.
- ESPN's public site API: the injury report with a written comment per player, headlines,
  and the scoreboard (kickoff times, game status, venue and weather). Game status also drives
  a lock check: a lineup change involving a player whose game has started is rejected.
- ESPN's fantasy projections, scored with your league's settings and shown next to Sleeper's
  as a second opinion (offense only; ESPN does not project IDP in a usable way).
- Web search by the model, limited to espn.com, nfl.com, nbcsports.com, rotowire.com,
  fantasypros.com, cbssports.com, pff.com and sleeper.com, at most six searches per run.
  (Sites that block Anthropic's crawler, such as The Athletic, cannot be added.) Turn off with `FFM_WEB_SEARCH=false`.

### What each proposal type does on approval

| Kind | Sleeper action |
|---|---|
| add, drop, add_drop | Immediate free-agent transaction |
| waiver_claim | Waiver claim with the stated FAAB bid (processes when your league runs waivers) |
| lineup | Sets your starters for the current week |
| ir, activate_ir, taxi | Moves the player to/from IR or taxi |
| trade | Sends the trade offer to the other manager (they still have to accept) |

### Waivers

The briefing tells the advisor how your league's waivers work: the waiver type and your
priority or FAAB budget, when claims actually processed (learned from the league's transaction
history), your pending claims and how recent ones resolved, and your `FFM_LEAGUE_NOTES`. From
that it decides between a direct add and a claim. If a claim turns out to be unnecessary, the
app adds the player directly instead. If a direct add fails because the player is still on
waivers, the default (`FFM_AUTO_CLAIM=false`) is to stop and tell you, since a claim can cost
priority or FAAB; set it to `true` to file the claim automatically.

### What happens on approval

1. The proposal is re-validated against fresh league data (the player is still a free agent,
   the drop is still on your roster, no lock was added since).
2. The move is sent to Sleeper.
3. Your roster is re-read to confirm the change is visible. If Sleeper accepted the request but
   the roster doesn't show it, the proposal is marked failed with that explanation. Waiver claims
   and trade offers are pending by nature and are not re-checked.

## Hosting it while you're away

The bot must be online for scheduled posts and buttons to work. It is one small always-on
process with no public port, so almost any host works. Three options:

**Railway (easiest).** Deploys straight from the GitHub repo using the `Dockerfile` and
`railway.json` included here; the Hobby plan is about $5 a month for this workload.

1. Push this repo to GitHub.
2. At railway.com create a project, choose **Deploy from GitHub repo**, and pick the repo.
   Railway detects the Dockerfile and builds it.
3. Open the service, go to **Variables**, click **Raw Editor**, and paste the contents of your
   local `.env`. Keep `FFM_TIMEZONE` set to your league's timezone.
4. Go to **Settings → Volumes**, add a volume, and mount it at `/app/data`. That folder holds
   the database (locks, proposals) and the player cache, so they survive redeploys.
5. Deploy. Under **Deployments → Logs** you should see "Discord connected as ..." followed by
   the next scheduled run times. `/status` in Discord confirms it from your phone.

Railway does not need a public domain for this service; ignore any prompt to add one. Every
push to `main` redeploys automatically. To renew the Sleeper session token later, edit the
variable in Railway and the service restarts on its own.

**A small always-on Linux server.** Any cheap VPS works (Hetzner, DigitalOcean,
Vultr and similar are a few dollars a month; Oracle Cloud has a free tier). Location doesn't
matter: Sleeper, Discord and Anthropic are reachable from anywhere, and the schedule follows
`FFM_TIMEZONE`, not the server's clock. With Docker installed on the server:

```bash
git clone <your repo> ffm && cd ffm
cp .env.example .env        # then paste your values
docker compose up -d --build
docker compose logs -f      # watch it connect
```

The database and player cache live in `./data` on the host, so `docker compose down` and
`up` again keeps your locks and proposal history. To update: `git pull` and
`docker compose up -d --build`.

**Your own Windows PC.** Works if the PC stays on and awake. Create a Task Scheduler task that
runs `python -m ffm run` from the project folder at logon, and disable sleep in power settings.
Less reliable than a server, and useless while the PC is off.

Either way, remember the Sleeper session token expires and has to be renewed. `/status` shows
the expiry date, and it works from any device.

## Running the tests

```bash
.venv\Scripts\python -m pytest -q
```

The tests are offline; they cover scoring, proposal validation, execution mapping and storage.

## Project layout

See `CLAUDE.md` for a file-by-file map.

## Credits

The idea of driving Sleeper through its GraphQL endpoint comes from
[cameron-eth/sleeper-sdk](https://github.com/cameron-eth/sleeper-sdk); the mutation names in
this project were confirmed against Sleeper's live schema, since the older names had been
retired. Player data, projections and league data come from
[Sleeper](https://docs.sleeper.com/) and are used for personal, non-commercial purposes.

## License

This is a personal project shared for reference. All rights reserved; it is not licensed for
reuse or redistribution.
