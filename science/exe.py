# Copyright 2022 Science project contributors.
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import dataclasses
import functools
import hashlib
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path
from textwrap import dedent
from types import TracebackType
from typing import BinaryIO

import click
import click_log
from click_didyoumean import DYMGroup
from packaging import version

from science import __version__
from science.commands import build, lift
from science.commands.lift import AppInfo, FileMapping, LiftConfig, PlatformInfo
from science.config import parse_config
from science.context import ScienceConfig
from science.errors import InputError
from science.fs import temporary_directory
from science.model import Application
from science.platform import Platform

logger = logging.getLogger(__name__)


def _log_fatal(
    type_: type[BaseException],
    value: BaseException,
    tb: TracebackType,
    *,
    always_include_backtrace: bool,
) -> None:
    if always_include_backtrace or not isinstance(value, InputError):
        click.secho("".join(traceback.format_tb(tb)), fg="yellow", file=sys.stderr, nl=False)
        click.secho(
            f"{type_.__module__}.{type_.__qualname__}: ", fg="yellow", file=sys.stderr, nl=False
        )
    click.secho(value, fg="red", file=sys.stderr)


@click.group(
    cls=DYMGroup,
    context_settings=dict(auto_envvar_prefix="SCIENCE", help_option_names=["-h", "--help"]),
)
@click.version_option(__version__, "-V", "--version", message="%(version)s")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help=dedent(
        """\
        Increase output verbosity.

        Can be specified multiple times to further increase verbosity.
        """
    ),
)
@click.option(
    "-q",
    "--quiet",
    count=True,
    help=dedent(
        """\
        Decrease output verbosity.

        Can be specified multiple times to further decrease verbosity.
        """
    ),
)
@click.option(
    "--cache-dir",
    type=Path,
    default=ScienceConfig.DEFAULT_CACHE_DIR,
    show_default=True,
    help="Specify an alternate location for the science cache.",
)
@click.pass_context
def _main(ctx: click.Context, verbose: int, quiet: int, cache_dir: Path) -> None:
    """Science helps you prepare scies for your application.

    Science provides a high-level TOML manifest format for a scie application and can build scies
    and export scie lift JSON manifests from these configuration files.

    For more information on the TOML manifest format, see:
    https://github.com/a-scie/lift/blob/main/docs/manifest.md
    """
    science_config = ScienceConfig(verbosity=verbose - quiet, cache_dir=cache_dir)
    science_config.configure_logging(root_logger=click_log.basic_config())
    sys.excepthook = functools.partial(_log_fatal, always_include_backtrace=science_config.verbose)
    ctx.obj = science_config


pass_lift = click.make_pass_decorator(LiftConfig)


