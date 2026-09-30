"""
Discord Admin Jail Bot (with timed jails)
-------------------------------------------
Admins can "jail" a member so they can only see one designated jail channel,
optionally for a set amount of time (auto-released after it passes).

SETUP
1. pip install discord.py PyNaCl   (PyNaCl is required for voice / VC features)
2. Set your bot token as an environment variable: DISCORD_BOT_TOKEN
3. Run: python jail_bot.py
4. Works immediately for anyone with "Manage Roles" or Administrator permission.
   To let a normal mod role use it too:

   !addadminrole @role        - allow that role to use jail/unjail
   !removeadminrole @role     - remove that role's permission
   !setprefix v                - change the prefix (default: !)
   !jailsetup #channel        - set which channel is used as the jail
                                (this also locks every other channel for the
                                 Jailed role, once, so jail/unjail stay fast)
   !vcjoin #voicechannel      - bot joins and stays in that VC permanently,
                                even if everyone else leaves; auto-rejoins if
                                disconnected (restart, network drop, etc.)
   !vcleave                   - bot leaves the VC and stops auto-rejoining

USAGE (once prefix is set to "v" and jail channel + admin role are set up):
   v jail @Dan 27 days spamming     -> jails Dan for 27 days, reason "spamming"
   v jail @Dan spamming             -> jails Dan with no time limit
   v jail @Dan                      -> jails Dan, no duration, no reason
   v unjail @Dan                    -> releases Dan and restores his old roles
   v role @Dan chongkids            -> toggles the "chongkids" role on Dan (plain text, no @ needed)
   vrole @Dan chongkids             -> same as above, no space needed between prefix and command

Accepted duration units: minute(s)/min/m, hour(s)/hr/h, day(s)/d, week(s)/w

Jailing a member strips ALL of their current roles and leaves only "Jailed" —
their original roles are saved and restored automatically on unjail (manual
or automatic, once the duration expires).
The bot auto-creates the "Jailed" role the first time it's needed.
You still need to designate the jail channel yourself with jailsetup.
Make sure the bot's own role is ABOVE every role you might jail someone with,
including "Jailed", in Server Settings > Roles.
"""

import os
import re
import json
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------- CONFIG ----------
JAILED_ROLE_NAME = "Jailed"
DEFAULT_PREFIX = "!"
CONFIG_PATH = "guild_config.json"
# -----------------------------

# ---------- simple per-guild config storage ----------
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


config = load_config()


