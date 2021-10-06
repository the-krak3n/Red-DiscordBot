from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import copy
import logging
import math
import os
import re
import shutil
import tarfile
import warnings
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    NoReturn,
    Optional,
    Union,
    TypeVar,
    TYPE_CHECKING,
    Tuple,
    cast,
    final,
)

import aiohttp
import discord
import pkg_resources
from discord.ext.commands import Cog, check
from fuzzywuzzy import fuzz, process
from rich.progress import ProgressColumn
from rich.progress_bar import ProgressBar

from redbot import VersionInfo, json
from redbot.core import data_manager
from redbot.core.utils.chat_formatting import box

if TYPE_CHECKING:
    from redbot.core.bot import Red
    from redbot.core.commands import Command, Context

main_log = logging.getLogger("red")

__all__ = (
    "timed_unsu",
    "safe_delete",
    "fuzzy_command_search",
    "format_fuzzy_results",
    "create_backup",
    "send_to_owners_with_preprocessor",
    "send_to_owners_with_prefix_replaced",
    "expected_version",
    "fetch_latest_red_version_info",
    "_is_unsafe_on_strict_config",
    "is_sudo_enabled",
    "fetch_last_fork_update",
    "deprecated_removed",
    "RichIndefiniteBarColumn",
    "is_sudo_enabled",
)

_T = TypeVar("_T")

UTF8_RE = re.compile(r"^[\U00000000-\U0010FFFF]*$")
MIN_INT, MAX_INT = 1 - (2 ** 64), (2 ** 64) - 1


def safe_delete(pth: Path):
    if pth.exists():
        for root, dirs, files in os.walk(str(pth)):
            os.chmod(root, 0o700)

            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)

            for f in files:
                os.chmod(os.path.join(root, f), 0o700)

        shutil.rmtree(str(pth), ignore_errors=True)


def _fuzzy_log_filter(record):
    return record.funcName != "extractWithoutOrder"


logging.getLogger().addFilter(_fuzzy_log_filter)


async def fuzzy_command_search(
    ctx: Context,
    term: Optional[str] = None,
    *,
    commands: Optional[Union[AsyncIterator[Command], Iterator[Command]]] = None,
    min_score: int = 80,
) -> Optional[List[Command]]:
    """Search for commands which are similar in name to the one invoked.

    Returns a maximum of 5 commands which must all be at least matched
    greater than ``min_score``.

    Parameters
    ----------
    ctx : `commands.Context <redbot.core.commands.Context>`
        The command invocation context.
    term : Optional[str]
        The name of the invoked command. If ``None``,
        `Context.invoked_with` will be used instead.
    commands : Optional[Union[AsyncIterator[commands.Command], Iterator[commands.Command]]]
        The commands available to choose from when doing a fuzzy match.
        When omitted, `Bot.walk_commands` will be used instead.
    min_score : int
        The minimum score for matched commands to reach. Defaults to 80.

    Returns
    -------
    Optional[List[`commands.Command <redbot.core.commands.Command>`]]
        A list of commands which were fuzzily matched with the invoked
        command.

    """
    if ctx.guild is not None:
        enabled = await ctx.bot._config.guild(ctx.guild).fuzzy()
    else:
        enabled = await ctx.bot._config.fuzzy()

    if not enabled:
        return None

    if term is None:
        term = ctx.invoked_with

    # If the term is an alias or CC, we don't want to send a supplementary fuzzy search.
    alias_cog = ctx.bot.get_cog("Alias")
    if alias_cog is not None:
        alias = await alias_cog._aliases.get_alias(ctx.guild, term)

        if alias:
            return None
    customcom_cog = ctx.bot.get_cog("CustomCommands")
    if customcom_cog is not None:
        cmd_obj = customcom_cog.commandobj

        try:
            await cmd_obj.get(ctx.message, term)
        except:
            pass
        else:
            return None

    if commands is None:
        choices = set(ctx.bot.walk_commands())
    elif isinstance(commands, collections.abc.AsyncIterator):
        choices = {c async for c in commands}
    else:
        choices = set(commands)

    # Do the scoring. `extracted` is a list of tuples in the form `(command, score)`
    extracted = process.extract(term, choices, limit=5, scorer=fuzz.QRatio)
    if not extracted:
        return None

    # Filter through the fuzzy-matched commands.
    matched_commands = []
    for command, score in extracted:
        if score < min_score:
            # Since the list is in decreasing order of score, we can exit early.
            break
        if await command.can_see(ctx):
            matched_commands.append(command)

    return matched_commands


