# IV2Video Discord Bot
# A clean rewrite of the bot with improved readability and documentation.

import os
import sys
import time
import random
import discord
import requests
import asyncio
import logging
import threading
from hashlib import sha256
from pyjson5 import load as json_load
from combiner import combiner
from editor.download import download
from collections import namedtuple, defaultdict
from func_helper import *
from functools import reduce
from operator import add
from editor import editor
from math import ceil
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from datetime import datetime
from discord import app_commands
import math

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger_handler = logging.StreamHandler(sys.stdout)
logger_handler.setLevel(logging.DEBUG)
logger.addHandler(logger_handler)
info = lambda *args: logger.info(' | '.join(map(str, args)))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

config = json_load(open("config.json", 'r'))

FILE_SIZE_LIMIT_MB = 10  # File size limit in megabytes

message_search_count      = config["message_search_count"]
command_chain_limit       = config["command_chain_limit"]
working_directory         = os.path.realpath(config["working_directory"])
response_messages         = config["response_messages"]
max_concat_count          = config["max_concat_count"]
discord_token             = config["discord_token"]
meta_prefixes             = config["meta_prefixes"]
cookie_file               = config.setdefault("cookie_file")

disable_donor_check              = config.setdefault("disable_donor_check")
disable_guild_owner_check        = config.setdefault("disable_guild_owner_check")
donor_guild_id                   = config.setdefault("donor_guild_id")
donor_teir_roles                 = config.setdefault("donor_teir_roles")
donor_guild_check_seconds        = config.setdefault("donor_guild_check_seconds")
disable_guild_owner_author_check = config.setdefault("disable_guild_owner_author_check")

valid_video_extensions = ("mp4", "webm", "avi", "mkv", "mov")
valid_image_extensions = ("png", "gif", "jpg", "jpeg")
valid_extensions       = valid_video_extensions + valid_image_extensions

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
hash_str      = lambda s: str(sha256(s.encode()).digest().hex())[:32]
hash_filename = lambda s: f"{hash_str((q := os.path.splitext(s))[0])}{q[1]}"

get_default = lambda v, d=config["unspecified_default_timeout"]: v["default"] if "default" in v else d


def config_timeout(default, custom):
    """Build a nested defaultdict for timeout durations from config dicts."""
    default_timeout = get_default(default)
    return defaultdict(
        lambda: defaultdict(lambda: default_timeout, **default),
        **{
            k: defaultdict(lambda: get_default(v, default_timeout), **v)
            for k, v in custom.items()
        }
    )


guild_timeout_durations = config_timeout(config["default_guild_timeouts"], config["custom_guild_timeouts"])
user_timeout_durations  = config_timeout(config["default_user_timeouts"],  config["custom_user_timeouts"])
guild_timeouts = defaultdict(lambda: 0)
user_timeouts  = defaultdict(lambda: 0)

# Named tuples used throughout the bot
qued_msg = namedtuple("qued_msg", "context message filepath filename reply edit", defaults=6 * [None])
result   = namedtuple("result",   "success filename message",                    defaults=3 * [None])

async_runner = Async_handler()
taskList, messageQue = [], []

# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
intents          = discord.Intents.all()
intents.typing   = False
intents.presences = False
discord_status   = discord.Activity(type=discord.ActivityType.playing, name="iv2video.com")
bot              = discord.AutoShardedClient(status=discord_status, intents=intents, chunk_guilds_at_startup=False)

# ---------------------------------------------------------------------------
# Command counter / log
# ---------------------------------------------------------------------------

COMMAND_COUNT_FILE = "CommandCount.dat"
COMMAND_LOG_FILE   = "CommandHistory.dat"

if os.path.exists(COMMAND_COUNT_FILE):
    with open(COMMAND_COUNT_FILE, "r") as f:
        try:
            command_count = int(f.read().strip())
        except ValueError:
            command_count = 0
else:
    command_count = 0

start_time = time.time()