@_main.group(cls=DYMGroup, name="lift")
@click.option(
    "--file",
    "file_mappings",
    metavar="NAME=LOCATION",
    type=FileMapping.parse,
    multiple=True,
    default=[],
    help=dedent(
        """\
        Map paths to files defined in your manifest.

        Science looks fore each non-lazy file you define at the path denoted by its name relative
        to the CWD you invoke science from. If any file is not at that path, you can tell science
        to look elsewhere with: `--file <name>=<location>`.

        For example, for this manifest snippet::

         \b
         [[lift.files]]
         name = "example.txt"

        If the file is located at `src/examples/example.txt` relative to the CWD you would specify
        `--file example.txt=src/examples/example.txt`.
        """
    ),
)
@click.option(
    "--invert-lazy",
    "invert_lazy_ids",
    metavar="FILE_ID",
    multiple=True,
    default=[],
    help=dedent(
        """\
        Toggle the laziness of a file declared in the application lift manifest.

        For example, for this manifest snippet::

         \b
         [lift]
         name = "example"
         \b
         [[lift.interpreters]]
         id = "cpython"
         provider = "PythonBuildStandalone"
         version = "3.11"
         \b
         [[lift.files]]
         name = "example.txt"
         digest = { size = 137, fingerprint = "abcd1234" }}
         source = { url = "https://example.com", lazy = false }

        The default scie built will be "fat". Both the Python Build Standalone CPython 3.11
        interpreter distribution and the example.txt file will be downloaded by `science` and
        packed into the `example` (or `example.exe` on Windows) scie.

        To create a "skinny" scie in addition using this same application lift manifest you can
        specify::

         \b
         science lift --invert-lazy cpython --invert-lazy example.txt --app-name example-thin

        The resulting `example-thin` (or `example-thin.exe` on Windows) scie will include the
        `ptex` binary which will be used to fetch both the Python Build Standalone CPython 3.11
        interpreter distribution and the example.txt file upon first execution.

        Note: only interpreter distributions and files with url sources can be toggled. Trying to
        toggle the laziness for other file types, like those with either no source or a binding
        command source, will produce an informative error.
        """
    ),
)
@click.option(
    "--include-provenance",
    is_flag=True,
    help=dedent(
        """\
        Include provenance information for the build in the resulting scie lift JSON manifest.

        Provenance information for the `science` binary used to build the scie as well as
        provenance information for the lift manifest TOML used to create the scie will be included.

        If run in a git repository, the git state will be included in
        `git describe --always --dirty --long` format.

        If the application lift manifest has a `[lift.app_info]` table, all data in that table will
        be included. If any `--app-info` are specified, these top-level keys will also be included
        and over-ride any top level keys of the same name present in `[lift.app_info]`.

        For example, given the following application lift manifest snippet::

         \b
         [lift.app_info]
         provided_by = { sponsor = "example.org", licenses = ["Apache-2.0", "MIT"] }
         edition = "free"

        Running the following::

         \b
         science lift \\
             --include-provenance \\
             --app-info edition=paid \\
             --app-info releaser=$(id -un) \\
             export

        Would result in a scie lift JSON manifest with extra content like::

         \b
         {
           "scie": {
             ...
           },
           "science": {
             "app_info": {
               "edition" = "paid"
               "provided_by": {
                 "licenses": [
                   "Apache-2.0",
                   "MIT"
                 ],
                 "releaser": "jsirois",
                 "sponsor": "example.org"
               }
             },
             "binary": {
               "url": "https://github.com/a-scie/lift/releases/tag/v0.1.0/science-linux-x86_64",
               "version": "0.1.0"
             },
             "git_state": "v0.1.0-0-gc423e47",
             "manifest": {
               "hash": "49dc36a6db71bccf1bff35363454f7567fd124ba80d1e488bd320668a11c70bc",
               "size": 432,
               "source": "lift.toml"
             },
             "notes": [
               "This scie lift JSON manifest was generated from a source lift toml manifest using the science binary.",
               "Find out more here: https://github.com/a-scie/lift/blob/v0.1.0/README.md"
             ]
           }
         }
        """
    ),
)
@click.option(
    "--app-name",
    help=dedent(
        """\
        Override the name of the application declared in the lift manifest.

        This is particularly useful in combination with `--invert-lazy` to produce both "skinny"
        and "fat" scies from the same lift manifest. See the `--invert-lazy` help for an example.
        """
    ),
)
@click.option(
    "--app-info",
    metavar="KEY=VALUE",
    type=AppInfo.parse,
    multiple=True,
    default=[],
    help=dedent(
        """\
        Override top-level `[lift.app_info]` keys or define new ones.

        Implies `--include-provenance` whose help provides an example.
        """
    ),
)
@click.pass_context
def _lift(
    ctx: click.Context,
    file_mappings: list[FileMapping],
    invert_lazy_ids: list[str],
    include_provenance: bool,
    app_name: str | None,
    app_info: list[AppInfo],
) -> None:
    """Perform operations against your application lift TOML manifest.

    For more information on the TOML manifest format, see:
    https://github.com/a-scie/lift/blob/main/docs/manifest.md
    """
    ctx.obj = LiftConfig(
        file_mappings=tuple(file_mappings),
        invert_lazy_ids=frozenset(invert_lazy_ids),
        include_provenance=include_provenance or bool(app_info),
        app_info=tuple(app_info),
        app_name=app_name,
    )


def config_arg():
    return click.argument(
        "config", metavar="LIFT_TOML_PATH", type=click.File("rb"), default="lift.toml"
    )


def dest_dir_option():
    return click.option(
        "--dest-dir",
        type=Path,
        default=Path.cwd(),
        show_default=True,
        help=dedent("The destination directory to output files to."),
    )


def use_platform_suffix_option():
    return click.option(
        "--use-platform-suffix",
        is_flag=True,
        help=dedent(
            """\
            Force science to use a platform suffix.

            Science will automatically use a platform suffix for disambiguation. When there is no
            ambiguity, you can force a suffix anyway by using this flag.

            The current platform suffixes are::

             \b
             {suffixes}

            * Indicates the current platform.
            """
        ).format(
            suffixes="\n ".join(
                f"{'*' if platform == Platform.current() else ' '} {platform.value}"
                for platform in Platform
            )
        ),
    )


def parse_application(lift_config: LiftConfig, config: BinaryIO) -> Application:
    application = parse_config(config, source=config.name)
    if lift_config.app_name:
        application = dataclasses.replace(application, name=lift_config.app_name)
    return application


@_lift.command()
@config_arg()
@dest_dir_option()
@use_platform_suffix_option()
@pass_lift
def export(
    lift_config: LiftConfig,
    config: BinaryIO,
    dest_dir: Path,
    use_platform_suffix: bool,
) -> None:
    """Export the lift TOML manifest as one or more scie lift JSON manifests."""

    application = parse_application(lift_config, config)
    platform_info = PlatformInfo.create(application, use_suffix=use_platform_suffix)
    with temporary_directory(cleanup=True) as td:
        for _, manifest_path in lift.export_manifest(lift_config, application, dest_dir=td):
            lift_manifest = dest_dir / (
                manifest_path.relative_to(td) if platform_info.use_suffix else manifest_path.name
            )
            lift_manifest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(
                manifest_path,
                lift_manifest,
            )
            click.echo(lift_manifest)