async def format_fuzzy_results(
    ctx: Context, matched_commands: List[Command], *, embed: Optional[bool] = None
) -> Union[str, discord.Embed]:
    """Format the result of a fuzzy command search.

    Parameters
    ----------
    ctx : `commands.Context <redbot.core.commands.Context>`
        The context in which this result is being displayed.
    matched_commands : List[`commands.Command <redbot.core.commands.Command>`]
        A list of commands which have been matched by the fuzzy search, sorted
        in order of decreasing similarity.
    embed : bool
        Whether or not the result should be an embed. If set to ``None``, this
        will default to the result of `ctx.embed_requested`.

    Returns
    -------
    Union[str, discord.Embed]
        The formatted results.

    """
    if embed is not False and (embed is True or await ctx.embed_requested()):
        lines = []
        for cmd in matched_commands:
            short_doc = cmd.format_shortdoc_for_context(ctx)
            lines.append(f"**{ctx.clean_prefix}{cmd.qualified_name}** {short_doc}")
        return discord.Embed(
            title="Perhaps you wanted one of these?",
            colour=await ctx.embed_colour(),
            description="\n".join(lines),
        )
    else:
        lines = []
        for cmd in matched_commands:
            short_doc = cmd.format_shortdoc_for_context(ctx)
            lines.append(f"{ctx.clean_prefix}{cmd.qualified_name} -- {short_doc}")
        return "Perhaps you wanted one of these? " + box("\n".join(lines), lang="vhdl")


async def create_backup(dest: Path = Path.home()) -> Optional[Path]:
    data_path = Path(data_manager.core_data_path().parent)
    if not data_path.exists():
        return None

    dest.mkdir(parents=True, exist_ok=True)
    timestr = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    backup_fpath = dest / f"redv3_{data_manager.instance_name}_{timestr}.tar.gz"

    to_backup = []
    exclusions = [
        "__pycache__",
        "Lavalink.jar",
        os.path.join("Downloader", "lib"),
        os.path.join("CogManager", "cogs"),
        os.path.join("RepoManager", "repos"),
        os.path.join("Audio", "logs"),
    ]

    # Avoiding circular imports
    from ...cogs.downloader.repo_manager import RepoManager

    repo_mgr = RepoManager()
    await repo_mgr.initialize()
    repo_output = []
    for repo in repo_mgr.repos:
        repo_output.append({"url": repo.url, "name": repo.name, "branch": repo.branch})
    repos_file = data_path / "cogs" / "RepoManager" / "repos.json"
    with repos_file.open("w") as fs:
        json.dump(repo_output, fs, indent=4)
    instance_file = data_path / "instance.json"
    with instance_file.open("w") as fs:
        json.dump({data_manager.instance_name: data_manager.basic_config}, fs, indent=4)
    for f in data_path.glob("**/*"):
        if not any(ex in str(f) for ex in exclusions) and f.is_file():
            to_backup.append(f)

    with tarfile.open(str(backup_fpath), "w:gz") as tar:
        for f in to_backup:
            tar.add(str(f), arcname=str(f.relative_to(data_path)), recursive=False)
    return backup_fpath


# this might be worth moving to `bot.send_to_owners` at later date


async def send_to_owners_with_preprocessor(
    bot: Red,
    content: str,
    *,
    content_preprocessor: Optional[
        Callable[[Red, discord.abc.Messageable, str], Awaitable[str]]
    ] = None,
    **kwargs,
):
    """
    This sends something to all owners and their configured extra destinations.

    This acts the same as `Red.send_to_owners`, with
    one added keyword argument as detailed below in *Other Parameters*.

    Other Parameters
    ----------------
    content_preprocessor: Optional[Callable[[Red, discord.abc.Messageable, str], Awaitable[str]]]
        Optional async function that takes
        bot object, owner notification destination and message content
        and returns the content that should be sent to given location.
    """
    destinations = await bot.get_owner_notification_destinations()

    async def wrapped_send(bot, location, content=None, preprocessor=None, **kwargs):
        try:
            if preprocessor is not None:
                content = await preprocessor(bot, location, content)
            await location.send(content, **kwargs)
        except Exception as _exc:
            main_log.error(
                "I could not send an owner notification to %s (%s)",
                location,
                location.id,
                exc_info=_exc,
            )

    sends = [wrapped_send(bot, d, content, content_preprocessor, **kwargs) for d in destinations]
    await asyncio.gather(*sends)


