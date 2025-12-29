"""
SZABot - Discord bot for managing Fortnite ZoneWars matches.

This bot provides commands to create teams for ZoneWars matches (2v2, 3v3 or 4v4),
optionally using random assignment, a simple captained draft, or manual team
assignment. After teams are created the bot can trade players between teams,
move players into temporary voice channels for each team, pause/resume the
match (moving everyone back to a central lobby channel or returning them to
their team channels) and end the match. When a match ends the bot awards
BSNBucks ($BSN) to each player on the winning team and deducts BSNBucks from
players on the losing team. The bot maintains BSNBucks balances in a JSON
file on disk.

To use this bot you must create a Discord application and bot account,
enable the appropriate intents (Guild Members and Voice States) in the
Discord developer portal and invite the bot to your server with the
applications.commands and bot scopes. You should set the bot token as
the environment variable DISCORD_BOT_TOKEN before running this script.

This code depends on the ``discord.py`` library version 2.0 or higher.
You can install it with ``pip install -U discord.py``.
"""

import os
import json
import random
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands


def load_bsn_data(path: str) -> Dict[int, int]:
    """Load BSNBucks data from a JSON file.

    The data is stored as a mapping from Discord user ID (int) to their
    BSNBucks balance (int). If the file does not exist it returns an
    empty dictionary.

    Parameters
    ----------
    path: str
        The path to the JSON file.

    Returns
    -------
    Dict[int, int]
        A dictionary mapping user IDs to BSNBucks balances.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Convert keys back to int
        return {int(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


def save_bsn_data(path: str, data: Dict[int, int]) -> None:
    """Save BSNBucks data to a JSON file.

    The data is stored as a mapping from Discord user ID (int) to their
    BSNBucks balance (int). Keys are converted to strings because JSON
    requires string keys.

    Parameters
    ----------
    path: str
        The path to the JSON file.
    data: Dict[int, int]
        The mapping from user IDs to BSNBucks balances.
    """
    try:
        to_dump = {str(k): v for k, v in data.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(to_dump, f, indent=2)
    except Exception:
        # If saving fails we silently ignore to avoid crashing the bot
        pass


@dataclass
class GameSession:
    """Represent an active ZoneWars game session.

    Attributes
    ----------
    guild_id : int
        The ID of the guild (server) where the game is running.
    teams : List[List[discord.Member]]
        A list containing exactly two lists of players for the two teams.
    team_channels : List[discord.VoiceChannel]
        A list of two voice channels corresponding to the two teams.
    central_channel : discord.VoiceChannel
        The shared lobby voice channel used when pausing the game.
    category : discord.CategoryChannel
        The category under which temporary voice channels were created.
    original_channels : Dict[int, Optional[int]]
        A mapping from user ID to the ID of the voice channel they were
        originally in before joining the game (if any). This allows the
        bot to return users to their original channels when the game ends.
    paused_locations : Dict[int, int]
        A mapping from user ID to the ID of the voice channel they were
        in before the game was paused. Used to restore users on resume.
    paused : bool
        Indicates whether the game is currently paused.
    """

    guild_id: int
    teams: List[List[discord.Member]]
    team_channels: List[discord.VoiceChannel]
    central_channel: discord.VoiceChannel
    category: discord.CategoryChannel
    original_channels: Dict[int, Optional[int]] = field(default_factory=dict)
    paused_locations: Dict[int, int] = field(default_factory=dict)
    paused: bool = False

    async def move_players_to_team_channels(self) -> None:
        """Move players into their team voice channels.

        For each player in each team, this function moves the player into
        the corresponding team voice channel. It records the player's
        original voice channel if not already recorded.
        """
        for team_index, team in enumerate(self.teams):
            channel = self.team_channels[team_index]
            for member in team:
                try:
                    # Record original channel if not already stored
                    if member.id not in self.original_channels:
                        if member.voice:
                            self.original_channels[member.id] = member.voice.channel.id
                        else:
                            self.original_channels[member.id] = None
                    if member.voice is None or member.voice.channel.id != channel.id:
                        await member.move_to(channel)
                except Exception:
                    # Ignore move failures silently
                    continue

    async def move_all_to_central(self) -> None:
        """Move all players to the central lobby channel.

        This is used when the game is paused. It records where each player
        came from so that they can be returned to their team channels on
        resume.
        """
        for team_index, team in enumerate(self.teams):
            for member in team:
                try:
                    # Record the channel where they were before moving
                    if member.voice:
                        self.paused_locations[member.id] = member.voice.channel.id
                    await member.move_to(self.central_channel)
                except Exception:
                    continue

    async def move_back_from_central(self) -> None:
        """Move players back to their team voice channels after a pause.

        This function uses the stored paused_locations to restore players to
        their team channels. If for some reason the location is missing, it
        defaults to moving them into their team's voice channel again.
        """
        for team_index, team in enumerate(self.teams):
            channel = self.team_channels[team_index]
            for member in team:
                try:
                    if member.id in self.paused_locations:
                        target_id = self.paused_locations[member.id]
                        # If the saved location corresponds to one of the team channels
                        # we move them there; otherwise default to the team channel
                        target_channel = None
                        if target_id == channel.id:
                            target_channel = channel
                        else:
                            # Attempt to fetch the channel
                            target_channel = member.guild.get_channel(target_id)
                        if target_channel is None:
                            target_channel = channel
                        await member.move_to(target_channel)
                    else:
                        await member.move_to(channel)
                except Exception:
                    continue
        self.paused_locations.clear()

    async def teardown(self) -> None:
        """Clean up temporary channels and category when the game ends.

        Deletes the team and central voice channels and the category that
        contains them.
        """
        # Delete team channels
        for ch in self.team_channels:
            try:
                await ch.delete(reason="ZoneWars match ended")
            except Exception:
                continue
        # Delete central channel
        try:
            await self.central_channel.delete(reason="ZoneWars match ended")
        except Exception:
            pass
        # Delete category
        try:
            await self.category.delete(reason="ZoneWars match ended")
        except Exception:
            pass


class SZABot(commands.Bot):
    """Discord bot to manage Fortnite ZoneWars matches."""

    def __init__(self, *, command_prefix: str = "!", bsn_file: str = "bsn_data.json"):
        intents = discord.Intents.default()
        # We need member and voice state intents to move users
        intents.members = True
        intents.voice_states = True
        # We also need guilds for slash commands
        intents.guilds = True
        super().__init__(command_prefix=command_prefix, intents=intents)
        # The JSON file path for BSNBucks
        self.bsn_file = bsn_file
        # In-memory BSNBucks balances; key: user ID, value: int
        self.bsn_balances: Dict[int, int] = load_bsn_data(self.bsn_file)
        # Active game sessions keyed by guild ID
        self.active_games: Dict[int, GameSession] = {}
        # Sync application commands on ready
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        """Called on bot startup to sync slash commands."""
        # Register all app commands on the command tree before syncing
        self.tree.add_command(self.setup_command)
        self.tree.add_command(self.help_command)
        self.tree.add_command(self.bsn_balance)
        self.tree.add_command(self.bsn_leaderboard)
        self.tree.add_command(self.create_teams)
        self.tree.add_command(self.trade_players)
        self.tree.add_command(self.pause_game)
        self.tree.add_command(self.resume_game)
        self.tree.add_command(self.end_game)
        # Sync commands with all guilds
        await self.tree.sync()

    def add_bsn(self, member: discord.Member, amount: int) -> None:
        """Adjust a member's BSNBucks balance by a given amount.

        Parameters
        ----------
        member: discord.Member
            The member whose balance will be adjusted.
        amount: int
            The amount to add (positive) or subtract (negative).
        """
        current = self.bsn_balances.get(member.id, 0)
        self.bsn_balances[member.id] = current + amount
        # Persist the updated balances
        save_bsn_data(self.bsn_file, self.bsn_balances)

    # --------------- Command definitions ---------------

    @app_commands.command(name="setup", description="Check if the bot has the necessary permissions to operate")
    async def setup_command(self, interaction: discord.Interaction) -> None:
        """Slash command to verify the bot's permissions in the server.

        This command inspects the bot's guild-level permissions and reports
        whether the bot has the permissions required to create and manage
        channels and move members between voice channels. If any required
        permission is missing the response will indicate which ones need
        adjusting. This helps server administrators configure the bot
        correctly after inviting it to a server.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild.",
                                            ephemeral=True)
            return
        me = guild.me
        perms = me.guild_permissions
        # Define the permissions we care about
        required = {
            "manage_channels": perms.manage_channels,
            "move_members": perms.move_members,
            "connect": perms.connect,
            "view_channel": perms.view_channel,
        }
        missing = [name for name, has in required.items() if not has]
        if not missing:
            message = (
                "All required permissions are present!\n"
                "The bot should operate correctly in this server."
            )
        else:
            pretty = ", ".join(missing)
            message = (
                "The bot is missing some required permissions and may not function properly.\n"
                f"Missing permissions: {pretty}.\n"
                "Please grant these permissions to the bot's role."
            )
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="help", description="Show help information for SZABot")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Provide usage instructions for all available SZABot commands.

        This command sends a summary of the bot's features and the syntax for
        each slash command. It is delivered as an embedded message to make
        it easy for users to discover how to use the bot.
        """
        await interaction.response.defer(ephemeral=True)
        # Prepare an embed with command descriptions
        embed = discord.Embed(title="SZABot Help", colour=discord.Colour.blue())
        embed.add_field(
            name="/create_teams",
            value=("Create a ZoneWars match and assign players to two teams.\n"
                   "**mode**: `random`, `draft`, or `manual`.\n"
                   "**team_size**: 2, 3 or 4.\n"
                   "**players**: comma‑separated list of player mentions or IDs (required for random/draft).\n"
                   "**captains**: two player mentions/IDs (required for draft).\n"
                   "**team1/team2**: comma‑separated player lists (required for manual)."),
            inline=False
        )
        embed.add_field(
            name="/trade_players",
            value=("Swap two players between teams in the active match.\n"
                   "Specify a player from Team 1 and a player from Team 2."),
            inline=False
        )
        embed.add_field(
            name="/pause_game & /resume_game",
            value=("Pause moves everyone to a lobby channel.\n"
                   "Resume returns players to their team voice channels."),
            inline=False
        )
        embed.add_field(
            name="/end_game",
            value=("End the current match and award/penalize 10 $BSN to the winning/losing team.\n"
                   "Specify which team won (1 or 2)."),
            inline=False
        )
        embed.add_field(
            name="/bsn_balance",
            value=("Display your BSNBucks balance or someone else's.\n"
                   "Provide a member if you want to check another player's balance."),
            inline=False
        )
        embed.add_field(
            name="/bsn_leaderboard",
            value=("Show the top players based on their BSNBucks balances."),
            inline=False
        )
        embed.add_field(
            name="/setup",
            value=("Check whether the bot has the required permissions to operate in this server."),
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="bsn_balance", description="Show a user's BSNBucks balance")
    @app_commands.describe(member="The member whose BSNBucks balance you want to view (optional)")
    async def bsn_balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        """Display the BSNBucks balance for the invoking user or a specified user.

        Parameters
        ----------
        member: discord.Member, optional
            The member whose BSNBucks balance to display. If omitted, the
            invoking user's balance is shown.
        """
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        balance = self.bsn_balances.get(target.id, 0)
        await interaction.followup.send(
            f"{target.display_name} has {balance} $BSN.",
            ephemeral=True
        )

    @app_commands.command(name="bsn_leaderboard", description="Display the BSNBucks leaderboard")
    async def bsn_leaderboard(self, interaction: discord.Interaction) -> None:
        """Show a leaderboard of the top BSNBucks holders.

        The leaderboard displays up to the top 10 players sorted by their
        BSNBucks balances in descending order. If a user is no longer in the
        server their name will show as their user ID.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        # Sort balances by value descending
        sorted_balances: List[Tuple[int, int]] = sorted(
            self.bsn_balances.items(), key=lambda item: item[1], reverse=True
        )
        # Build a leaderboard string
        lines: List[str] = []
        rank = 1
        for user_id, amount in sorted_balances[:10]:
            name = str(user_id)
            if guild:
                member = guild.get_member(user_id)
                if member:
                    name = member.display_name
            lines.append(f"{rank}. {name} – {amount} $BSN")
            rank += 1
        if not lines:
            lines.append("No BSNBucks have been recorded yet.")
        leaderboard_text = "\n".join(lines)
        await interaction.followup.send(
            f"**BSNBucks Leaderboard**\n{leaderboard_text}",
            ephemeral=True
        )

    @app_commands.command(name="create_teams", description="Create ZoneWars teams (2v2, 3v3 or 4v4)")
    @app_commands.describe(
        mode="Team creation mode: random, draft or manual",
        team_size="Number of players per team (2, 3 or 4)",
        players="Comma separated list of players to include (mention or ID)",
        captains="For draft mode: comma separated list of exactly two captains (mention or ID)",
        team1="For manual mode: comma separated list of players for Team 1 (mention or ID)",
        team2="For manual mode: comma separated list of players for Team 2 (mention or ID)"
    )
    async def create_teams(self, interaction: discord.Interaction, mode: str, team_size: int, players: str = None, captains: str = None, team1: str = None, team2: str = None) -> None:
        """Slash command to create ZoneWars teams.

        Parameters
        ----------
        interaction: discord.Interaction
            The interaction that triggered the command.
        mode: str
            The team creation mode: 'random', 'draft' or 'manual'.
        team_size: int
            The size of each team (2, 3 or 4).
        players: str, optional
            Comma separated list of players participating in the match. Each
            entry may be a mention (e.g. ``<@123456789>``) or a numeric ID.
            Required for 'random' and 'draft' modes.
        captains: str, optional
            Comma separated list of exactly two players to serve as team
            captains for draft mode. Each entry may be a mention or ID.
        team1: str, optional
            Comma separated list of players for Team 1 in manual mode.
        team2: str, optional
            Comma separated list of players for Team 2 in manual mode.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild (server).", ephemeral=True)
            return

        # Check for an existing active game
        if guild.id in self.active_games:
            await interaction.followup.send("A game is already active in this server. End it before starting a new one.", ephemeral=True)
            return

        # Validate team size
        if team_size not in (2, 3, 4):
            await interaction.followup.send("Invalid team size. Please choose 2, 3, or 4 players per team.", ephemeral=True)
            return

        # Parse players from the input string into Member objects
        def parse_members(input_str: str) -> List[discord.Member]:
            members: List[discord.Member] = []
            if not input_str:
                return members
            parts = [p.strip() for p in input_str.split(',') if p.strip()]
            for part in parts:
                # Remove mention formatting if present
                # e.g. <@!123456789> or <@123456789>
                if part.startswith('<@') and part.endswith('>'):
                    part = part[2:-1]
                    # Remove exclamation if present
                    if part.startswith('!'):
                        part = part[1:]
                # Now part should be an ID
                try:
                    uid = int(part)
                except ValueError:
                    continue
                member = guild.get_member(uid)
                if member is not None:
                    members.append(member)
            return members

        mode_lower = mode.lower()
        all_players: List[discord.Member] = []
        team_a: List[discord.Member] = []
        team_b: List[discord.Member] = []

        # Random and draft modes require a list of players
        if mode_lower in ("random", "draft"):
            if not players:
                await interaction.followup.send("You must specify the players participating in the match (comma separated).", ephemeral=True)
                return
            all_players = parse_members(players)
            expected_count = team_size * 2
            if len(all_players) != expected_count:
                await interaction.followup.send(f"Exactly {expected_count} players are required for a {team_size}v{team_size} match.",
                                                ephemeral=True)
                return
        # Manual mode requires specific teams
        elif mode_lower == "manual":
            if not team1 or not team2:
                await interaction.followup.send("For manual mode you must specify players for both teams.",
                                                ephemeral=True)
                return
            team_a = parse_members(team1)
            team_b = parse_members(team2)
            if len(team_a) != team_size or len(team_b) != team_size:
                await interaction.followup.send(f"Each team must have exactly {team_size} players.",
                                                ephemeral=True)
                return
            # Check for duplicate players between teams
            duplicate_ids = {m.id for m in team_a}.intersection({m.id for m in team_b})
            if duplicate_ids:
                await interaction.followup.send("A player cannot be on both teams.",
                                                ephemeral=True)
                return
            all_players = team_a + team_b
        else:
            await interaction.followup.send("Invalid mode. Please choose 'random', 'draft', or 'manual'.",
                                            ephemeral=True)
            return

        # Draft mode requires captains
        if mode_lower == "draft":
            if not captains:
                await interaction.followup.send("You must specify exactly two captains (comma separated) for draft mode.",
                                                ephemeral=True)
                return
            captain_members = parse_members(captains)
            # Ensure exactly two captains
            if len(captain_members) != 2:
                await interaction.followup.send("Exactly two captains must be specified.",
                                                ephemeral=True)
                return
            # Ensure the captains are part of the player list
            for cap in captain_members:
                if cap not in all_players:
                    await interaction.followup.send("Captains must be among the listed players.",
                                                    ephemeral=True)
                    return
            # Remove captains from player pool
            remaining_players = [m for m in all_players if m not in captain_members]
            # Assign each captain to its own team
            team_a = [captain_members[0]]
            team_b = [captain_members[1]]
            # Randomly distribute the remaining players to teams until each has team_size
            random.shuffle(remaining_players)
            while len(team_a) < team_size and remaining_players:
                team_a.append(remaining_players.pop())
            while len(team_b) < team_size and remaining_players:
                team_b.append(remaining_players.pop())
            # If after distribution teams are imbalanced (shouldn't happen) fill in
            for m in remaining_players:
                if len(team_a) < team_size:
                    team_a.append(m)
                elif len(team_b) < team_size:
                    team_b.append(m)
        # Random mode simply shuffles and divides players
        elif mode_lower == "random":
            random.shuffle(all_players)
            team_a = all_players[:team_size]
            team_b = all_players[team_size:]
        # Manual handled above

        # At this point team_a and team_b should each have exactly team_size members
        if len(team_a) != team_size or len(team_b) != team_size:
            await interaction.followup.send("Internal error creating teams. Please try again.",
                                            ephemeral=True)
            return

        # Create a new category to contain team voice channels
        category_name = f"ZoneWars Match"
        try:
            category = await guild.create_category(name=category_name, reason="Creating ZoneWars match")
        except Exception as e:
            await interaction.followup.send(f"Failed to create category: {e}",
                                            ephemeral=True)
            return

        # Create central lobby voice channel
        try:
            central_vc = await guild.create_voice_channel(
                name="Lobby",
                category=category,
                reason="Central lobby for ZoneWars match"
            )
        except Exception as e:
            await category.delete(reason="Cleanup after failure to create lobby")
            await interaction.followup.send(f"Failed to create lobby: {e}",
                                            ephemeral=True)
            return

        team_channels: List[discord.VoiceChannel] = []
        # Create two team voice channels
        for i in range(2):
            try:
                vc = await guild.create_voice_channel(
                    name=f"Team {i+1}",
                    category=category,
                    reason="Team voice channel for ZoneWars match"
                )
                team_channels.append(vc)
            except Exception as e:
                # Cleanup partially created channels and category
                for ch in team_channels:
                    try:
                        await ch.delete(reason="Cleanup after failure to create team channels")
                    except Exception:
                        pass
                try:
                    await central_vc.delete(reason="Cleanup after failure to create team channels")
                except Exception:
                    pass
                await category.delete(reason="Cleanup after failure to create team channels")
                await interaction.followup.send(f"Failed to create team channels: {e}",
                                                ephemeral=True)
                return

        # Store game session data
        session = GameSession(
            guild_id=guild.id,
            teams=[team_a, team_b],
            team_channels=team_channels,
            central_channel=central_vc,
            category=category
        )
        self.active_games[guild.id] = session

        # Move players into their team voice channels
        await session.move_players_to_team_channels()

        # Send feedback to the user summarizing team composition and voice channel info
        team_a_names = ", ".join([member.display_name for member in team_a])
        team_b_names = ", ".join([member.display_name for member in team_b])
        response = (
            f"ZoneWars match created!\n"
            f"Team 1 ({team_size} players): {team_a_names}\n"
            f"Team 2 ({team_size} players): {team_b_names}\n"
            f"Players have been moved to their respective team voice channels."
        )
        await interaction.followup.send(response, ephemeral=True)

    # ---------------------------------------------------------------------

    @app_commands.command(name="trade_players", description="Trade players between teams in the active match")
    @app_commands.describe(member_a="Player from Team 1", member_b="Player from Team 2")
    async def trade_players(self, interaction: discord.Interaction, member_a: discord.Member, member_b: discord.Member) -> None:
        """Swap two players between teams.

        Parameters
        ----------
        interaction: discord.Interaction
            The interaction that triggered the command.
        member_a: discord.Member
            A player on Team 1 that you wish to trade.
        member_b: discord.Member
            A player on Team 2 that you wish to trade.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild.",
                                            ephemeral=True)
            return
        session = self.active_games.get(guild.id)
        if session is None:
            await interaction.followup.send("There is no active game in this server.",
                                            ephemeral=True)
            return
        # Ensure both players are part of the game
        team1 = session.teams[0]
        team2 = session.teams[1]
        if member_a not in team1 or member_b not in team2:
            await interaction.followup.send("One or both of the specified players are not on the expected teams.",
                                            ephemeral=True)
            return
        # Perform the swap
        team1.remove(member_a)
        team1.append(member_b)
        team2.remove(member_b)
        team2.append(member_a)
        # Update voice channel assignments if currently not paused
        try:
            if not session.paused:
                # Move member_a (now on team2) to Team2 channel
                await member_a.move_to(session.team_channels[1])
                # Move member_b (now on team1) to Team1 channel
                await member_b.move_to(session.team_channels[0])
        except Exception:
            # Move errors ignored
            pass
        await interaction.followup.send(
            f"Traded {member_a.display_name} and {member_b.display_name} between teams.",
            ephemeral=True
        )

    # ---------------------------------------------------------------------

    @app_commands.command(name="pause_game", description="Pause the active ZoneWars match and move everyone to the lobby")
    async def pause_game(self, interaction: discord.Interaction) -> None:
        """Pause the current match.

        Moves all players from their team channels to the central lobby channel
        and marks the game as paused. If the game is already paused this
        command has no effect.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild.",
                                            ephemeral=True)
            return
        session = self.active_games.get(guild.id)
        if session is None:
            await interaction.followup.send("There is no active game to pause.",
                                            ephemeral=True)
            return
        if session.paused:
            await interaction.followup.send("The game is already paused.",
                                            ephemeral=True)
            return
        # Move everyone to the central channel
        await session.move_all_to_central()
        session.paused = True
        await interaction.followup.send("Game paused. All players have been moved to the lobby.",
                                        ephemeral=True)

    # ---------------------------------------------------------------------

    @app_commands.command(name="resume_game", description="Resume the paused ZoneWars match and return players to their teams")
    async def resume_game(self, interaction: discord.Interaction) -> None:
        """Resume a paused match.

        Moves all players back to the voice channels they were in prior to
        pausing. If the game is not paused this command has no effect.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild.",
                                            ephemeral=True)
            return
        session = self.active_games.get(guild.id)
        if session is None:
            await interaction.followup.send("There is no active game to resume.",
                                            ephemeral=True)
            return
        if not session.paused:
            await interaction.followup.send("The game is not paused.",
                                            ephemeral=True)
            return
        # Move players back to their teams
        await session.move_back_from_central()
        session.paused = False
        await interaction.followup.send("Game resumed. Players have been returned to their teams.",
                                        ephemeral=True)

    # ---------------------------------------------------------------------

    @app_commands.command(name="end_game", description="End the current ZoneWars match and award BSNBucks")
    @app_commands.describe(winning_team="Specify the winning team: 1 or 2")
    async def end_game(self, interaction: discord.Interaction, winning_team: int) -> None:
        """End the active match and award/penalize BSNBucks.

        Parameters
        ----------
        interaction: discord.Interaction
            The interaction that triggered the command.
        winning_team: int
            The number of the team that won (1 or 2).

        After updating the BSNBucks balances the bot cleans up all
        temporary channels and deletes the game session.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a guild.",
                                            ephemeral=True)
            return
        session = self.active_games.get(guild.id)
        if session is None:
            await interaction.followup.send("There is no active game to end.",
                                            ephemeral=True)
            return
        # Validate winning_team
        if winning_team not in (1, 2):
            await interaction.followup.send("Winning team must be 1 or 2.",
                                            ephemeral=True)
            return
        # Determine winners and losers
        win_index = winning_team - 1
        winners = session.teams[win_index]
        losers = session.teams[1 - win_index]
        # Award winners and penalize losers
        for member in winners:
            self.add_bsn(member, 10)
        for member in losers:
            self.add_bsn(member, -10)
        # If paused, move players back to lobby before end
        if session.paused:
            try:
                await session.move_back_from_central()
            except Exception:
                pass
        # Return players to their original channels if recorded
        for team in session.teams:
            for member in team:
                try:
                    orig = session.original_channels.get(member.id)
                    if orig is not None:
                        original_channel = guild.get_channel(orig)
                        if original_channel:
                            await member.move_to(original_channel)
                        else:
                            # If original channel disappeared just disconnect them
                            await member.move_to(None)
                    else:
                        # If there was no original channel we disconnect them
                        await member.move_to(None)
                except Exception:
                    continue
        # Clean up temporary channels and category
        await session.teardown()
        # Remove the session
        del self.active_games[guild.id]
        # Summarize results
        winner_names = ", ".join([m.display_name for m in winners])
        loser_names = ", ".join([m.display_name for m in losers])
        response = (
            f"Match ended. Team {winning_team} won!\n"
            f"Winners (+10 $BSN each): {winner_names}\n"
            f"Losers (-10 $BSN each): {loser_names}\n"
            f"All temporary channels have been deleted."
        )
        # Send the result to the user. Make the message ephemeral to avoid spamming the channel.
        await interaction.followup.send(response, ephemeral=True)


def main() -> None:
    """Entry point for running the bot.

    Retrieves the Discord bot token from the DISCORD_BOT_TOKEN environment
    variable and starts the bot. If the token is missing the function
    prints an error message.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set.")
    bot = SZABot(command_prefix="!", bsn_file="bsn_data.json")
    bot.run(token)


if __name__ == "__main__":
    main()