@_lift.command(name="build")
@config_arg()
@dest_dir_option()
@use_platform_suffix_option()
@click.option(
    "--preserve-sandbox",
    is_flag=True,
    help=dedent(
        """\
        Preserve the scie assembly sandbox and print its path to stderr.

        When `science` builds a scie it creates a temporary sandbox to house the exported JSON lift
        manifest and any application files that will be included in the scie. If you preserve the
        sandbox, the native `scie-jump` binary is also included such that you can change directory
        to the sandbox and run `scie-jump` (or `scie-jump.exe` on Windows) to test assembling the
        scie "by hand".
        """
    ),
)
@click.option(
    "--use-jump",
    metavar="REPO_PATH",
    type=Path,
    help=dedent(
        """\
        The path to a clone of the scie-jump repo.

        Mainly useful for testing new `scie-jump` fixes or integrating new `scie-jump` features
        into science. The canonical repo to clone is at https://github.com/a-scie/jump.
        """
    ),
)
@click.option(
    "--hash",
    "hash_functions",
    type=click.Choice(sorted(hashlib.algorithms_guaranteed)),
    multiple=True,
    default=[],
    help=dedent(
        """\
        Output a checksum file compatible with the shasum family of tools.

        For each unique `--hash` specified, a sibling file to the scie executable will be generated
        with the same name and hash algorithm name suffix. The file will contain the hex fingerprint
        of the scie executable using that algorithm to hash it.

        For example, for `--hash sha256` against a scie named example on Windows you might get::

         \b
         dist/example.exe
         dist/example.exe.sha256

        The contents of `dist/example.exe.sha256` would look like (`*` means executable)::

         \b
         33fd890f056b0434241a357b616b4a651c82acc1ee4ce42e0b95c059d4a76f04 *example.exe

        And the fingerprint of `example.exe` could be checked by running the following in the
        `dist` dir::

         \b
         sha256sum -c example.exe.sha256
         example.exe: OK
        """
    ),
)
@pass_lift
def _build(
    lift_config: LiftConfig,
    config: BinaryIO,
    dest_dir: Path,
    use_platform_suffix: bool,
    preserve_sandbox: bool,
    use_jump: Path | None,
    hash_functions: list[str],
) -> None:
    """Build scie executables from the lift TOML manifest.

    If the LIFT_TOML_PATH is left unspecified, `lift.toml` is assumed.
    """

    application = parse_application(lift_config, config)
    platform_info = PlatformInfo.create(application, use_suffix=use_platform_suffix)

    platforms = application.platforms
    if use_jump and use_platform_suffix:
        logger.warning(f"Cannot use a custom scie jump build with a multi-platform configuration.")
        logger.warning(
            "Restricting requested platforms of "
            f"{', '.join(sorted(platform.value for platform in platforms))} to "
            f"{platform_info.current.value}",
        )
        platforms = frozenset([platform_info.current])

    scie_jump_version = application.scie_jump.version if application.scie_jump else None
    if scie_jump_version and scie_jump_version < version.parse("0.9.0"):
        # N.B.: The scie-jump 0.9.0 or later is needed to support cross-building against foreign
        # platform scie-jumps with "-sj".
        sys.exit(
            f"A scie-jump version of {scie_jump_version} was requested but {sys.argv[0]} "
            f"requires at least 0.9.0."
        )

    with temporary_directory(cleanup=not preserve_sandbox) as td:
        assembly_info = build.assemble_scies(
            lift_config=lift_config,
            application=application,
            dest_dir=td,
            platforms=platforms,
            platform_info=platform_info,
            use_jump=use_jump,
            hash_functions=hash_functions,
        )
        dest_dir.mkdir(parents=True, exist_ok=True)

        def move(path: Path) -> None:
            dst = dest_dir / path.name
            shutil.move(src=path, dst=dst)
            click.echo(dst)

        for scie_assembly in assembly_info.scies:
            move(scie_assembly.scie)
            for checksum_file in scie_assembly.hashes:
                move(checksum_file)

            if preserve_sandbox:
                (scie_assembly.lift_manifest.parent / assembly_info.native_jump.name).symlink_to(
                    assembly_info.native_jump
                )
                click.secho(
                    f"Sandbox preserved at {scie_assembly.lift_manifest.parent}", fg="yellow"
                )


def main():
    # By default, click help messages expose the fact the app is written in Python. The resulting
    # program name (`python -m module` or `__main__.py`) is both confusing and unusable for the end
    # user since both the Python distribution and the code are hidden away in the nce cache. Since
    # we know we run as a scie in normal circumstances, use the SCIE_ARGV0 exported by the
    # scie-jump when present.
    _main(prog_name=os.environ.get("SCIE_ARGV0"))
