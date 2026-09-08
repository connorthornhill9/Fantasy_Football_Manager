"""The Claude-powered advisor: analyses the league snapshot and records proposals."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from anthropic import beta_async_tool

from .config import Config
from .executor import ExecTarget
from .league import LeagueContext
from .players import PlayerDB
from .proposals import KINDS, Proposal
from .sleeper.public import SleeperPublic
from .store import ProposalRecord, Store

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the assistant general manager for one fantasy football team on Sleeper. Your job is to find roster moves that improve the team's chances and record them as proposals the manager approves or rejects from Discord. Nothing happens on Sleeper until the manager approves, so be concrete, honest and selective.

How to work:
- Ground every recommendation in the data you are given: this week's projections under the league's own scoring (including IDP if the league uses defenders), season-long projections per game as a rest-of-season proxy, recent scoring, Sleeper injury status, the ESPN injury report, byes, the matchup, roster construction, and waiver economics. Use the tools for a deeper look at a player, a team's depth chart, another roster, or the free-agent pool.
- News: the snapshot includes an injury report with ESPN commentary and the next men up. If you have a web_search tool, use it sparingly (a handful of searches per run) and only for players on my roster, my opponent's starters, or a pickup you are seriously considering, to confirm injury, suspension, illness or role news from the last few days. Cite the source and date in the rationale. Never invent news; if you cannot confirm something, say so.
- Think like a good manager, in this order: (1) can I start a stronger lineup this week, (2) are there players on the wire clearly better than my worst bench players, this week and rest of season, (3) handcuffs, bye-week coverage and upside stashes, (4) trades that help both sides.
- Do not churn. A move must be clearly better than standing pat. Use the 'vs my lineup' and season projection columns to quantify it. Drop the least valuable bench player, never a starter for a marginal bench upgrade, and respect the active roster limit.
- The briefing lists my pending waiver claims. Treat them as moves already made: do not claim those players again or reuse their drop-side players, and remember a claim can fail if a higher-priority team wants the same player, so mention a fallback when it matters.
- LOCKED players may never be dropped or traded away; the tool will reject it. You may still put them in or out of the lineup, and you may mention in the summary what you would do if the manager unlocked someone.
- Waivers: read the league's waiver rules in the briefing, including any day-by-day notes from the manager; they decide whether today calls for a direct add (add / add_drop) or a waiver_claim. If you guess wrong the app automatically resubmits the other way, so pick the likelier one and move on. In a FAAB league size the bid to the player's value, the competition (adds in the last 24h), and my remaining budget. In a rolling-priority league a successful claim sends me to the bottom of the order, so only claim players worth that cost, and say what priority is being spent.
- Lineup: only propose a lineup if it changes something. The starters list must be complete and in the league's slot order, with player ids, and "0" only where no eligible player exists.
- Keep adds/drops separate from lineup decisions. A lineup proposal must use only players currently on the roster, because the manager may reject an add and still wants to know who to start. If a player you propose adding should also be started, say so in that proposal's rationale and expected_gain (e.g. "start him at LB over Gray once added") rather than proposing a lineup that includes him.
- The briefing states today's date. Use it to judge how fresh news is, and put the correct year in any search query.
- Trades: propose only when the partner plausibly says yes and it clearly helps me; explain their incentive. Trade proposals are advice: the manager sends the offer in Sleeper himself, so write the rationale as a pitch he could paste to the other manager.
- The briefing includes a computed optimal lineup by projection and a heuristic win probability. Treat the optimal lineup as the baseline and adjust for injury news, weather, and matchup context; when you propose a lineup, explain any deviation from the computed optimum.
- IR: only for players whose Sleeper injury status this league allows on IR.
- Record each move with propose_move, one move per call, most important first, at most a handful per run. If the tool rejects a proposal, fix it or drop it.
- Finish with a short summary written for a Discord message: what you propose and why, in priority order, then anything notable you considered and passed on (including moves blocked by locks). Plain prose with at most light bold, under 1500 characters, no tables. If nothing is worth doing, say so plainly; that is a good outcome."""

