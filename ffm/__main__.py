"""Command-line entry point: `python -m ffm <command>`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .app import App, build_app, setup_logging
from .config import Config, ConfigError

log = logging.getLogger("ffm")


def _print_result(app: App, result) -> None:
    print("\n" + "=" * 78)
    print(result.summary)
    print("=" * 78)
    if not result.proposals:
        print("\nNo proposals recorded.")
        return
    players = result.context.players
    print(f"\n{len(result.proposals)} proposal(s) recorded:")
    for rec in result.proposals:
        p = rec.proposal
        print(f"\n  #{rec.id}  [{p.kind}, {p.confidence}, {rec.status}]  {p.title(players.label)}")
        if p.expected_gain:
            print(f"      gain: {p.expected_gain}")
        print(f"      {p.rationale}")
    print("\nApprove one from the terminal with:  python -m ffm execute <id>")
    usage = result.usage
    print(f"\nTokens: {usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out, {usage.get('iterations', 0)} model calls")


async def _run_maybe_post(app: App, trigger: str, focus: str | None, post: bool) -> int:
    if post:
        from .discord_bot import post_once

        result = await post_once(app, trigger, focus)
        if result is None:
            print("Analysis did not complete; see the log and the Discord channel for details.")
            return 1
        print(f"Posted run {result.run_id} to Discord with {len(result.proposals)} proposal(s).")
    else:
        result = await app.advisor.run(trigger, focus)
    _print_result(app, result)
    return 0


async def cmd_analyze(app: App, args: argparse.Namespace) -> int:
    return await _run_maybe_post(app, args.trigger, args.focus, args.post)


async def cmd_ask(app: App, args: argparse.Namespace) -> int:
    return await _run_maybe_post(app, "ask", " ".join(args.question), args.post)


async def cmd_snapshot(app: App, args: argparse.Namespace) -> int:
    """Print the full briefing the model receives (for debugging)."""
    ctx = await app.advisor.build_context()
    print(ctx.snapshot_markdown(include_team_needs=args.trades))
    return 0


async def cmd_claims(app: App, args: argparse.Namespace) -> int:
    if app.auth is None:
        print("No Sleeper token is set, so claims cannot be read.")
        return 1
    target = await app.advisor.exec_target()
    players = await app.advisor.player_db()
    txs = await app.auth.get_transactions(target.league_id, types=["waiver"], limit=200)
    mine = [t for t in txs if target.roster_id in (t.get("roster_ids") or [])]
    if not mine:
        print("No waiver claims on record for your team.")
        return 0
    mine.sort(key=lambda t: (t.get("status") != "pending", -(t.get("created") or 0)))
    for t in mine[:20]:
        adds = ", ".join(players.label(p) for p in (t.get("adds") or {})) or "-"
        drops = ", ".join(players.label(p) for p in (t.get("drops") or {})) or "-"
        note = (t.get("metadata") or {}).get("notes")
        print(f"{t.get('status'):8} add {adds}; drop {drops}" + (f"  ({note})" if note else ""))
    return 0


async def cmd_introduce(app: App, args: argparse.Namespace) -> int:
    print(await app.advisor.introduce(" ".join(args.persona) if args.persona else None))
    return 0


async def cmd_roast(app: App, args: argparse.Namespace) -> int:
    print(await app.advisor.roast(" ".join(args.team) if args.team else None, args.persona))
    return 0


async def cmd_matchup(app: App, args: argparse.Namespace) -> int:
    if args.post:
        from .discord_bot import FFMBot

        app.config.require_discord()
        bot = FFMBot(app, start_scheduler=False)
        async with bot:
            await bot.login(app.config.discord_bot_token)  # type: ignore[arg-type]
            task = asyncio.create_task(bot.connect())
            try:
                await asyncio.wait_for(bot.wait_until_ready(), timeout=60)
                await bot.post_matchup()
            finally:
                await bot.close()
                task.cancel()
        print("Posted the matchup to Discord.")
    ctx = await app.advisor.build_context()
    print(ctx.matchup_summary_markdown())
    print()
    print(ctx.optimal_lineup_markdown())
    return 0


async def cmd_whoami(app: App, args: argparse.Namespace) -> int:
    cfg = app.config
    user = await app.public.get_user(cfg.sleeper_username or "")
    if not user:
        print(f"Sleeper user {cfg.sleeper_username!r} not found")
        return 1
    state = await app.public.get_state()
    season = str(state.get("league_season") or state.get("season"))
    print(f"Sleeper user: {user.get('display_name')} (id {user['user_id']})")
    print(f"NFL state: season {season}, week {state.get('week')} ({state.get('season_type')})")
    leagues = await app.public.get_user_leagues(user["user_id"], season)
    print(f"Leagues in {season}:")
    for lg in leagues:
        marker = "  <- selected" if lg["league_id"] == cfg.sleeper_league_id else ""
        print(f"  {lg['league_id']}  {lg['name']} ({lg.get('total_rosters')} teams){marker}")
    if app.auth:
        info = app.auth.token_info
        who = info.display_name or info.user_id or "unknown"
        if info.is_expired:
            print(f"Sleeper token: EXPIRED (user {who})")
        elif info.seconds_remaining is None:
            print(f"Sleeper token: set for user {who}, no expiry claim")
        else:
            print(f"Sleeper token: set for user {who}, expires in {info.seconds_remaining / 86400:.1f} days")
        if info.user_id and info.user_id != user["user_id"]:
            print("  WARNING: the token belongs to a different Sleeper user than SLEEPER_USERNAME")
    else:
        print("Sleeper token: not set (moves cannot be executed)")
    print(f"Model: {cfg.model}, effort {cfg.effort}, dry run: {cfg.dry_run}")
    print(f"Discord: {'configured' if cfg.discord_enabled else 'not configured'}")
    return 0


async def cmd_pending(app: App, args: argparse.Namespace) -> int:
    recs = app.store.pending()
    if not recs:
        print("No pending proposals.")
        return 0
    players = await app.advisor.player_db()
    for rec in recs:
        print(f"#{rec.id}  [{rec.kind}]  {rec.proposal.title(players.label)}")
        print(f"     {rec.proposal.rationale}\n")
    return 0


async def cmd_execute(app: App, args: argparse.Namespace) -> int:
    rec = app.store.get_proposal(args.id)
    if rec is None:
        print(f"No proposal #{args.id}")
        return 1
    if rec.status != "pending":
        print(f"Proposal #{args.id} is {rec.status}, not pending")
        return 1
    players = await app.advisor.player_db()
    print(f"#{rec.id}  {rec.proposal.title(players.label)}")
    print(f"     {rec.proposal.rationale}")
    if app.executor.dry_run:
        print("(dry run: nothing will be sent to Sleeper)")
    if not args.yes:
        answer = input("Execute this move on Sleeper? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0
    if not app.store.claim_for_execution(rec.id):
        print("Proposal was already handled.")
        return 1
    target = await app.advisor.exec_target()
    result = await app.executor.execute(rec.proposal, target)
    app.store.set_status(rec.id, "executed" if result.ok else "failed", result.to_dict())
    print(("OK: " if result.ok else "FAILED: ") + result.message)
    return 0 if result.ok else 1


async def cmd_reject(app: App, args: argparse.Namespace) -> int:
    rec = app.store.get_proposal(args.id)
    if rec is None or rec.status != "pending":
        print("No pending proposal with that id.")
        return 1
    app.store.set_status(rec.id, "rejected")
    print(f"Rejected #{rec.id}.")
    return 0


async def cmd_lock(app: App, args: argparse.Namespace) -> int:
    ctx = await app.advisor.build_context()
    query = " ".join(args.player)
    hits = [p["player_id"] for p in ctx.players.search(query, limit=10)]
    mine = [pid for pid in hits if ctx.on_my_roster(pid)]
    if len(mine) != 1:
        if not hits:
            print(f"No player matches {query!r}.")
        elif not mine:
            print(f"{query!r} is not on your roster. Matches: " + ", ".join(ctx.players.label(p) for p in hits[:5]))
        else:
            print("Several roster players match; use the id: " + ", ".join(f"{ctx.players.label(p)} = {p}" for p in mine))
        return 1
    added = app.store.add_lock(mine[0], ctx.players.name(mine[0]))
    print(f"{ctx.players.label(mine[0])} is {'now locked' if added else 'already locked'}.")
    return 0


async def cmd_unlock(app: App, args: argparse.Namespace) -> int:
    query = " ".join(args.player).strip().lower()
    locks = app.store.locks()
    matches = [pid for pid, name in locks.items() if pid == query or query in name.lower()]
    if len(matches) != 1:
        print("Could not find exactly one locked player matching that. Locked: " + (", ".join(f"{n} = {p}" for p, n in locks.items()) or "none"))
        return 1
    app.store.remove_lock(matches[0])
    print(f"Unlocked {locks[matches[0]]}.")
    return 0


async def cmd_locks(app: App, args: argparse.Namespace) -> int:
    locks = app.store.locks()
    if not locks:
        print("No players are locked.")
    for pid, name in locks.items():
        print(f"  {name}  (id {pid})")
    return 0


async def cmd_run(app: App, args: argparse.Namespace) -> int:
    from .discord_bot import run_bot

    await run_bot(app)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ffm", description="Fantasy Football Manager for Sleeper")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="start the Discord bot and the schedule (default)")

    p = sub.add_parser("analyze", help="run an analysis now and print the proposals")
    p.add_argument(
        "--trigger",
        choices=["weekly_waivers", "fa_sweep", "post_waivers", "late_week", "lineup", "trades", "manual"],
        default="weekly_waivers",
    )
    p.add_argument("--focus", help="extra instructions for the advisor")
    p.add_argument("--post", action="store_true", help="also post the result to the Discord channel")

    p = sub.add_parser("ask", help="ask the advisor a question")
    p.add_argument("question", nargs="+")
    p.add_argument("--post", action="store_true", help="also post the result to the Discord channel")

    p = sub.add_parser("snapshot", help="print the full briefing the model receives (debugging)")
    p.add_argument("--trades", action="store_true", help="include the positional-strength table")

    sub.add_parser("claims", help="list my waiver claims and their outcomes")

    p = sub.add_parser("introduce", help="print a funny self-introduction for the league")
    p.add_argument("persona", nargs="*", help="optional personality to imitate, e.g. John Madden")

    p = sub.add_parser("roast", help="smack talk the other teams (or one team)")
    p.add_argument("team", nargs="*", help="optional team name; blank = whole league")
    p.add_argument("--persona", help="optional personality to imitate")

    p = sub.add_parser("matchup", help="optimal lineup by projection and win probability (no AI call)")
    p.add_argument("--post", action="store_true", help="also post it to the Discord channel")

    sub.add_parser("whoami", help="check Sleeper user, leagues and token status")
    sub.add_parser("pending", help="list pending proposals")

    p = sub.add_parser("execute", help="execute a pending proposal on Sleeper")
    p.add_argument("id", type=int)
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    p = sub.add_parser("reject", help="reject a pending proposal")
    p.add_argument("id", type=int)

    p = sub.add_parser("lock", help="protect a roster player from being dropped or traded")
    p.add_argument("player", nargs="+", help="player name or Sleeper id")
    p = sub.add_parser("unlock", help="remove a player's lock")
    p.add_argument("player", nargs="+")
    sub.add_parser("locks", help="list locked players")
    return parser


COMMANDS = {
    "run": cmd_run,
    "analyze": cmd_analyze,
    "ask": cmd_ask,
    "snapshot": cmd_snapshot,
    "claims": cmd_claims,
    "introduce": cmd_introduce,
    "roast": cmd_roast,
    "matchup": cmd_matchup,
    "whoami": cmd_whoami,
    "pending": cmd_pending,
    "execute": cmd_execute,
    "reject": cmd_reject,
    "lock": cmd_lock,
    "unlock": cmd_unlock,
    "locks": cmd_locks,
}


async def _main(args: argparse.Namespace) -> int:
    config = Config.load()
    setup_logging(config, verbose=args.verbose)
    app = build_app(config)
    try:
        return await COMMANDS[args.command or "run"](app, args)
    finally:
        await app.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; player names are UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