async def send_to_owners_with_prefix_replaced(bot: Red, content: str, **kwargs):
    """
    This sends something to all owners and their configured extra destinations.

    This acts the same as `Red.send_to_owners`, with one addition - `[p]` in ``content`` argument
    is replaced with a clean prefix for each specific destination.
    """

    async def preprocessor(bot: Red, destination: discord.abc.Messageable, content: str) -> str:
        prefixes = await bot.get_valid_prefixes(getattr(destination, "guild", None))
        prefix = re.sub(
            rf"<@!?{bot.user.id}>", f"@{bot.user.name}".replace("\\", r"\\"), prefixes[0]
        )
        return content.replace("[p]", prefix)

    await send_to_owners_with_preprocessor(bot, content, content_preprocessor=preprocessor)


def expected_version(current: str, expected: str) -> bool:
    # `pkg_resources` needs a regular requirement string, so "x" serves as requirement's name here
    return current in pkg_resources.Requirement.parse(f"x{expected}")


async def fetch_latest_red_version_info() -> Tuple[Optional[VersionInfo], Optional[str]]:
    try:
        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get("https://pypi.org/pypi/Red-DiscordBot/json") as r:
                data = await r.json(loads=json.loads)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, None
    else:
        release = VersionInfo.from_str(data["info"]["version"])
        required_python = data["info"]["requires_python"]

        return release, required_python