def get_guild_config(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {
            "prefix": DEFAULT_PREFIX,
            "admin_roles": [],
            "jail_channel_id": None,
            "active_jails": {},  # member_id (str) -> {"release_ts": float|None, "saved_roles": [role_id,...]}
            "voice_channel_id": None,  # VC the bot should stay connected to permanently
        }
        save_config(config)
    return config[gid]


async def get_prefix(bot, message):
    if message.guild is None:
        return DEFAULT_PREFIX
    gc = get_guild_config(message.guild.id)
    p = gc["prefix"]
    # check the "prefix + space" form FIRST so "v jail" parses correctly;
    # discord.py doesn't auto-skip a space after the prefix otherwise,
    # which is why commands typed with a space were silently doing nothing.
    return commands.when_mentioned_or(f"{p} ", p)(bot, message)


# ---------- bot setup ----------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=get_prefix, intents=intents)


def is_bot_admin(member: discord.Member) -> bool:
    gc = get_guild_config(member.guild.id)
    member_role_ids = {r.id for r in member.roles}
    if member_role_ids.intersection(set(gc["admin_roles"])):
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


def is_setup_permitted(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


async def get_or_create_jailed_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=JAILED_ROLE_NAME)
    if role is None:
        role = await guild.create_role(
            name=JAILED_ROLE_NAME, reason="Auto-created jail role", color=discord.Color.dark_grey()
        )
    return role


# ---------- duration parsing ----------
DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b\s*(.*)$",
    re.IGNORECASE,
)
UNIT_SECONDS = {
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration_and_reason(rest: str):
    """Given 'Dan 27 days spamming'-style trailing text (member already stripped),
    returns (duration_seconds_or_None, reason_str)."""
    rest = rest.strip()
    if not rest:
        return None, "No reason provided"
    match = DURATION_RE.match(rest)
    if match:
        amount, unit, remainder = match.groups()
        seconds = int(amount) * UNIT_SECONDS[unit.lower()]
        reason = remainder.strip() or "No reason provided"
        return seconds, reason
    return None, rest


# ---------- lifecycle ----------

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")
    if not check_expired_jails.is_running():
        check_expired_jails.start()

    # reconnect to any VC we're supposed to be permanently in
    for gid, gc in config.items():
        vc_id = gc.get("voice_channel_id")
        if not vc_id:
            continue
        guild = bot.get_guild(int(gid))
        if guild is None:
            continue
        channel = guild.get_channel(vc_id)
        if channel and guild.voice_client is None:
            try:
                await channel.connect(reconnect=True, self_deaf=True)
            except discord.ClientException:
                pass

    print(f"Logged in as {bot.user}")


@bot.event
async def on_voice_state_update(member, before, after):
    # if the BOT itself got disconnected from its permanent VC, rejoin immediately
    if member.id != bot.user.id:
        return
    if after.channel is not None:
        return  # still connected somewhere, nothing to do

    guild = member.guild
    gc = get_guild_config(guild.id)
    vc_id = gc.get("voice_channel_id")
    if not vc_id:
        return  # bot isn't supposed to be permanently in a VC here

    channel = guild.get_channel(vc_id)
    if channel is None:
        return

    await asyncio.sleep(2)  # brief pause before reconnecting
    try:
        if guild.voice_client is None:
            await channel.connect(reconnect=True, self_deaf=True)
    except discord.ClientException:
        pass


@tasks.loop(seconds=60)
async def check_expired_jails():
    now = time.time()
    for gid, gc in config.items():
        guild = bot.get_guild(int(gid))
        if guild is None:
            continue
        jailed_role = discord.utils.get(guild.roles, name=JAILED_ROLE_NAME)
        if jailed_role is None:
            continue
        expired = [mid for mid, data in gc["active_jails"].items() if data.get("release_ts") is not None and data["release_ts"] <= now]
        for mid in expired:
            member = guild.get_member(int(mid))
            if member and jailed_role in member.roles:
                saved_ids = gc["active_jails"][mid].get("saved_roles", [])
                restored = [guild.get_role(rid) for rid in saved_ids if guild.get_role(rid)]
                try:
                    await member.edit(roles=restored, reason="Jail duration expired")
                except discord.Forbidden:
                    pass
            gc["active_jails"].pop(mid, None)
        if expired:
            save_config(config)


# ---------- setup commands ----------

@bot.hybrid_command(name="setprefix", description="Change the bot's command prefix for this server.")
@app_commands.describe(prefix="New prefix, e.g. v or ?")
async def setprefix(ctx: commands.Context, prefix: str):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return
    gc = get_guild_config(ctx.guild.id)
    gc["prefix"] = prefix
    save_config(config)
    await ctx.reply(f"Prefix set to `{prefix}`", ephemeral=True)


@bot.hybrid_command(name="addadminrole", description="Allow a role to use jail/unjail commands.")
@app_commands.describe(role="The role to grant bot-admin access")
async def addadminrole(ctx: commands.Context, role: discord.Role):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return
    gc = get_guild_config(ctx.guild.id)
    if role.id not in gc["admin_roles"]:
        gc["admin_roles"].append(role.id)
        save_config(config)
    await ctx.reply(f"**{role.name}** can now use jail/unjail.", ephemeral=True)


@bot.hybrid_command(name="removeadminrole", description="Remove a role's access to jail/unjail commands.")
@app_commands.describe(role="The role to revoke bot-admin access from")
async def removeadminrole(ctx: commands.Context, role: discord.Role):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return
    gc = get_guild_config(ctx.guild.id)
    if role.id in gc["admin_roles"]:
        gc["admin_roles"].remove(role.id)
        save_config(config)
    await ctx.reply(f"**{role.name}** can no longer use jail/unjail.", ephemeral=True)


@bot.hybrid_command(name="jailsetup", description="Set which channel is used as the jail.")
@app_commands.describe(channel="The channel jailed members will be restricted to")
async def jailsetup(ctx: commands.Context, channel: discord.TextChannel):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    guild = ctx.guild
    jailed_role = await get_or_create_jailed_role(guild)

    gc = get_guild_config(guild.id)
    gc["jail_channel_id"] = channel.id
    save_config(config)

    await channel.set_permissions(
        jailed_role, view_channel=True, send_messages=True, read_message_history=True
    )
    await channel.set_permissions(guild.default_role, view_channel=True)

    failed_channels = await lock_out_other_channels(guild, jailed_role, channel.id)

    reply_text = f"Jail channel set to {channel.mention}. All other channels are now locked for the Jailed role."
    if failed_channels:
        reply_text += f"\n⚠️ Couldn't lock these (check my permissions there): {', '.join(failed_channels)}"
    await ctx.reply(reply_text, ephemeral=True)


@bot.hybrid_command(name="vcjoin", description="Join a voice channel and stay in it permanently.")
@app_commands.describe(channel="The voice channel to join and stay in")
async def vcjoin(ctx: commands.Context, channel: discord.VoiceChannel):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    guild = ctx.guild
    try:
        if guild.voice_client is not None:
            await asyncio.wait_for(guild.voice_client.move_to(channel), timeout=15)
        else:
            await channel.connect(reconnect=True, self_deaf=True, timeout=15)
    except asyncio.TimeoutError:
        await ctx.reply(
            "Timed out connecting to the voice channel. This usually means the "
            "server hosting the bot is blocking the connection Discord voice needs "
            "(common on Railway/free hosts) — check the pinned message for details.",
            ephemeral=True,
        )
        return
    except Exception as e:
        await ctx.reply(f"Couldn't join: {e}", ephemeral=True)
        return

    gc = get_guild_config(guild.id)
    gc["voice_channel_id"] = channel.id
    save_config(config)

    await ctx.reply(f"🔊 Joined {channel.mention} and will stay there permanently, even if empty.", ephemeral=True)


@bot.hybrid_command(name="vcleave", description="Leave the voice channel and stop staying in it.")
async def vcleave(ctx: commands.Context):
    if not is_setup_permitted(ctx.author):
        await ctx.reply("You need Manage Roles or Administrator permission to do that.", ephemeral=True)
        return

    gc = get_guild_config(ctx.guild.id)
    gc["voice_channel_id"] = None
    save_config(config)

    if ctx.guild.voice_client is not None:
        await ctx.guild.voice_client.disconnect(force=True)
        await ctx.reply("🔇 Left the voice channel.", ephemeral=True)
    else:
        await ctx.reply("I wasn't in a voice channel here.", ephemeral=True)


# ---------- role assignment ----------

def find_role_by_name(guild: discord.Guild, name: str):
    name = name.strip()
    # exact match first (case-insensitive)
    role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
    if role:
        return role
    # fallback: role name contains the given text, or vice versa
    role = discord.utils.find(
        lambda r: name.lower() in r.name.lower() or r.name.lower() in name.lower(),
        guild.roles,
    )
    return role


@bot.hybrid_command(
    name="role",
    description="Give a member a role. Type the role name as plain text, no need to @ it.",
)
@app_commands.describe(member="The member to give the role to", role_name="The role name, e.g. chongkids")
async def role_cmd(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not is_bot_admin(ctx.author):
        await ctx.reply("You don't have permission to use this command.", ephemeral=True)
        return

    guild = ctx.guild
    role = find_role_by_name(guild, role_name)
    if role is None:
        await ctx.reply(f"Couldn't find a role matching `{role_name}`.", ephemeral=True)
        return

    if role >= ctx.author.top_role and ctx.author.id != guild.owner_id:
        await ctx.reply("You can't assign a role equal to or higher than your own.", ephemeral=True)
        return
    if role >= guild.me.top_role:
        await ctx.reply(f"My role needs to be above **{role.name}** for me to assign it.", ephemeral=True)
        return

    if role in member.roles:
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.reply(f"🎭 Took the **{role.name}** role away from {member.mention}.")
        return

    await member.add_roles(role, reason=f"Assigned by {ctx.author}")
    await ctx.reply(f"🎭 {member.mention} was given the **{role.name}** role.")


# ---------- jail / unjail ----------

async def lock_out_other_channels(guild: discord.Guild, jailed_role: discord.Role, jail_channel_id: int):
    failed = []
    for channel in guild.channels:
        if channel.id == jail_channel_id:
            continue
        try:
            await channel.set_permissions(jailed_role, view_channel=False)
        except discord.Forbidden:
            failed.append(channel.name)
    return failed


@bot.hybrid_command(
    name="jail",
    description="Jail a member. Optional: lead with a duration like '27 days' then a reason.",
)
@app_commands.describe(
    member="The member to jail",
    duration_and_reason="Optional, e.g. '27 days spamming' or just 'spamming'",
)
async def jail(ctx: commands.Context, member: discord.Member, *, duration_and_reason: str = ""):
    if not is_bot_admin(ctx.author):
        await ctx.reply("You don't have permission to use this command.", ephemeral=True)
        return

    guild = ctx.guild
    gc = get_guild_config(guild.id)

    if gc["jail_channel_id"] is None:
        await ctx.reply("No jail channel set yet. An admin needs to run `jailsetup #channel` first.", ephemeral=True)
        return

    jail_channel = guild.get_channel(gc["jail_channel_id"])
    if jail_channel is None:
        await ctx.reply("The saved jail channel no longer exists. Run `jailsetup` again.", ephemeral=True)
        return

    if member.top_role >= ctx.author.top_role and ctx.author.id != guild.owner_id:
        await ctx.reply("You can't jail someone with an equal or higher role than you.", ephemeral=True)
        return

    # defer immediately as a safety net in case of network latency
    await ctx.defer(ephemeral=True)

    duration_seconds, reason = parse_duration_and_reason(duration_and_reason)

    jailed_role = await get_or_create_jailed_role(guild)

    # save every role they currently have (except @everyone and roles higher than
    # or equal to the bot's own, which the bot can't touch anyway) so we can
    # restore them on unjail, then swap them down to ONLY the Jailed role.
    # This is a single API call and avoids other roles' permissions overriding
    # the Jailed role's channel restrictions.
    saved_role_ids = [r.id for r in member.roles if r.id != guild.id]
    await member.edit(roles=[jailed_role], reason=reason)

    release_ts = time.time() + duration_seconds if duration_seconds else None
    gc["active_jails"][str(member.id)] = {"release_ts": release_ts, "saved_roles": saved_role_ids}
    save_config(config)

    if duration_seconds:
        human = duration_and_reason.split(reason)[0].strip() if reason in duration_and_reason else f"{duration_seconds}s"
        duration_text = f"for {human}"
    else:
        human = None
        duration_text = "with no time limit"

    embed = discord.Embed(title="Jailed", color=discord.Color.red())
    embed.add_field(name="Server", value=guild.name, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Duration", value=human or "Indefinite", inline=True)
    if release_ts:
        embed.add_field(name="Expires", value=f"<t:{int(release_ts)}:F>", inline=True)
    embed.add_field(name="Moderator", value=str(ctx.author), inline=False)

    async def safe(coro):
        try:
            await coro
        except discord.Forbidden:
            pass

    # fire all three off at once instead of one after another, to cut down wait time
    await asyncio.gather(
        safe(member.send(embed=embed)),
        safe(ctx.send(f"{member.mention} sa oblo ka muna tangahin! 🤣")),
        safe(jail_channel.send(f"🔒 {member.mention} jailed {duration_text}. Reason: {reason}")),
    )


@bot.hybrid_command(name="unjail", description="Release a member from jail.")
@app_commands.describe(member="The member to release")
async def unjail(ctx: commands.Context, member: discord.Member):
    if not is_bot_admin(ctx.author):
        await ctx.reply("You don't have permission to use this command.", ephemeral=True)
        return

    jailed_role = discord.utils.get(ctx.guild.roles, name=JAILED_ROLE_NAME)
    if jailed_role is None or jailed_role not in member.roles:
        await ctx.reply(f"{member.mention} isn't jailed.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    gc = get_guild_config(ctx.guild.id)
    jail_data = gc["active_jails"].get(str(member.id), {})
    saved_ids = jail_data.get("saved_roles", [])
    restored = [ctx.guild.get_role(rid) for rid in saved_ids if ctx.guild.get_role(rid)]

    await member.edit(roles=restored, reason=f"Unjailed by {ctx.author}")
    gc["active_jails"].pop(str(member.id), None)
    save_config(config)

    try:
        await ctx.send(f"🔓 mag pa rehab ka tangahin!")
    except discord.Forbidden:
        pass

    await ctx.reply(f"🔓 {member.mention} has been released.", ephemeral=True)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before running.")
    bot.run(token)