def get_uptime():
    """Return a human-readable string showing how long the bot has been running."""
    seconds = int(time.time() - start_time)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def get_next_milestone(count):
    """Return the next multiple-of-1000 milestone above *count*."""
    return ((count // 1000) + 1) * 1000


def is_milestone(count):
    """Return True when *count* is a positive multiple of 1000."""
    return count % 1000 == 0 and count > 0


# ---------------------------------------------------------------------------
# Target grouping
# ---------------------------------------------------------------------------

class target_group:
    """Holds attachment lists from three sources and deduplicates on compile."""

    def __init__(self, attachments, reply, channel):
        self.attachments = attachments
        self.reply       = reply
        self.channel     = channel

    def compile(self):
        """Return a deduplicated, ordered list of all attachments."""
        k = []
        [k.append(i) for i in (self.attachments + self.reply + self.channel) if i not in k]
        return k


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def human_size(size, units="B|KB|MB|GB|TB|PB|EB".split('|')):
    """Convert a byte count to a human-readable string."""
    return str(size) + units[0] if size < 1024 else human_size(size >> 10, units[1:])


def generate_uuid_from_msg(msg_id):
    return f"{msg_id}_{(time.time_ns() // 100) % 1000000}"


def generate_uuid_folder_from_msg(msg_id):
    return f"{working_directory}/{generate_uuid_from_msg(msg_id)}"


def clean_message(msg):
    """Strip left-to-right marks and sanitise @ mentions."""
    return msg.replace(chr(8206), '').replace('@', '@' + chr(8206))


# ---------------------------------------------------------------------------
# Timeout helpers
# ---------------------------------------------------------------------------

def apply_timeouts(msg, command,
        guild_timeout_durations=guild_timeout_durations,
        user_timeout_durations=user_timeout_durations,
        guild_timeouts=guild_timeouts,
        user_timeouts=user_timeouts):
    """
    Check and apply cooldowns for a regular (on_message) command.
        
    Returns True if the command may proceed, or a float representing the
    remaining cooldown in seconds.
    """
    ahr_id = str(msg.author.id)
    
    if disable_guild_owner_check:
        gld_id = '0'
    else:
        try:
            gld_id = str(msg.guild.id)
        except AttributeError:
            gld_id = '0'
            print(f"Error acquiring guild ID for author ID {ahr_id}")
    
    if disable_guild_owner_author_check:
        gld_own_id = '0'
    else:
        try:
            gld_own_id = '0' if gld_id == '0' else str(msg.guild.owner.id)
        except AttributeError:
            gld_own_id = '0'
            print(f"Error acquiring owner ID for guild ID {gld_id}")
    
    if "ghost" in user_timeout_durations[ahr_id] or "ghost" in guild_timeout_durations[gld_id]:
        return True
    
    gt, ut = guild_timeouts[gld_id], user_timeouts[ahr_id]
    is_donor_user  = "donor" in user_timeout_durations[ahr_id]
    is_donor_guild = "donor" in user_timeout_durations[gld_own_id]
    
    ct = time.time()
    if not is_donor_user and ct < gt:
        return gt - ct
    if ct < ut:
        return ut - ct
    
    user_timeouts[ahr_id] = ct + user_timeout_durations[ahr_id][command] * (
        user_timeout_durations[ahr_id]["user_timeout_multiplier"] if is_donor_user else 1)
    if not is_donor_user:
        guild_timeouts[gld_id] = ct + guild_timeout_durations[gld_id][command] * (
            user_timeout_durations[gld_own_id]["guild_timeout_multiplier"] if is_donor_guild else 1)
    
    return True


def apply_timeouts2(msg, command,
        guild_timeout_durations=guild_timeout_durations,
        user_timeout_durations=user_timeout_durations,
        guild_timeouts=guild_timeouts,
        user_timeouts=user_timeouts):
    """
    Check and apply cooldowns for a slash-command interaction.
        
    Mirrors apply_timeouts but reads from msg.user instead of msg.author,
    and always treats the guild as '0' (DM-safe).
    """
    ahr_id    = str(msg.user.id)
    gld_id    = '0'
    gld_own_id = '0'
    
    if "ghost" in user_timeout_durations[ahr_id] or "ghost" in guild_timeout_durations[gld_id]:
        return True
    
    gt, ut = guild_timeouts[gld_id], user_timeouts[ahr_id]
    is_donor_user  = "donor" in user_timeout_durations[ahr_id]
    is_donor_guild = "donor" in user_timeout_durations[gld_own_id]
    
    ct = time.time()
    if not is_donor_user and ct < gt:
        return gt - ct
    if ct < ut:
        return ut - ct
    
    user_timeouts[ahr_id] = ct + user_timeout_durations[ahr_id][command] * (
        user_timeout_durations[ahr_id]["user_timeout_multiplier"] if is_donor_user else 1)
    if not is_donor_user:
        guild_timeouts[gld_id] = ct + guild_timeout_durations[gld_id][command] * (
            user_timeout_durations[gld_own_id]["guild_timeout_multiplier"] if is_donor_guild else 1)
    
    return True


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def check_donors():
    """Periodically sync the slash-command tree (and refresh donor data when implemented)."""
    global guild_timeout_durations, user_timeout_durations
    
    while True:
        await tree.sync()
        info("🔁 Syncing command tree")
        await asyncio.sleep(donor_guild_check_seconds)
async def processQue():
    """Drain the messageQue, sending each queued message in order."""
    while True:
        if not len(messageQue):
            await asyncio.sleep(1)
            continue
        
        res = messageQue.pop(0)
        
        try:
            action = res.context.reply if res.reply else (
                res.context.edit if res.edit else res.context.channel.send)

            if res.filepath:
                filesize = os.path.getsize(res.filepath)
                if filesize >= FILE_SIZE_LIMIT_MB * 1024 ** 2:
                    await action(
                        f":x: Sorry, but the resulting file ({human_size(filesize)}) "
                        f"is over the {FILE_SIZE_LIMIT_MB}MB file size limit."
                    )
                        else:
                    with open(res.filepath, 'rb') as f:
                        args        = [res.message] if res.message else []
                        file_kwargs = {"filename": res.filename} if res.filename else {}
                        # If the triggering message was a bot-issued prefix command,
                        # delete it first then resend in the channel so the thread stays clean.
                        if (res.message
                                and res.context.content.startswith('!')
                                and action == res.context.reply
                                and res.context.author.id == bot.user.id):
                            await res.context.delete()
                            await asyncio.sleep(1)
                            await res.context.channel.send(*args, file=discord.File(f, **file_kwargs))
        else:
                            await action(*args, file=discord.File(f, **file_kwargs))
    else:
                await action(res.message)

        except Exception as err:
            print(f'Unable to post a message, "{err}"')
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Attachment-gathering helpers
# ---------------------------------------------------------------------------

async def get_targets(msg, attachments=True, reply=True, channel=True,
                      message_search_count=8, stop_on_first=True):
    """Collect attachments from the message itself, its reply, or the channel history."""
    msg_attachments, msg_reply, msg_channel = [], [], []

    async def do_setters():
        nonlocal msg_attachments, msg_reply, msg_channel
        if attachments:
            msg_attachments = msg.attachments
        if stop_on_first and msg_attachments:
        return

        if reply and msg.reference:
            fetched = await msg.channel.fetch_message(msg.reference.message_id)
            msg_reply = fetched.attachments
        if stop_on_first and msg_reply:
        return
    
        if channel and message_search_count > 0:
            msg_channel = reduce(
                add, [i.attachments async for i in msg.channel.history(limit=message_search_count)])

    await do_setters()
    return target_group(msg_attachments, msg_reply, msg_channel)


async def get_targets_slash(msg, attachments=True, reply=True, channel=True,
                            message_search_count=8, stop_on_first=True):
    """Slash-command variant: only direct attachments are supported."""
    msg_attachments, msg_reply, msg_channel = [], [], []

    async def do_setters():
        nonlocal msg_attachments
        if attachments:
            msg_attachments = msg[0]
        # stop_on_first applies but there is nothing else to fall back to here

    await do_setters()
    return target_group(msg_attachments, msg_reply, msg_channel)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def download_discord_attachment(target, filename, keep_ext=False):
    """Download a single Discord attachment to *filename* and return the path."""
    if keep_ext:
        filename = f"{filename}.{os.path.splitext(target.filename)[1][1:]}"
    with open(filename, 'wb') as f:
        f.write(requests.get(target.url).content)
    return filename


def download_discord_attachments(targets, folder):
    """Download multiple Discord attachments into *folder* and return a list of paths."""
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return [
        download_discord_attachment(t, folder + '/' + generate_uuid_from_msg(t.id), keep_ext=False)
        for t in targets
    ]


# ---------------------------------------------------------------------------
# Command preparation helpers
# ---------------------------------------------------------------------------

async def prepare_VideoEdit(msg):
    """
    Resolve the target attachment and build an output filename for a video-edit
    prefix command.  Returns (attachment, output_path) or None on failure.
    """
    targets = (await get_targets(msg, message_search_count=message_search_count)).compile()
    if not targets:
        await msg.channel.send("Unable to find a message to edit, maybe upload a video and try again?")
        return

    file_ext = os.path.splitext(targets[0].filename)[1][1:]
    if file_ext not in valid_extensions:
        await msg.channel.send(
            f":x: File type not valid, valid file types are: "
            f"`{'`, `'.join(valid_extensions)}`"
        )
        return

    return targets[0], f"{generate_uuid_folder_from_msg(msg.id)}.{file_ext}"


async def prepare_VideoEdit_SlashCmd(msg):
    """
    Slash-command variant of prepare_VideoEdit.
    *msg* is [attachment_list, uuid, interaction].
    """
    targets = (await get_targets_slash(msg, message_search_count=message_search_count)).compile()
    if not targets:
        await msg.og.followup.send("Unable to find a message to edit, maybe upload a video and try again?")
        return

    file_ext = os.path.splitext(targets[0].filename)[1][1:]
    if file_ext not in valid_extensions:
        await msg.og.followup.send(
            f":x: File type not valid, valid file types are: "
            f"`{'`, `'.join(valid_extensions)}`"
        )
        return
    
    return targets[0], f"{generate_uuid_folder_from_msg(msg[1])}.{file_ext}"


async def prepare_concat(msg, args):
    """
    Parse the concat argument string, gather video attachments, and return
    them sorted according to any name specifiers the user provided.
    """
    concat_count, *name_spec = params if len(params := args.split()) else '2'
    try:
        concat_count = min(
            max_concat_count,
            max(2, int(concat_count) if len(concat_count.strip()) else len(msg.attachments))
        )
    except Exception:
        await msg.reply(f':x: No video amount given, interpreting "{concat_count}" as specifier...')
        name_spec.insert(0, concat_count)
        concat_count = min(max_concat_count, max(2, len(name_spec)))

    targets_unsorted = list(filter(
        lambda t: os.path.splitext(t.filename)[1][1:] in valid_video_extensions,
        (await get_targets(msg, message_search_count=message_search_count, stop_on_first=False)).compile()
    ))[:concat_count]

    if len(targets_unsorted) < 2:
        await msg.reply(":x: Unable to find enough videos to combine.")
        return

    # Re-order targets so that name-specifier matches come first
    targets = []
    for s in map(lambda c: c.strip().lower(), name_spec):
        i = 0
        while i < len(targets_unsorted):
            if targets_unsorted[i].filename.lower().startswith(s):
                targets.append(targets_unsorted.pop(i))
            i += 1
    targets += targets_unsorted

    return targets


# ---------------------------------------------------------------------------
# Result posting
# ---------------------------------------------------------------------------

def process_result_post(msg, res, filename="video.mp4", prefix=None, random_message=True):
    """
    Queue a reply for a finished prefix-command job.  Increments the global
    command counter and writes it to disk.
    """
    if res.success:
        text = random.choice(response_messages) if random_message else res.message

        global command_count
        command_count += 1
        print(f"The total amount has been increased to {command_count}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - edited video #{command_count})\n"
        with open(COMMAND_COUNT_FILE, "w") as f:
            f.write(str(command_count))
        with open(COMMAND_LOG_FILE, "a") as log_file:
            log_file.write(log_entry)

        next_milestone = get_next_milestone(command_count)
        remaining      = int(next_milestone) - int(command_count)

        if is_milestone(command_count):
            print("🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉 WOWIE ZOWIE! NEW ACHIEVEMENT!! 🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉")
            content = (
                f"# YOU JUST EDITED THE {command_count}th VIDEO! :tada:\n"
                f"Please send proof to *beebo_robot64* **immediately** and join the IV2Video server for the celebration!\n"
                f"**See you there!**\n-# **#{command_count}**"
            )
        else:
            base = f"{text.strip()}\n"
            if prefix:
                base += f"{prefix.strip()}\n"
            base += f"-# **#{command_count}**\n-# {remaining} left until {next_milestone}"
            content = base

        messageQue.append(qued_msg(
            context=msg, filepath=res.filename,
            filename=hash_filename(filename), message=content, reply=True))
    else:
        messageQue.append(qued_msg(context=msg, message=res.message, reply=True))


async def process_result_post_SlashCmd(msg, res, filename="video.mp4", prefix=None, random_message=True):
    """
    Send a followup for a finished slash-command job.  Mirrors
    process_result_post but uses the interaction followup API directly.
    """
    if res.success:
        text = random.choice(response_messages) if random_message else res.message

        filesize = os.path.getsize(res.filename)
        if filesize >= FILE_SIZE_LIMIT_MB * 1024 ** 2:
            await msg.followup.send(
                f":x: Sorry, but the resulting file ({human_size(filesize)}) "
                f"is over the {FILE_SIZE_LIMIT_MB}MB file size limit."
            )
        return
    
        global command_count
        command_count += 1
        print(f"The total amount has been increased to {command_count}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - edited video #{command_count})\n"
        with open(COMMAND_COUNT_FILE, "w") as f:
            f.write(str(command_count))
        with open(COMMAND_LOG_FILE, "a") as log_file:
            log_file.write(log_entry)

        next_milestone = get_next_milestone(command_count)
        remaining      = int(next_milestone) - int(command_count)

        if is_milestone(command_count):
            print("🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉 WOWIE ZOWIE! NEW ACHIEVEMENT!! 🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉")
            content = (
                f"# YOU JUST EDITED THE {command_count}th VIDEO! :tada:\n"
                f"Please send proof to *beebo_robot64* **immediately** and join the IV2Video server for the celebration!\n"
                f"**See you there!**\n-# **#{command_count}**"
            )
            await msg.followup.send(content, files=[discord.File(res.filename)], ephemeral=False)
        else:
            base = f"{text.strip()}\n"
            if prefix:
                base += f"`{prefix.strip()}`\n"
            base += f"-# **#{command_count}**\n-# {remaining} left until {next_milestone}"
            await msg.followup.send(base, files=[discord.File(res.filename)], ephemeral=False)
    else:
        await msg.followup.send(res.message)


# ---------------------------------------------------------------------------
# Prefix-command parser
# ---------------------------------------------------------------------------

async def parse_command(message):
    """
    Parse an incoming message as a IV2Video prefix command and dispatch it.

    Supports chaining via '>>', donor/ghost overrides, and a meta-prefix
    system that lets users trigger the bot by replying or @-mentioning it.
    """
    # Ignore other bots; allow the bot itself only when chaining (║ present)
    if (message.author.id == bot.user.id and '║' not in message.content) or \
       (message.author.bot and message.author.id != bot.user.id):
        return

    original_msg = message.content
    msg = message.content.split('║', 1)[0]
    if not msg:
        return

    try:
        is_reply_to_bot = (message.reference and
                           (await message.channel.fetch_message(
                               message.reference.message_id)).author.id == bot.user.id)
    except discord.errors.NotFound:
        is_reply_to_bot = False

    # Skip bare messages that aren't directed at the bot
    if (message.author.id != bot.user.id
            and not is_reply_to_bot
            and msg.split('>>')[0].removeprefix('!').strip() == ""):
        return

    has_meta_prefix = is_reply_to_bot
    append_space = ' ' if ' ' in msg else ''
    for pre in meta_prefixes:
        if msg.startswith(pre + append_space):
            has_meta_prefix = True
            msg = msg.removeprefix(pre).lstrip()
            break

    cmd_name_opts = ["concat", "combine", "download", "downloader", "bv"]
    author_id     = str(message.author.id)

    chain_limit = 9999 if message.author.id == bot.user.id else (
        user_timeout_durations[author_id]["max_chain"]
        if author_id in user_timeout_durations and "max_chain" in user_timeout_durations[author_id]
        else command_chain_limit
    )

    command, *remainder = msg.split(">>")[:chain_limit]
    if command.startswith('!'):
        command = command.removeprefix('!')

    remainder = clean_message('>>'.join(remainder)).strip()

    if remainder and not any(remainder.removeprefix('!').startswith(i) for i in cmd_name_opts):
        remainder = f"bv {remainder}"

    spl  = command.strip().split(' ', 1)
    cmd  = spl[0].strip()
    args = spl[1].strip() if len(spl) > 1 else ""

    # Route to the correct handler
    final_command_name = None
    if cmd in ["concat", "combine"]:
        final_command_name = "concat"
    elif cmd in ["download", "downloader"]:
        final_command_name = "download"
    elif cmd == "help":
        final_command_name = "help"
    elif cmd == "leaderboard.global":
        final_command_name = "leaderboard.global"
    elif cmd == "discord":
        final_command_name = "discord"
    elif cmd == "iv2video":
        final_command_name = "iv2video"
    elif cmd == "sysinfo":
        final_command_name = "sysinfo"
    elif (ev1 := (cmd in ["bv", ""])) or has_meta_prefix:
        final_command_name = "bv"
        if not ev1 or cmd == "":
            args = f"{spl[0].strip()} {args}"

    if not final_command_name:
        return

    is_timeout = apply_timeouts(
        message, cmd,
        guild_timeout_durations, user_timeout_durations,
        guild_timeouts, user_timeouts)
    if is_timeout is not True:
        await message.reply(f"Please wait {ceil(is_timeout)} seconds to use this command again.")
        return

    match final_command_name:
        case "help":
            if 'ovb' in original_msg:
                await message.reply(
                    "VideoEditBot Command Documentation: "
                    "https://github.com/GanerCodes/videoEditBot/blob/master/COMMANDS.md")

        case "discord":
            if 'ovb' in original_msg:
                await message.reply("Join our Discord server: https://discord.gg/yCVkJaDc37")

        case "iv2video":
            if 'ovb' in original_msg:
                await message.reply("Visit IV2Video: https://iv2video.com")

        case "sysinfo":
            if 'ovb' in original_msg:
                guild_count  = len(bot.guilds)
                guild_pct    = guild_count / 100 * 100
                guild_str    = f"**{guild_count}/100** {guild_pct:.1f}%"
                next_ms      = int(get_next_milestone(command_count))
                left_until   = next_ms - command_count
                embed = discord.Embed(title="IV2Video System Info", color=discord.Color.purple())
                embed.add_field(name="Bot Uptime",         value=get_uptime(), inline=False)
                embed.add_field(name="Total Files Edited", value=f"**{command_count}** edited so far - {left_until} until {next_ms}", inline=False)
                embed.add_field(name="Servers",            value=guild_str,    inline=False)
                await message.reply(embed=embed)

        case "concat":
            Task(
                Action(prepare_concat, message, args,
                       name="Concat Command Prep",
                       check=lambda x: x),
                Action(download_discord_attachments, swap_arg("result"),
                       generate_uuid_folder_from_msg(message.id),
                       name="Download videos to Concat",
                       check=lambda x: x),
                Action(combiner, swap_arg("result"),
                       (concat_filename := f"{generate_uuid_folder_from_msg(message.id)}.mp4"),
                       SILENCE="./editor/SILENCE.mp3",
                       print_info=False,
                       name="Concat Videos",
                       fail_action=Action(
                           lambda n, e: messageQue.append(
                               qued_msg(context=message,
                                        message="Sorry, something went wrong during concatenation.",
                                        reply=True)))),
                Action(process_result_post, message,
                       result(True, concat_filename, ""), concat_filename, remainder,
                       name="Post Concat"),
                async_handler=async_runner
                ).run_threaded()
            
        case "download":
            Task(
                Action(download,