async def fetch_last_fork_update() -> Tuple[Optional[int], Optional[str]]:
    try:
        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(
                "https://api.github.com/repos/Drapersniper/Red-DiscordBot/commits"
            ) as r:
                if r.status != 200:
                    return None, None
                data = await r.json(loads=json.loads)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, None
    else:
        date = (
            datetime.strptime(data[0]["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ")
            - datetime(1970, 1, 1)
        ).total_seconds()
        sha = data[0]["sha"]
        return int(date), sha


def _is_unsafe_on_strict_config(data: Any) -> bool:
    _type = type(data)
    if _type not in {dict, list, str, int, float}:
        return True
    if _type is dict:
        for k, v in data.items():
            if _is_unsafe_on_strict_config(k) or _is_unsafe_on_strict_config(v):
                return True
    if _type is list:
        for i in data:
            if _is_unsafe_on_strict_config(i):
                return True
    if _type is str and not UTF8_RE.match(data):
        return True
    if _type is int:
        if not (MIN_INT < data < MAX_INT):
            return True
    if _type is float:
        if math.isnan(data) or math.isinf(data):
            return True


@final
class ProxyCounter:
    __slots__ = ("__counters",)

    def __init__(self):
        self.__counters: Dict[str, Dict[str, int]] = {}

    def register_counters(self, cog: Cog, *counters: str) -> None:
        self.register_counters_raw(cog.qualified_name, *counters)

    def register_counters_raw(self, cog_qualified_name: str, *counters: str) -> None:
        if not type(cog_qualified_name) is str:
            raise TypeError(
                f"Expected cog_qualified_name to be a string, received {cog_qualified_name.__class__.__name__} instead."
            )
        if not all(type(counter) is str for counter in counters):
            raise TypeError("Expected counter to be a string.")
        if cog_qualified_name.lower().startswith("red_"):
            raise RuntimeError("You cannot add counters to the 'red_' namespace.")
        if cog_qualified_name not in self.__counters:
            self.__counters[cog_qualified_name] = {}
        for counter in counters:
            counter = str(counter)
            if counter not in self.__counters[cog_qualified_name]:
                self.__counters[cog_qualified_name][counter] = 0

    def _register_core_counters_raw(self, cog_qualified_name: str, *counters: str):
        if not type(cog_qualified_name) is str:
            raise TypeError(
                f"Expected cog_qualified_name to be a string, received {cog_qualified_name.__class__.__name__} instead."
            )
        if not all(type(counter) is str for counter in counters):
            raise TypeError("Expected counter to be a string.")
        if not cog_qualified_name.lower().startswith("red_"):
            raise RuntimeError(
                "This private method should only be used to register cogs to the 'red_'."
            )
        if cog_qualified_name not in self.__counters:
            self.__counters[cog_qualified_name] = {}
        for counter in counters:
            counter = str(counter)
            if counter not in self.__counters[cog_qualified_name]:
                self.__counters[cog_qualified_name][counter] = 0

    def unregister_counter(self, cog: Cog, counter: str) -> None:
        self.unregister_counter_raw(cog.qualified_name, counter)

    def unregister_counter_raw(self, cog_qualified_name: str, counter: str) -> None:
        if not type(cog_qualified_name) is str:
            raise TypeError(
                f"Expected cog_qualified_name to be a string, received {cog_qualified_name.__class__.__name__} instead."
            )
        if not type(counter) is str:
            raise TypeError(
                f"Expected counter to be a string, received {counter.__class__.__name__} instead."
            )
        if not self.__contains__((cog_qualified_name, counter)):
            raise KeyError(f"'{counter}' hasn't been registered under '{cog_qualified_name}'.")
        if cog_qualified_name.lower().startswith("red_"):
            raise RuntimeError("You cannot removed counters added to the 'red_' namespace.")
        del self.__counters[cog_qualified_name][counter]

    def get(self, cog: Cog, counter: str) -> int:
        return self.get_raw(cog.qualified_name, counter)

    def get_raw(self, cog_qualified_name: str, counter: str) -> int:
        return self.__getitem__(
            (
                cog_qualified_name,
                counter,
            )
        )

    def inc(self, cog: Cog, counter: str, by: int = 1) -> int:
        return self.inc_raw(cog.qualified_name, counter, by=by)

    def inc_raw(self, cog_qualified_name: str, counter: str, by: int = 1) -> int:
        if not type(cog_qualified_name) is str:
            raise TypeError(
                f"Expected cog_qualified_name to be a string, received {cog_qualified_name.__class__.__name__} instead."
            )
        if not type(counter) is str:
            raise TypeError(
                f"Expected counter to be a string, received {counter.__class__.__name__} instead."
            )
        if not self.__contains__((cog_qualified_name, counter)):
            raise KeyError(f"'{counter}' hasn't been registered under '{cog_qualified_name}'.")
        if cog_qualified_name.lower().startswith("red_"):
            raise RuntimeError("You cannot increment counters in the 'red_' namespace.")
        if not type(by) is int:
            raise TypeError(
                f"Expected 'by' to be an integer, received {by.__class__.__name__} instead."
            )
        elif by < 0:
            raise ValueError(
                f"'by' needs to be greater than or equals to 0, however '{by}' was provided."
            )

        self.__counters[cog_qualified_name][counter] += by
        return self.__counters[cog_qualified_name][counter]

    def _inc_core_raw(self, cog_qualified_name: str, counter: str, by: int = 1) -> int:
        if not type(cog_qualified_name) is str:
            raise TypeError(
                f"Expected cog_qualified_name to be a string, received {cog_qualified_name.__class__.__name__} instead."
            )
        if not type(counter) is str:
            raise TypeError(
                f"Expected counter to be a string, received {counter.__class__.__name__} instead."
            )
        if not self.__contains__((cog_qualified_name, counter)):
            raise KeyError(f"'{counter}' hasn't been registered under '{cog_qualified_name}'.")
        if not cog_qualified_name.lower().startswith("red_"):
            raise RuntimeError(
                "You cannot increment counters outside the 'red_' namespace with this private method."
            )
        if not type(by) is int:
            raise TypeError(
                f"Expected counter to be an integer, received {counter.__class__.__name__} instead."
            )
        elif by < 0:
            raise ValueError(
                f"'by' needs to be greater than or equals to 0, however '{by}' was provided."
            )

        self.__counters[cog_qualified_name][counter] += by
        return self.__counters[cog_qualified_name][counter]

    def contains(self, cog: Cog, counter: str) -> bool:
        return self.contains_raw(cog.qualified_name, counter)

    def contains_raw(self, cog_qualified_name: str, counter: str) -> bool:
        return self.__contains__(
            (
                cog_qualified_name,
                counter,
            )
        )

    def get_all(self) -> Dict[str, Dict[str, int]]:
        return copy.deepcopy(self.__counters)

    def walk_counters(self, cog: Optional[Cog] = None) -> Optional[Tuple[str, str, int]]:
        if cog:
            if isinstance(cog, Cog):
                cog_name = cog.qualified_name
            else:
                cog_name = cog

            if not type(cog_name) is str:
                raise TypeError(
                    f"Expected cog_name to be a string, received {cog_name.__class__.__name__} instead."
                )
            if cog_name not in self.__counters:
                return
            for counter, value in self.__counters[cog_name].items():
                yield cog_name, counter, value
        else:
            for cog_name, cog_counters in self.__counters.items():
                for counter, value in cog_counters.items():
                    yield cog_name, counter, value

    def __contains__(self, keys: Tuple[Union[Cog, str], str]) -> bool:
        cog, counter = keys[0], keys[1]
        if not type(counter) is str:
            raise TypeError(
                f"Expected counter to be a string, received {counter.__class__.__name__} instead."
            )
        if isinstance(cog, Cog):
            cog_name = cog.qualified_name
        else:
            cog_name = cog

        if not type(cog_name) is str:
            raise TypeError(
                f"Expected cog_name to be a string, received {cog_name.__class__.__name__} instead."
            )
        if cog_name in self.__counters:
            if counter in self.__counters[cog_name]:
                return True
        return False

    def __getitem__(self, keys: Tuple[Union[Cog, str], str]) -> int:
        cog, counter = keys[0], keys[1]
        if not type(counter) is str:
            raise TypeError(
                f"Expected counter to be a string, received {counter.__class__.__name__} instead."
            )
        if isinstance(cog, Cog):
            cog_name = cog.qualified_name
        else:
            cog_name = cog
        if not type(cog_name) is str:
            raise TypeError(
                f"Expected cog_name to be a string, received {cog_name.__class__.__name__} instead."
            )
        if cog_name not in self.__counters:
            raise KeyError(f"'{cog_name}' hasn't registered any counters.")

        if counter not in self.__counters[cog_name]:
            raise KeyError(f"'{counter}' hasn't been registered under '{cog_name}'.")

        return self.__counters[cog_name][counter]

    def __dir__(self):
        return [
            "__contains__",
            "__repr__",
            "__getitem__",
            "contains",
            "contains_raw",
            "get",
            "get_all",
            "get_raw",
            "inc",
            "inc_raw",
            "register_counters",
            "register_counters_raw",
            "unregister_counter",
            "unregister_counter_raw",
            "walk_counters",
        ]

    def __delitem__(self, keys: Tuple[Union[Cog, str], str]) -> NoReturn:
        raise NotImplementedError("This operation is not supported.")

    def __setitem__(self, keys: Tuple[Union[Cog, str], str], value: int) -> NoReturn:
        raise NotImplementedError("This operation is not supported.")

    def __repr__(self) -> str:
        return "ProxyCounter(cogs={}, counters={})".format(
            len(self.__counters), sum(len(v) for v in self.__counters.values())
        )


def deprecated_removed(
    deprecation_target: str,
    deprecation_version: str,
    minimum_days: int,
    message: str = "",
    stacklevel: int = 1,
) -> None:
    warnings.warn(
        f"{deprecation_target} is deprecated since version {deprecation_version}"
        " and will be removed in the first minor version that gets released"
        f" after {minimum_days} days since deprecation. {message}",
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )


class RichIndefiniteBarColumn(ProgressColumn):
    def render(self, task):
        return ProgressBar(
            pulse=task.completed < task.total,
            animation_time=task.get_time(),
            width=40,
            total=task.total,
            completed=task.completed,
        )


def is_sudo_enabled():
    """Deny the command if sudo mechanic is not enabled."""

    async def predicate(ctx):
        return ctx.bot._sudo_ctx_var is not None

    return check(predicate)


async def timed_unsu(user_id: int, bot: Red):
    await asyncio.sleep(delay=await bot._config.sudotime())
    bot._elevated_owner_ids -= {user_id}
    bot._owner_sudo_tasks.pop(user_id, None)