TASKS = {
    "weekly_waivers": (
        "Market review heading into week {week}. This run is about the ROSTER, not the lineup: adds, drops, waiver "
        "claims and IR moves. Think on three horizons and tag each proposal with one: this_week (a hole to fill now), "
        "short_term (the next 3-4 weeks: byes, injuries, role changes), and season (long-term investments: rising "
        "roles, stashes whose value is weeks away, handcuffs of my key players). Use season projections, depth charts, "
        "the injury report, headlines and web search for outlook news. Weigh every add against the drop it costs and "
        "against the waiver priority or FAAB it spends. Do NOT propose a lineup; if an add should start, say so in "
        "its rationale."
    ),
    "lineup": (
        "Set my starting lineup for week {week}. This run is about the LINEUP only: no adds or drops. Start from the "
        "computed optimal lineup, then adjust for what projections miss: injury designations and practice notes, "
        "suspensions, game-time decisions, weather (wind and heavy rain hurt passing games and kickers; domes do not), "
        "kickoff timing (a player whose game has started cannot be moved; prefer a healthy earlier-game player over a "
        "Monday-night question mark only if the projections are close), recent usage trends and the matchup. Propose "
        "one complete lineup only if it differs from the current one, with one sentence per change explaining why it "
        "deviates from (or agrees with) the computed optimum. If a slot has no healthy option, say so and point the "
        "manager to /analyze. End with the projected total and win probability."
    ),
    "fa_sweep": (
        "Free-agent sweep the morning after this week's games (week {week} just finished; week {next_week} is next). "
        "Today unrostered players are FREE AGENTS: direct adds, first come first served, no waiver priority spent, "
        "and in this league a player can be added even right after he played. Look for breakouts, role changes and "
        "injuries to other teams' starters that just created a new starter, and propose direct adds (kind add or "
        "add_drop) for anyone clearly worth a roster spot over my weakest bench player. Tag each with a horizon. "
        "Keep it tight: at most 3 proposals, few tool calls, no lineup, no waiver claims. If nothing stands out, say so."
    ),
    "post_waivers": (
        "Waivers just processed. First report the result of each of my claims (the briefing lists recent claim "
        "outcomes). Anyone unclaimed is now a free agent again: propose direct adds for leftovers worth a roster "
        "spot, especially if a claim of mine failed and the fallback is still available. At most 3 proposals, few "
        "tool calls, no lineup."
    ),
    "late_week": (
        "Saturday evening: the final injury reports for week {week} are in and today is a free-agent day. Two jobs, "
        "in order. (1) If one of my starters is Out, Doubtful or unlikely to play and no bench player is adequate, "
        "propose a direct free-agent replacement (kind add_drop). (2) Propose my starting lineup for week {week} "
        "using only players currently on the roster; if you also proposed an add, say in its rationale which slot he "
        "should take if approved. Weigh injuries, practice notes, kickoff times, weather and matchups. If the "
        "current lineup is already right and nobody needs replacing, say so in one short paragraph."
    ),
    "trades": (
        "Trade day. Using the positional-strength table, find one to three realistic trades that make my team better "
        "for the rest of the season: target teams that are weak where I have surplus and strong where I am thin, "
        "inspect their rosters with get_team, and make each offer fair enough that the other manager plausibly accepts. "
        "Prefer two-for-one consolidation or surplus-for-need swaps over lopsided asks. Record each with "
        "propose_move(kind='trade'); these are ideas the manager will send himself, not offers the app sends. "
        "Never offer LOCKED players. If no sensible trade exists, say so."
    ),
    "manual": "The manager asked for a review now. Focus: {focus}",
    "ask": "The manager asks: {focus}\n\nAnswer the question directly. If the best answer is a concrete move, record it with propose_move; otherwise just answer.",
}

WEB_SEARCH_DOMAINS = [
    "espn.com",
    "nfl.com",
    "nbcsports.com",
    "rotowire.com",
    "fantasypros.com",
    "cbssports.com",
    "pff.com",
    "sleeper.com",
]
MAX_PAUSE_RESTARTS = 3

# Per-run budget: reasoning effort (capped by FFM_EFFORT), web searches, and tool-loop iterations.
# The sweeps are quick checks; the Tuesday market review is the one worth spending on.
RUN_PROFILES = {
    "weekly_waivers": {"effort": None, "searches": 6, "iterations": 30},
    "trades": {"effort": None, "searches": 3, "iterations": 25},
    "fa_sweep": {"effort": "medium", "searches": 3, "iterations": 12},
    "post_waivers": {"effort": "medium", "searches": 2, "iterations": 12},
    "late_week": {"effort": "medium", "searches": 4, "iterations": 16},
    "lineup": {"effort": "medium", "searches": 4, "iterations": 16},
    "manual": {"effort": None, "searches": 6, "iterations": 30},
    "ask": {"effort": "medium", "searches": 3, "iterations": 15},
}
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}


