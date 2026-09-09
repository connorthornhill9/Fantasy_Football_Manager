"""Discord front end: posts proposals with Approve / Reject buttons and offers slash commands."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .agent import RunResult
from .app import App
from .players import PlayerDB
from .proposals import HORIZON_LABELS, KIND_LABELS
from .scheduler import build_scheduler, describe_jobs
from .store import ProposalRecord

log = logging.getLogger(__name__)

STATUS_COLORS = {
    "pending": discord.Color.blurple(),
    "approved": discord.Color.gold(),
    "executing": discord.Color.gold(),
    "executed": discord.Color.green(),
    "failed": discord.Color.red(),
    "rejected": discord.Color.dark_grey(),
    "expired": discord.Color.light_grey(),
    "advice": discord.Color.purple(),
}
TRIGGER_TITLES = {
    "weekly_waivers": "Market review",
    "fa_sweep": "Free-agent sweep",
    "post_waivers": "Post-waiver sweep",
    "late_week": "Injury replacements + lineup",
    "lineup": "Lineup",
    "trades": "Trade ideas",
    "manual": "Team review",
    "ask": "Answer",
}
PENDING_TTL = timedelta(days=7)


# ---------------------------------------------------------------------- embeds


def proposal_embed(rec: ProposalRecord, players: PlayerDB, note: str | None = None) -> discord.Embed:
    p = rec.proposal
    embed = discord.Embed(
        title=f"#{rec.id}  {p.title(players.label)}"[:256],
        description=p.rationale[:3500],
        color=STATUS_COLORS.get(rec.status, discord.Color.blurple()),
    )
    embed.add_field(name="Type", value=KIND_LABELS.get(p.kind, p.kind), inline=True)
    embed.add_field(name="Confidence", value=p.confidence, inline=True)
    if p.horizon:
        embed.add_field(name="Horizon", value=HORIZON_LABELS.get(p.horizon, p.horizon), inline=True)
    if p.expected_gain:
        embed.add_field(name="Expected gain", value=p.expected_gain[:200], inline=True)
    if p.kind == "waiver_claim" and p.faab_bid is not None:
        embed.add_field(name="FAAB bid", value=f"${p.faab_bid}", inline=True)
    if p.kind == "lineup" and p.starters:
        lines = [f"{i + 1}. {players.label(s) if s != '0' else '(empty)'}" for i, s in enumerate(p.starters)]
        embed.add_field(name="Starters (slot order)", value="\n".join(lines)[:1024], inline=False)
    if p.kind == "trade":
        embed.add_field(name="I give", value="\n".join(players.label(x) for x in p.i_give) or "nothing", inline=True)
        embed.add_field(name="I get", value="\n".join(players.label(x) for x in p.i_get) or "nothing", inline=True)
    status_line = rec.status.capitalize()
    if rec.status == "advice":
        status_line = "Advice only: send this offer yourself in Sleeper if you like it."
    if rec.result and rec.result.get("message"):
        status_line += f": {rec.result['message']}"
    if note:
        status_line += f"\n{note}"
    embed.add_field(name="Status", value=status_line[:1024], inline=False)
    embed.set_footer(text=f"Proposal #{rec.id} · {rec.created_at[:16].replace('T', ' ')} UTC")
    return embed


def summary_embed(result: RunResult, trigger: str) -> discord.Embed:
    ctx = result.context
    title = TRIGGER_TITLES.get(trigger, "Review")
    if ctx:
        title += f" — week {ctx.week}"
    embed = discord.Embed(title=title, description=result.summary[:4000], color=discord.Color.dark_teal())
    n = len(result.proposals)
    footer = f"{n} proposal{'s' if n != 1 else ''}"
    if result.usage:
        footer += f" · {result.usage.get('input_tokens', 0):,} in / {result.usage.get('output_tokens', 0):,} out tokens"
    embed.set_footer(text=footer)
    return embed


def matchup_embed(ctx) -> discord.Embed:
    s = ctx.matchup_summary()
    starts, sits, gain = ctx.lineup_changes()
    players = ctx.players
    embed = discord.Embed(title=f"Week {ctx.week} matchup", color=discord.Color.dark_teal())
    if s.get("opponent"):
        embed.description = (
            f"**{ctx.owner_name(ctx.my_roster)}** {s['my_current']:.1f} proj  vs  **{s['opponent']}** {s['opp_current']:.1f} proj\n"
            f"Win probability: **{s['win_prob_current']:.0%}** with current lineups, {s['win_prob_optimal']:.0%} if both start their best."
        )
    else:
        embed.description = f"Projected {s['my_current']:.1f}. No opponent this week."
    embed.add_field(name="Best lineup by projection", value=f"```\n{ctx.optimal_lineup_compact()}\n```", inline=False)
    if starts or sits:
        lines = []
        if starts:
            lines.append("Start: " + ", ".join(f"{players.name(p)} ({_pts(ctx.proj_pts(p))})" for p in starts))
        if sits:
            lines.append("Sit: " + ", ".join(f"{players.name(p)} ({_pts(ctx.proj_pts(p))})" for p in sits))
        lines.append(f"Projected gain: {gain:+.1f}")
        embed.add_field(name="Changes from your current lineup (* above)", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Changes", value="Your current lineup is already the best by projection.", inline=False)
    embed.set_footer(text="Projections exclude players marked Out, Doubtful, IR or suspended. Win probability is a heuristic.")
    return embed


def _pts(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


# ---------------------------------------------------------------------- persistent buttons


class ApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ffm:approve:(?P<id>\d+)"):
    def __init__(self, proposal_id: int):
        super().__init__(
            discord.ui.Button(
                label="Approve", style=discord.ButtonStyle.success, custom_id=f"ffm:approve:{proposal_id}", emoji="✅"
            )
        )
        self.proposal_id = proposal_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: FFMBot = interaction.client  # type: ignore[assignment]
        await bot.decide(interaction, self.proposal_id, approve=True)


class RejectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ffm:reject:(?P<id>\d+)"):
    def __init__(self, proposal_id: int):
        super().__init__(
            discord.ui.Button(
                label="Reject", style=discord.ButtonStyle.secondary, custom_id=f"ffm:reject:{proposal_id}", emoji="✖"
            )
        )
        self.proposal_id = proposal_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: FFMBot = interaction.client  # type: ignore[assignment]
        await bot.decide(interaction, self.proposal_id, approve=False)


def decision_view(proposal_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApproveButton(proposal_id))
    view.add_item(RejectButton(proposal_id))
    return view


# ---------------------------------------------------------------------- bot


class FFMBot(commands.Bot):
    def __init__(self, app: App, start_scheduler: bool = True):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.app = app
        self.config = app.config
        self._analysis_lock = asyncio.Lock()
        self._players: PlayerDB | None = None
        self._start_scheduler = start_scheduler
        self.scheduler = build_scheduler(self.config, self.scheduled_run)

    # ----------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ApproveButton, RejectButton)
        register_commands(self)
        if self.config.discord_guild_id:
            guild = discord.Object(id=self.config.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", self.config.discord_guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)
        if self._start_scheduler and not self.scheduler.running:
            self.scheduler.start()
            log.info("Scheduler started:\n%s", describe_jobs(self.scheduler))

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await super().close()

    # ----------------------------------------------------------------- helpers

    def is_owner_user(self, user: discord.abc.User) -> bool:
        return self.config.discord_owner_id is not None and user.id == self.config.discord_owner_id

    async def players(self) -> PlayerDB:
        if self._players is None:
            self._players = await self.app.advisor.player_db()
        return self._players

    async def target_channel(self) -> discord.abc.Messageable:
        assert self.config.discord_channel_id is not None
        channel = self.get_channel(self.config.discord_channel_id) or await self.fetch_channel(self.config.discord_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Channel {self.config.discord_channel_id} is not a text channel")
        return channel

    # ----------------------------------------------------------------- analysis

    async def scheduled_run(self, trigger: str) -> None:
        log.info("Scheduled run: %s", trigger)
        await self.run_analysis(trigger)

    async def run_analysis(self, trigger: str, focus: str | None = None) -> RunResult | None:
        if self._analysis_lock.locked():
            channel = await self.target_channel()
            await channel.send("An analysis is already running; try again in a few minutes.")
            return None
        async with self._analysis_lock:
            self._expire_stale()
            channel = await self.target_channel()
            try:
                result = await self.app.advisor.run(trigger, focus)
            except Exception as exc:  # noqa: BLE001
                log.exception("Analysis failed")
                await channel.send(f"Analysis failed: `{type(exc).__name__}: {str(exc)[:500]}`")
                return None
            if result.context is not None:
                self._players = result.context.players
            await self.post_result(result, trigger)
            return result

    def _expire_stale(self) -> None:
        cutoff = (datetime.now(timezone.utc) - PENDING_TTL).isoformat(timespec="seconds")
        n = self.app.store.expire_pending_older_than(cutoff)
        if n:
            log.info("Expired %d stale pending proposals", n)

    async def post_result(self, result: RunResult, trigger: str) -> None:
        channel = await self.target_channel()
        await channel.send(embed=summary_embed(result, trigger))
        players = await self.players()
        for rec in result.proposals:
            view = decision_view(rec.id) if rec.status == "pending" else None
            msg = await channel.send(embed=proposal_embed(rec, players), view=view)
            self.app.store.set_discord_message(rec.id, channel.id, msg.id)  # type: ignore[attr-defined]

    async def post_matchup(self, channel: discord.abc.Messageable | None = None) -> None:
        """Post the computed optimal lineup and win probability (no model call)."""
        ctx = await self.app.advisor.build_context()
        self._players = ctx.players
        target = channel or await self.target_channel()
        await target.send(embed=matchup_embed(ctx))

    # ----------------------------------------------------------------- decisions

    async def decide(self, interaction: discord.Interaction, proposal_id: int, approve: bool) -> None:
        if not self.is_owner_user(interaction.user):
            await interaction.response.send_message("Only the team owner can approve or reject moves.", ephemeral=True)
            return
        store = self.app.store
        rec = store.get_proposal(proposal_id)
        players = await self.players()
        if rec is None:
            await interaction.response.send_message(f"Proposal #{proposal_id} no longer exists.", ephemeral=True)
            return
        if rec.status != "pending":
            await interaction.response.edit_message(embed=proposal_embed(rec, players), view=None)
            await interaction.followup.send(f"Proposal #{proposal_id} is already {rec.status}.", ephemeral=True)
            return

        if not approve:
            store.set_status(proposal_id, "rejected")
            rec = store.get_proposal(proposal_id)
            assert rec is not None
            await interaction.response.edit_message(embed=proposal_embed(rec, players), view=None)
            return

        if not store.claim_for_execution(proposal_id):
            await interaction.response.send_message("That proposal was just handled by someone else.", ephemeral=True)
            return
        rec = store.get_proposal(proposal_id)
        assert rec is not None
        await interaction.response.edit_message(embed=proposal_embed(rec, players, note="Re-checking and sending to Sleeper..."), view=None)
        await self._execute_record(rec)
        rec = store.get_proposal(proposal_id)
        assert rec is not None
        try:
            await interaction.edit_original_response(embed=proposal_embed(rec, players), view=None)
        except discord.HTTPException:
            channel = await self.target_channel()
            await channel.send(embed=proposal_embed(rec, players))

    async def decide_by_id(self, interaction: discord.Interaction, proposal_id: int, approve: bool) -> None:
        """Slash-command path: same as the buttons but must locate and edit the original message itself."""
        if not self.is_owner_user(interaction.user):
            await interaction.response.send_message("Only the team owner can approve or reject moves.", ephemeral=True)
            return
        store = self.app.store
        rec = store.get_proposal(proposal_id)
        if rec is None:
            await interaction.response.send_message(f"No proposal #{proposal_id}.", ephemeral=True)
            return
        if rec.status != "pending":
            await interaction.response.send_message(f"Proposal #{proposal_id} is already {rec.status}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        players = await self.players()
        if approve:
            if not store.claim_for_execution(proposal_id):
                await interaction.followup.send("That proposal was just handled elsewhere.", ephemeral=True)
                return
            await self._execute_record(rec)
        else:
            store.set_status(proposal_id, "rejected")
        rec = store.get_proposal(proposal_id)
        assert rec is not None
        await self._refresh_proposal_message(rec, players)
        msg = rec.result.get("message") if rec.result else rec.status
        await interaction.followup.send(f"Proposal #{proposal_id}: {rec.status}. {msg}"[:1900], ephemeral=True)

    async def _execute_record(self, rec: ProposalRecord) -> None:
        """Preflight against fresh league data, then execute (or dry-run) and store the outcome."""
        store = self.app.store
        try:
            ctx = await self.app.advisor.build_context()
            self._players = ctx.players
            errors = ctx.validate_proposal(rec.proposal)
            if errors:
                store.set_status(
                    rec.id, "failed", {"ok": False, "message": "No longer valid: " + "; ".join(errors)}
                )
                return
            target = await self.app.advisor.exec_target()
            result = await self.app.executor.execute(rec.proposal, target)
        except Exception as exc:  # noqa: BLE001
            log.exception("Execution of proposal %s crashed", rec.id)
            store.set_status(rec.id, "failed", {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
            return
        store.set_status(rec.id, "executed" if result.ok else "failed", result.to_dict())

    async def _refresh_proposal_message(self, rec: ProposalRecord, players: PlayerDB) -> None:
        if not rec.discord_channel_id or not rec.discord_message_id:
            return
        try:
            channel = self.get_channel(rec.discord_channel_id) or await self.fetch_channel(rec.discord_channel_id)
            message = await channel.fetch_message(rec.discord_message_id)  # type: ignore[union-attr]
            await message.edit(embed=proposal_embed(rec, players), view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update Discord message for proposal %s: %s", rec.id, exc)


async def claims_text(bot: "FFMBot", limit: int = 12) -> str:
    """Human summary of my waiver claims (pending first, then the most recent outcomes)."""
    app = bot.app
    assert app.auth is not None
    target = await app.advisor.exec_target()
    players = await bot.players()
    txs = await app.auth.get_transactions(target.league_id, types=["waiver"], limit=200)
    mine = [t for t in txs if target.roster_id in (t.get("roster_ids") or [])]
    if not mine:
        return "No waiver claims on record for your team."
    mine.sort(key=lambda t: (t.get("status") != "pending", -(t.get("created") or 0)))
    lines = []
    for t in mine[:limit]:
        adds = ", ".join(players.label(p) for p in (t.get("adds") or {})) or "-"
        drops = ", ".join(players.label(p) for p in (t.get("drops") or {})) or "-"
        when = datetime.fromtimestamp(int(t.get("created") or 0) / 1000, tz=timezone.utc).strftime("%b %d")
        settings = t.get("settings") or {}
        bid = f", bid ${settings['waiver_bid']}" if settings.get("waiver_bid") is not None else ""
        note = (t.get("metadata") or {}).get("notes")
        status = str(t.get("status") or "?")
        icon = {"pending": "⏳", "complete": "✅", "failed": "❌"}.get(status, "•")
        lines.append(f"{icon} {when} {status}: add {adds}; drop {drops}{bid}" + (f" — {note}" if note else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------- slash commands


def register_commands(bot: FFMBot) -> None:
    tree = bot.tree

    def owner_only(interaction: discord.Interaction) -> bool:
        return bot.is_owner_user(interaction.user)

    async def deny(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Only the team owner can use this.", ephemeral=True)

    @tree.command(name="analyze", description="Market review: adds, drops, claims and stashes for this week and the season")
    @app_commands.describe(focus="Optional instructions, e.g. 'find me a TE' or 'RB depth only'")
    async def analyze(interaction: discord.Interaction, focus: str | None = None) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.send_message("On it. This usually takes a few minutes; proposals will appear in the channel.", ephemeral=True)
        result = await bot.run_analysis("manual" if focus else "weekly_waivers", focus)
        if result is not None:
            try:
                await interaction.followup.send(f"Done: {len(result.proposals)} proposal(s) posted.", ephemeral=True)
            except discord.HTTPException:
                pass

    @tree.command(name="lineup", description="Set my best lineup for this week: injuries, news, weather, kickoff times")
    async def lineup(interaction: discord.Interaction) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.send_message("Checking the lineup; results will appear in the channel.", ephemeral=True)
        await bot.run_analysis("lineup")

    RATINGS = [app_commands.Choice(name="PG-13", value="pg13"), app_commands.Choice(name="R", value="r")]

    @tree.command(name="introduce", description="Have the bot introduce itself as your team's manager (posted publicly, for the league to see)")
    @app_commands.describe(persona="Optional: a personality to imitate, e.g. 'John Madden'. Leave blank and it invents its own.", rating="PG-13 or R (default from FFM_ROAST_RATING)")
    @app_commands.choices(rating=RATINGS)
    async def introduce(interaction: discord.Interaction, persona: str | None = None, rating: str | None = None) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            text = await bot.app.advisor.introduce(persona, rating)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not write an introduction: {exc}", ephemeral=True)
            return
        await interaction.followup.send(text[:1990])

    @tree.command(name="roast", description="Smack talk the other teams from their actual rosters (posted publicly)")
    @app_commands.describe(team="Optional: one team name to roast; leave blank for the whole league", persona="Optional personality to imitate", rating="PG-13 or R (default from FFM_ROAST_RATING)")
    @app_commands.choices(rating=RATINGS)
    async def roast(interaction: discord.Interaction, team: str | None = None, persona: str | None = None, rating: str | None = None) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            text = await bot.app.advisor.roast(team, persona, rating)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not write a roast: {exc}", ephemeral=True)
            return
        first, rest = text[:1990], text[1990:]
        await interaction.followup.send(first)
        while rest:  # Discord caps a message at 2000 characters
            chunk, rest = rest[:1990], rest[1990:]
            await interaction.followup.send(chunk)

    @tree.command(name="recap", description="Grade the league's waiver and trade moves from the last two weeks (posted publicly)")
    @app_commands.describe(persona="Optional personality to imitate", rating="PG-13 or R (default from FFM_ROAST_RATING)")
    @app_commands.choices(rating=RATINGS)
    async def recap(interaction: discord.Interaction, persona: str | None = None, rating: str | None = None) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            text = await bot.app.advisor.recap(persona, rating)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not write a recap: {exc}", ephemeral=True)
            return
        first, rest = text[:1990], text[1990:]
        await interaction.followup.send(first)
        while rest:
            chunk, rest = rest[:1990], rest[1990:]
            await interaction.followup.send(chunk)

    @tree.command(name="sweep", description="Quick free-agent sweep: direct adds worth making today (no claims, no lineup)")
    async def sweep(interaction: discord.Interaction) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.send_message("Sweeping the free-agent pool; results will appear in the channel.", ephemeral=True)
        await bot.run_analysis("fa_sweep")

    @tree.command(name="matchup", description="Optimal lineup by projection and win probability for this week (no AI call)")
    async def matchup(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            ctx = await bot.app.advisor.build_context()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not load the league: {exc}")
            return
        bot._players = ctx.players
        await interaction.followup.send(embed=matchup_embed(ctx))

    @tree.command(name="trades", description="Ask the advisor for realistic trade ideas (advice only, nothing is sent)")
    async def trades(interaction: discord.Interaction) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.send_message("Looking for trades; ideas will appear in the channel in a few minutes.", ephemeral=True)
        await bot.run_analysis("trades")

    @tree.command(name="ask", description="Ask the advisor a question about your team or the league")
    @app_commands.describe(question="e.g. 'Should I start X or Y this week?' or 'Who is worth a FAAB bid?'")
    async def ask(interaction: discord.Interaction, question: str) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.send_message(f"Thinking about: *{question[:200]}*", ephemeral=True)
        await bot.run_analysis("ask", question)

    @tree.command(name="claims", description="My waiver claims: pending ones and how recent ones resolved")
    async def claims(interaction: discord.Interaction) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if bot.app.auth is None:
            await interaction.followup.send("No Sleeper token is set, so claims cannot be read.", ephemeral=True)
            return
        try:
            text = await claims_text(bot)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not read claims: {exc}", ephemeral=True)
            return
        await interaction.followup.send(text[:1900], ephemeral=True)

    @tree.command(name="pending", description="List proposals waiting for a decision")
    async def pending(interaction: discord.Interaction) -> None:
        recs = bot.app.store.pending()
        if not recs:
            await interaction.response.send_message("No pending proposals.", ephemeral=True)
            return
        players = await bot.players()
        lines = [f"#{r.id} · {r.proposal.title(players.label)}" for r in recs]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @tree.command(name="approve", description="Approve a proposal by id (same as the button)")
    async def approve(interaction: discord.Interaction, proposal_id: int) -> None:
        await bot.decide_by_id(interaction, proposal_id, approve=True)

    @tree.command(name="reject", description="Reject a proposal by id")
    async def reject(interaction: discord.Interaction, proposal_id: int) -> None:
        await bot.decide_by_id(interaction, proposal_id, approve=False)

    async def resolve_roster_player(interaction: discord.Interaction, query: str) -> str | None:
        """Turn a name or id into a player id on the owner's roster, replying with choices if ambiguous."""
        try:
            ctx = await bot.app.advisor.build_context()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Could not load the roster: {exc}", ephemeral=True)
            return None
        bot._players = ctx.players
        hits = [p["player_id"] for p in ctx.players.search(query, limit=10)]
        mine = [pid for pid in hits if ctx.on_my_roster(pid)]
        if len(mine) == 1:
            return mine[0]
        if not hits:
            await interaction.followup.send(f"No player matches *{query}*.", ephemeral=True)
        elif not mine:
            await interaction.followup.send(
                f"*{query}* is not on your roster. Matches: " + ", ".join(ctx.players.label(p) for p in hits[:5]), ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Several players on your roster match; use the id: " + ", ".join(f"{ctx.players.label(p)} = {p}" for p in mine),
                ephemeral=True,
            )
        return None

    @tree.command(name="lock", description="Protect a player: the advisor may never propose dropping or trading them")
    @app_commands.describe(player="Player name (or Sleeper id) on your roster")
    async def lock(interaction: discord.Interaction, player: str) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        pid = await resolve_roster_player(interaction, player)
        if pid is None:
            return
        players = await bot.players()
        added = bot.app.store.add_lock(pid, players.name(pid))
        await interaction.followup.send(
            f"🔒 {players.label(pid)} is {'now locked' if added else 'already locked'}.", ephemeral=True
        )

    @tree.command(name="unlock", description="Remove a player's lock")
    @app_commands.describe(player="Player name (or Sleeper id)")
    async def unlock(interaction: discord.Interaction, player: str) -> None:
        if not owner_only(interaction):
            return await deny(interaction)
        locks = bot.app.store.locks()
        players = await bot.players()
        query = player.strip().lower()
        matches = [pid for pid, name in locks.items() if pid == query or query in name.lower()]
        if len(matches) != 1:
            listing = ", ".join(f"{name} = {pid}" for pid, name in locks.items()) or "nothing is locked"
            await interaction.response.send_message(
                f"Could not find exactly one locked player matching *{player}*. Locked: {listing}", ephemeral=True
            )
            return
        bot.app.store.remove_lock(matches[0])
        await interaction.response.send_message(f"🔓 {players.label(matches[0])} is unlocked.", ephemeral=True)

    @tree.command(name="locks", description="List locked players")
    async def locks_cmd(interaction: discord.Interaction) -> None:
        locks = bot.app.store.locks()
        if not locks:
            await interaction.response.send_message("No players are locked.", ephemeral=True)
            return
        players = await bot.players()
        await interaction.response.send_message(
            "🔒 Locked: " + ", ".join(players.label(pid) for pid in locks), ephemeral=True
        )

    @tree.command(name="status", description="Show bot status, token expiry and the next scheduled runs")
    async def status(interaction: discord.Interaction) -> None:
        cfg = bot.config
        auth = bot.app.auth
        if auth is None:
            token = "not set (dry run)"
        elif auth.token_info.is_expired:
            token = "EXPIRED — capture a new one"
        elif auth.token_info.seconds_remaining is None:
            token = f"set (user {auth.token_info.display_name or auth.token_info.user_id}, no expiry claim)"
        else:
            token = f"set, expires in {auth.token_info.seconds_remaining / 86400:.1f} days"
        runs = bot.app.store.recent_runs(1)
        last = f"run {runs[0]['id']} ({runs[0]['trigger']}, {runs[0]['status']}) at {runs[0]['created_at'][:16]}" if runs else "none"
        lines = [
            f"Model: {cfg.model} (effort {cfg.effort}); web search {'on' if cfg.web_search else 'off'}; ESPN news {'on' if cfg.espn_news else 'off'}",
            f"Sleeper token: {token}",
            f"Dry run: {'yes (approved moves are logged, not sent)' if bot.app.executor.dry_run else 'no'}",
            f"Pending proposals: {len(bot.app.store.pending())}; locked players: {len(bot.app.store.locks())}",
            f"Last run: {last}",
            describe_jobs(bot.scheduler),
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def run_bot(app: App) -> None:
    app.config.require_discord()
    bot = FFMBot(app)
    assert app.config.discord_bot_token is not None
    async with bot:
        await bot.start(app.config.discord_bot_token)


async def post_once(app: App, trigger: str, focus: str | None = None) -> RunResult | None:
    """Connect to Discord, run one analysis, post it to the channel, and disconnect."""
    app.config.require_discord()
    bot = FFMBot(app, start_scheduler=False)
    assert app.config.discord_bot_token is not None
    async with bot:
        await bot.login(app.config.discord_bot_token)
        connect_task = asyncio.create_task(bot.connect())
        try:
            await asyncio.wait_for(bot.wait_until_ready(), timeout=60)
            channel = await bot.target_channel()
            log.info("Posting to #%s", getattr(channel, "name", channel))
            return await bot.run_analysis(trigger, focus)
        finally:
            await bot.close()
            connect_task.cancel()