@dataclass
class RunResult:
    run_id: int
    summary: str
    proposals: list[ProposalRecord] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    context: LeagueContext | None = None
    refused: bool = False


class Advisor:
    def __init__(self, config: Config, public: SleeperPublic, store: Store):
        self.config = config
        self.public = public
        self.store = store
        self.client = anthropic.AsyncAnthropic()
        self.auth = None  # set by build_app when a Sleeper token is configured
        self._league_id: str | None = config.sleeper_league_id
        self._roster_id: int | None = None

    # ------------------------------------------------------------------ public API

    async def build_context(self) -> LeagueContext:
        ctx = await LeagueContext.build(self.config, self.public, self.store, auth=self.auth)
        self._league_id, self._roster_id = ctx.league_id, ctx.my_roster_id
        return ctx

    async def exec_target(self) -> ExecTarget:
        """League id, my roster id and the current NFL week, for executing an approved proposal."""
        if self._league_id is None or self._roster_id is None:
            await self.build_context()
        assert self._league_id is not None and self._roster_id is not None
        state = await self.public.get_state()
        return ExecTarget(self._league_id, self._roster_id, leg=int(state.get("leg") or state.get("week") or 1))

    async def player_db(self) -> PlayerDB:
        return PlayerDB(await self.public.get_all_players())

    async def introduce(self, persona: str | None = None) -> str:
        """A short, funny public introduction of the bot as the team's manager (one cheap model call, no tools)."""
        ctx = await self.build_context()
        s = ctx.my_roster.get("settings") or {}
        starters = [ctx.players.name(p) for _, p in ctx.roster_sections(ctx.my_roster)["starters"] if p != "0"][:6]
        opp = ctx.opponent_roster()
        facts = "\n".join(
            [
                f"Team: {ctx.owner_name(ctx.my_roster)} in the league '{ctx.league_name}' ({ctx.league.get('total_rosters')} teams)",
                f"Record: {s.get('wins', 0)}-{s.get('losses', 0)}, week {ctx.week} of the {ctx.season} season",
                f"Some starters: {', '.join(starters)}",
                f"This week's opponent: {ctx.owner_name(opp) if opp else 'nobody'}",
                f"Waiver priority: {s.get('waiver_position', '?')} of {ctx.league.get('total_rosters')}",
                f"Other teams in the league: {', '.join(ctx.owner_name(r) for r in ctx.rosters if int(r['roster_id']) != ctx.my_roster_id)}",
            ]
        )
        style = (
            f"Adopt the persona of {persona} (a loving parody of their style and catchphrases, clearly a tribute, not a claim to be them)."
            if persona
            else "Invent your own name and personality for yourself as an AI front-office executive. Commit to the bit."
        )
        prompt = (
            "You are the AI assistant general manager of a fantasy football team, being introduced to the rest of the "
            "league in the team's Discord. Write a short introduction of yourself, in first person, that the manager can "
            "post to leaguemates. Be funny, confident and a little cocky about the team, with one or two light jabs at the "
            "other teams by name and a nod to the rolling-waiver grind. Mention that the manager approves every move, so "
            "nobody can blame the robot. Under 900 characters, Discord-friendly (light bold, no headers, no tables). "
            f"{style}\n\nFacts you may use:\n{facts}"
        )
        response = await self.client.messages.create(
            model=self.config.model,
            max_tokens=700,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip() or "(no introduction produced)"

    async def run(self, trigger: str, focus: str | None = None, allow_proposals: bool = True) -> RunResult:
        ctx = await self.build_context()
        run_id = self.store.create_run(trigger, focus)
        recorded: list[Proposal] = []
        tools = self._build_tools(ctx, recorded, allow_proposals)
        profile = RUN_PROFILES.get(trigger, RUN_PROFILES["manual"])
        effort = self.config.effort
        if profile["effort"] and EFFORT_RANK.get(profile["effort"], 1) < EFFORT_RANK.get(effort, 2):
            effort = profile["effort"]
        if self.config.web_search and profile["searches"] > 0:
            tools.append(
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": profile["searches"],
                    "allowed_domains": WEB_SEARCH_DOMAINS,
                }
            )

        task = TASKS.get(trigger, TASKS["manual"]).format(week=ctx.week, next_week=ctx.week + 1, focus=focus or "general review")
        if trigger not in ("ask", "manual") and focus:
            task += f"\nAdditional instructions from the manager: {focus}"
        now = datetime.now(ZoneInfo(self.config.timezone)) if self.config.timezone else datetime.now().astimezone()
        date_line = (
            f"Today is {now:%A, %B %d, %Y}, {now:%H:%M %Z}. NFL {ctx.season} season, week {ctx.week}"
            + (f"; the season started {ctx.state['season_start_date']}" if ctx.state.get("season_start_date") else "")
            + "."
        )
        snapshot = ctx.snapshot_markdown(include_team_needs=(trigger == "trades"))
        messages: list[dict] = [{"role": "user", "content": f"# Date\n{date_line}\n\n{snapshot}\n\n# Task\n{task}"}]

        kwargs: dict = {}
        model = self.config.model
        if model.startswith(("claude-opus-5", "claude-fable")):
            # Server-side refusal fallback: if the primary model declines, the API re-runs on a fallback model.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"

        log.info("Starting analysis run %s (trigger=%s, model=%s, effort=%s)", run_id, trigger, model, effort)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "iterations": 0}
        final = None
        try:
            restarts = 0
            while True:
                runner = self.client.beta.messages.tool_runner(
                    model=model,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=tools,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    cache_control={"type": "ephemeral"},
                    max_iterations=profile["iterations"],
                    **kwargs,
                )
                last = None
                async for message in runner:
                    last = message
                    usage["iterations"] += 1
                    if message.usage:
                        usage["input_tokens"] += message.usage.input_tokens or 0
                        usage["output_tokens"] += message.usage.output_tokens or 0
                        usage["cache_read_input_tokens"] += getattr(message.usage, "cache_read_input_tokens", 0) or 0
                    for block in message.content:
                        if block.type in ("tool_use", "server_tool_use"):
                            log.info("run %s: %s(%s)", run_id, block.name, _short(block.input))
                    # Mirror the history so a paused server-tool turn can be resumed with a fresh runner.
                    messages.append({"role": "assistant", "content": message.content})
                    tool_response = await runner.generate_tool_call_response()
                    if tool_response is not None:
                        messages.append(tool_response)  # type: ignore[arg-type]
                if last is not None:
                    final = last
                if last is None or last.stop_reason != "pause_turn":
                    break
                restarts += 1
                if restarts > MAX_PAUSE_RESTARTS:
                    log.warning("run %s: still paused after %d restarts; stopping", run_id, restarts)
                    break
                log.info("run %s: server tool paused the turn; resuming", run_id)
            if final is None:
                raise RuntimeError("model returned no message")
        except Exception as exc:
            log.exception("Analysis run %s failed", run_id)
            self.store.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}", usage)
            raise

        refused = final.stop_reason == "refusal"
        summary = "".join(b.text for b in final.content if b.type == "text").strip()
        if refused and not summary:
            summary = "The model declined to complete this analysis."
        if not summary:
            summary = "(no summary produced)"

        # Trades are advice: the manager sends the offer in Sleeper himself, so they never get an Approve button.
        records = [self.store.add_proposal(p, run_id, status="advice" if p.kind == "trade" else "pending") for p in recorded]
        self.store.finish_run(run_id, "refused" if refused else "done", summary, usage)
        log.info("Run %s finished: %d proposals, usage=%s", run_id, len(records), usage)
        return RunResult(run_id=run_id, summary=summary, proposals=records, usage=usage, context=ctx, refused=refused)

    # ------------------------------------------------------------------ tools

    def _build_tools(self, ctx: LeagueContext, recorded: list[Proposal], allow_proposals: bool) -> list:
        players = ctx.players
        max_proposals = self.config.max_proposals

        @beta_async_tool
        async def search_players(query: str) -> str:
            """Find Sleeper player ids by name (case-insensitive, partial names OK) or a team abbreviation for a D/ST.

            Args:
                query: Player name or fragment, e.g. "gibbs" or "Jahmyr Gibbs", or "ATL" for the Falcons defense.
            """
            hits = players.search(query, limit=10)
            if not hits:
                return f"No players match {query!r}."
            lines = []
            for p in hits:
                pid = p["player_id"]
                line = ctx.player_line(pid)
                lines.append(
                    f"- {line.name} [id {pid}] {line.pos} {line.team}, {ctx.owner_label(pid)}; proj wk {line.proj if line.proj is not None else '-'}, "
                    f"season/gm {line.ros if line.ros is not None else '-'}; injury: {line.injury or 'none'}"
                )
            return "\n".join(lines)

        @beta_async_tool
        async def get_player(player_id: str) -> str:
            """Detailed view of one player: projection breakdown, season projection, recent scores, injury notes, ownership.

            Args:
                player_id: Sleeper player id (from the snapshot tables or search_players).
            """
            p = players.get(player_id)
            if not p:
                return f"Unknown player id {player_id}."
            line = ctx.player_line(player_id, with_delta=True)
            proj = ctx.projections.get(str(player_id)) or {}
            stats = proj.get("stats") or {}
            keys = (
                "pass_att", "pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td", "rec_tgt", "rec", "rec_yd", "rec_td",
                "fum_lost", "idp_tkl_solo", "idp_tkl_ast", "idp_tkl_loss", "idp_sack", "idp_int", "idp_pass_def", "idp_ff",
            )
            breakdown = ", ".join(f"{k}={stats[k]:g}" for k in keys if k in stats) or "no game projected"
            delta = f"{line.delta:+.1f} in {line.delta_slot}" if line.delta is not None else "n/a"
            out = [
                f"{line.name} [id {player_id}] {p.get('position')} {line.team}, age {p.get('age', '?')}, {p.get('years_exp', '?')} yrs exp, "
                f"depth chart {p.get('depth_chart_position') or '?'} #{p.get('depth_chart_order') or '?'}",
                f"Ownership: {ctx.owner_label(player_id)}" + ("; LOCKED by manager" if ctx.is_locked(player_id) else ""),
                f"Week {ctx.week}: opponent {line.opp}, projected {line.proj if line.proj is not None else '-'} pts ({breakdown}); vs my lineup: {delta}",
                f"Season projection: {line.ros if line.ros is not None else '-'} pts/game",
                f"Recent weekly scores (oldest first): {', '.join(f'{x:.1f}' for x in line.recent) or 'none'}",
                f"Trending: +{line.trend_add} adds / -{line.trend_drop} drops in last 24h; Sleeper search rank {p.get('search_rank', '?')}",
                ctx.player_news_markdown(player_id),
            ]
            return "\n".join(out)

        @beta_async_tool
        async def player_news(player_id: str) -> str:
            """Injury / availability news for one player: Sleeper status, ESPN injury-report commentary, and recent headlines.

            Args:
                player_id: Sleeper player id.
            """
            if player_id not in players:
                return f"Unknown player id {player_id}."
            return f"{players.label(player_id)}\n{ctx.player_news_markdown(player_id)}"

        @beta_async_tool
        async def team_situation(team: str, position: str) -> str:
            """Depth chart for one NFL team at one position, with projections, injuries and who owns each player in my league. Use it to find the next man up when someone is hurt.

            Args:
                team: NFL team abbreviation, e.g. "KC", "SF", "WAS".
                position: One of QB, RB, WR, TE, K, DL, LB, DB.
            """
            return ctx.team_situation_markdown(team, position)

        @beta_async_tool
        async def list_free_agents(position: str, limit: int = 15) -> str:
            """List the best available free agents at a position, ranked by a blend of this week's and season projections.

            Args:
                position: One of QB, RB, WR, TE, K, DEF, DL, LB, DB.
                limit: How many to return (max 40).
            """
            lines = ctx.free_agents(position, min(max(limit, 1), 40), with_delta=True)
            if not lines:
                return f"No free agents found at {position}."
            return ctx.FA_HEADER + "\n" + "\n".join(line.row(with_delta=True) for line in lines)

        @beta_async_tool
        async def get_team(roster_id: int) -> str:
            """Full roster of another team in the league with projections (useful for trade targets and to see who owns whom).

            Args:
                roster_id: The roster id from the standings table.
            """
            roster = ctx.roster_by_id(roster_id)
            if not roster:
                return f"No roster with id {roster_id}."
            s = roster.get("settings") or {}
            header = f"{ctx.owner_name(roster)} (roster id {roster_id}), record {s.get('wins', 0)}-{s.get('losses', 0)}"
            if ctx.uses_faab:
                header += f", FAAB left ${ctx.faab_budget - int(s.get('waiver_budget_used') or 0)}"
            return header + "\n" + ctx.roster_markdown(roster, include_bench=True)

        @beta_async_tool
        async def week_games() -> str:
            """This week's NFL schedule: kickoff time, game status (pre/in progress/final), venue and weather for every team."""
            return ctx.games_markdown()

        @beta_async_tool
        async def recent_transactions(limit: int = 25) -> str:
            """Recent waiver, free-agent and trade activity in the league (last two weeks).

            Args:
                limit: Maximum number of transactions to return.
            """
            return ctx.transactions_markdown(limit)

        @beta_async_tool
        async def propose_move(
            kind: str,
            rationale: str,
            confidence: str = "medium",
            add_player_ids: list[str] | None = None,
            drop_player_ids: list[str] | None = None,
            faab_bid: int | None = None,
            starters: list[str] | None = None,
            trade_partner_roster_id: int | None = None,
            i_give: list[str] | None = None,
            i_get: list[str] | None = None,
            expected_gain: str | None = None,
            horizon: str | None = None,
        ) -> str:
            """Record one concrete roster move for the manager to approve in Discord. Validated immediately; fix and retry on rejection.

            Args:
                kind: One of add, drop, add_drop, waiver_claim, lineup, ir, activate_ir, taxi, trade.
                horizon: Why now: this_week, short_term (next 3-4 weeks) or season (long-term investment). Required for adds, drops and claims.
                rationale: Two to four sentences the manager will read: why this move, what it costs, what could go wrong. Cite news sources with dates when news drives the move.
                confidence: low, medium or high.
                add_player_ids: Players to add (add, add_drop, waiver_claim), or the single player to activate (activate_ir).
                drop_player_ids: Players to drop (drop, add_drop, waiver_claim optional), or the single player to move (ir, taxi).
                faab_bid: Dollar bid for waiver_claim in a FAAB league.
                starters: Complete starters list of player ids in slot order for kind=lineup ("0" for an empty slot).
                trade_partner_roster_id: Roster id of the other team for kind=trade.
                i_give: Player ids I send in a trade.
                i_get: Player ids I receive in a trade.
                expected_gain: Optional one-line estimate, e.g. "+3.1 projected pts this week" or "+2 pts/game rest of season".
            """
            if not allow_proposals:
                return "Proposals are disabled for this run; answer in text instead."
            if kind not in KINDS:
                return f"REJECTED: kind must be one of {', '.join(KINDS)}."
            if len(recorded) >= max_proposals:
                return f"REJECTED: the limit of {max_proposals} proposals per run is reached. Use withdraw_proposal to swap one out."
            if confidence not in ("low", "medium", "high"):
                confidence = "medium"
            if horizon not in (None, "this_week", "short_term", "season"):
                return "REJECTED: horizon must be this_week, short_term or season."
            if kind in ("add", "drop", "add_drop", "waiver_claim") and horizon is None:
                return "REJECTED: give a horizon (this_week, short_term or season) for roster moves."
            proposal = Proposal(
                kind=kind,  # type: ignore[arg-type]
                adds=[str(x) for x in (add_player_ids or [])],
                drops=[str(x) for x in (drop_player_ids or [])],
                faab_bid=faab_bid,
                starters=[str(x) for x in starters] if starters else None,
                trade_partner_roster_id=trade_partner_roster_id,
                i_give=[str(x) for x in (i_give or [])],
                i_get=[str(x) for x in (i_get or [])],
                rationale=rationale.strip(),
                confidence=confidence,  # type: ignore[arg-type]
                priority=len(recorded) + 1,
                expected_gain=expected_gain,
                horizon=horizon,  # type: ignore[arg-type]
            )
            errors = ctx.validate_proposal(proposal)
            if errors:
                return "REJECTED:\n- " + "\n- ".join(errors)
            recorded.append(proposal)
            return f"Recorded proposal #{len(recorded)}: {proposal.title(players.label)}"

        @beta_async_tool
        async def withdraw_proposal(number: int) -> str:
            """Remove a proposal you recorded earlier in this run.

            Args:
                number: The proposal number returned by propose_move.
            """
            if 1 <= number <= len(recorded):
                removed = recorded.pop(number - 1)
                for i, p in enumerate(recorded, start=1):
                    p.priority = i
                return f"Withdrew: {removed.title(players.label)}. {len(recorded)} proposals remain."
            return f"No proposal #{number}."

        tools: list = [search_players, get_player, player_news, team_situation, list_free_agents, get_team, week_games, recent_transactions]
        if allow_proposals:
            tools += [propose_move, withdraw_proposal]
        return tools


def _short(value: object, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
