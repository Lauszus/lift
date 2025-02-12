# Copyright 2023 Science project contributors.
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import glob
import hashlib
import io
import itertools
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from enum import Enum
from functools import wraps
from pathlib import Path, PurePath
from textwrap import dedent
from typing import Any, Callable, Collection, Iterable, TypeVar, cast

import nox
from nox import Session

nox.needs_version = ">=2022.11.21"
nox.options.stop_on_first_error = True
nox.options.sessions = ["fmt", "lint", "check", "test"]

# N.B.: Our nox version specifier above admits nox releases that support Python >=3.7, but we set
# the floor to the oldest Python supported by python.org.
MIN_NOX_PYTHON = (3, 8)
MIN_NOX_PYTHON_VER = ".".join(map(str, MIN_NOX_PYTHON))

if sys.version_info[:2] < MIN_NOX_PYTHON:
    sys.exit(
        dedent(
            f"""\
            This project requires nox running on Python>={MIN_NOX_PYTHON_VER}.

            Your nox is currently running under Python:
            version info: {sys.version}
                    path: {sys.executable}
            """
        )
    )


# N.B.: When updating these, update the corresponding values for the PythonBuildStandalone provider
# in lift.toml.
PBS_RELEASE = "20241206"
PBS_VERSION = "3.12.8"
PBS_FLAVOR = "install_only_stripped"

REQUIRES_PYTHON_VERSION = ".".join(PBS_VERSION.split(".")[:2])

PEX_REQUIREMENT = "pex==2.25.2"
PEX_PEX = f"pex-{hashlib.sha1(PEX_REQUIREMENT.encode('utf-8')).hexdigest()}.pex"

BUILD_ROOT = Path().resolve()
WINDOWS_AMD64_COMPLETE_PLATFORM = BUILD_ROOT / "complete-platform.windows-amd64-py3.12.json"
LOCK_FILE = BUILD_ROOT / "lock.json"


class OperatingSystem(Enum):
    @classmethod
    def current(cls) -> OperatingSystem:
        if sys.platform == "linux":
            return cls.LINUX
        elif sys.platform == "darwin":
            return cls.MAC
        elif os.name == "nt":
            return cls.WINDOWS
        else:
            expected = (
                f"{', '.join(_os.value for _os in tuple(cls)[:-1])} or {tuple(cls)[-1].value}"
            )
            sys.exit(
                dedent(
                    f"""\
                    Incompatible operating system. Expected {expected}:
                     interpreter: {sys.executable}
                    sys.platform: {sys.platform}
                         os.name: {os.name}
                    """
                )
            )

    LINUX = "Linux"
    MAC = "macOs"
    WINDOWS = "Windows"


OS = OperatingSystem.current()
IS_ARM64 = (
    subprocess.run(
        args=["pwsh.exe", "-c", "$Env:PROCESSOR_ARCHITECTURE.ToLower()"],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if OperatingSystem.WINDOWS is OS
    else platform.machine().lower()
) in ("aarch64", "arm64")
IS_ARMV7L = platform.machine().lower() in ("armv7l", "armv8l")


def check_lift_manifest(session: Session):
    session.run(
        "python",
        BUILD_ROOT / "scripts" / "check-manifest.py",
        "--release",
        PBS_RELEASE,
        "--version",
        PBS_VERSION,
        "--flavor",
        PBS_FLAVOR,
        BUILD_ROOT / "lift.toml",
    )


def run_pex(session: Session, script, *args, silent=False, **env) -> Any | None:
    pex_pex = session.cache_dir / PEX_PEX
    if not pex_pex.exists():
        session.install(PEX_REQUIREMENT)
        session.run("pex", PEX_REQUIREMENT, "--venv", "--sh-boot", "-o", str(pex_pex))
        session.run("python", "-m", "pip", "uninstall", "-y", "pex")
    return session.run(
        "python",
        str(pex_pex),
        *args,
        env={"PEX_SCRIPT": script, **env},
        silent=silent,
        stderr=subprocess.DEVNULL if silent else subprocess.STDOUT,
    )


def maybe_create_lock(session: Session) -> bool:
    all_requirements = [
        BUILD_ROOT / "requirements.txt",
        *(Path(p) for p in sorted(glob.glob(str(BUILD_ROOT / "nox-support" / "*-reqs.txt")))),
    ]
    requirements_digest = hashlib.sha256()
    for requirements_file in all_requirements:
        requirements_digest.update(requirements_file.read_bytes())
    requirements_checksum = requirements_digest.hexdigest()

    create_lock = True
    lock_checksum_file = BUILD_ROOT / "nox-support" / "lock.checksums"
    if LOCK_FILE.exists() and lock_checksum_file.exists():
        try:
            checksum_data = json.loads(lock_checksum_file.read_text())
            expected_requirements_checksum = checksum_data["requirements_checksum"]
            expected_lock_checksum = checksum_data["lock_checksum"]
        except (IOError, ValueError, KeyError) as e:
            session.warn(f"Failed to load lock checksum file at {lock_checksum_file}: {e}")
        else:
            if requirements_checksum == expected_requirements_checksum:
                lock_checksum = hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest()
                if lock_checksum == expected_lock_checksum:
                    create_lock = False
                else:
                    session.warn(
                        f"Lock checksum changed from {expected_lock_checksum} to {lock_checksum}, "
                        "re-generating lock..."
                    )
            else:
                session.warn(
                    f"Requirements checksum changed from {expected_requirements_checksum} to "
                    f"{requirements_checksum}, re-generating lock..."
                )
    elif LOCK_FILE.exists():
        session.warn(
            f"The lock checksum file {lock_checksum_file} does not exist, re-generating lock..."
        )
    else:
        session.warn(f"The lock file {LOCK_FILE} does not exist, re-generating lock...")

    if create_lock:
        run_pex(
            session,
            "pex3",
            "lock",
            "create",
            *itertools.chain.from_iterable(("-r", str(req)) for req in all_requirements),
            "--interpreter-constraint",
            f"=={REQUIRES_PYTHON_VERSION}.*",
            "--style",
            "universal",
            "--pip-version",
            "latest",
            "--resolver-version",
            "pip-2020-resolver",
            "--elide-unused-requires-dist",
            "--indent",
            "2",
            "-o",
            str(LOCK_FILE),
        )
        lock_checksum = hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest()
        lock_checksum_file.write_text(
            json.dumps(
                {"requirements_checksum": requirements_checksum, "lock_checksum": lock_checksum},
                indent=2,
                sort_keys=True,
            )
        )

    for subset in all_requirements:
        subset_lock = subset.with_suffix(".windows-amd64.lock.txt")
        if not create_lock and subset_lock.exists():
            continue
        run_pex(
            session,
            "pex3",
            "lock",
            "export-subset",
            "--lock",
            str(LOCK_FILE),
            "-r",
            str(subset),
            "--complete-platform",
            str(WINDOWS_AMD64_COMPLETE_PLATFORM),
            "-o",
            str(subset_lock),
        )

    return create_lock


def install_locked_requirements(session: Session, input_reqs: Iterable[Path]) -> None:
    maybe_create_lock(session)

    if OperatingSystem.WINDOWS is OS:
        # N.B: We avoid this installation technique when not on Windows since it's a good deal
        # slower than using Pex.
        session.install(
            *itertools.chain.from_iterable(
                ("-r", str(req_file.with_suffix(".windows-amd64.lock.txt")))
                for req_file in input_reqs
            )
        )
    else:
        run_pex(
            session,
            "pex3",
            "venv",
            "create",
            "-d",
            session.virtualenv.location,
            "--lock",
            str(LOCK_FILE),
            *itertools.chain.from_iterable(("-r", str(req_file)) for req_file in input_reqs),
        )


def ensure_PBS_python_dist(target_triple: str) -> PurePath:
    pbs_root = BUILD_ROOT / ".nox" / "PBS" / PBS_RELEASE / PBS_VERSION / PBS_FLAVOR
    if not pbs_root.exists():
        url = (
            "https://github.com/indygreg/python-build-standalone/releases/download/"
            f"{PBS_RELEASE}/"
            f"cpython-{PBS_VERSION}+{PBS_RELEASE}-{target_triple}-{PBS_FLAVOR}.tar.gz"
        )
        with urllib.request.urlopen(f"{url}.sha256") as resp:
            expected_hash = resp.read().decode().strip()
        pbs_root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pbs_root.parent, prefix="PBS-prepare.") as chroot:
            with urllib.request.urlopen(url) as resp, tempfile.TemporaryFile() as archive:
                digest = hashlib.sha256()
                for chunk in iter(lambda: resp.read(io.DEFAULT_BUFFER_SIZE), b""):
                    archive.write(chunk)
                    digest.update(chunk)
                if expected_hash != (actual_hash := digest.hexdigest()):
                    raise ValueError(
                        dedent(
                            f"""\
                            The PBS archive downloaded from {url} had unexpected contents:
                            Expected sha256 hash: {expected_hash}
                            Actual sha256 hash:   {actual_hash}
                            """
                        )
                    )
                archive.flush()
                archive.seek(0)
                with tarfile.open(fileobj=archive) as tf:
                    tf.extractall(chroot)
            os.replace(chroot, str(pbs_root))
    return pbs_root


T = TypeVar("T")


def nox_session() -> Callable[[Callable[[Session], T]], Callable[[Session], T]]:
    if OperatingSystem.LINUX is OS:
        if IS_ARM64:
            target_triple = "aarch64-unknown-linux-gnu"
        elif IS_ARMV7L:
            target_triple = "armv7-unknown-linux-gnueabihf"
        else:
            target_triple = "x86_64-unknown-linux-gnu"
    elif OperatingSystem.MAC is OS:
        if IS_ARM64:
            target_triple = "aarch64-apple-darwin"
        else:
            target_triple = "x86_64-apple-darwin"
    else:  # Windows
        # N.B.: We use an x86-64 Python for Windows ARM64 because this is what we ship with via PBS,
        # and we need to be able to resolve x86-64 compatible requirements (which include native
        # deps like psutil) for our shiv.
        target_triple = "x86_64-pc-windows-msvc"

    dist_root = ensure_PBS_python_dist(target_triple=target_triple)
    if OperatingSystem.WINDOWS is OS:
        python_exe_path = dist_root / "python" / "python.exe"
    else:
        python_exe_path = dist_root / "python" / "bin" / f"python{REQUIRES_PYTHON_VERSION}"

    return nox.session(python=[str(python_exe_path)], reuse_venv=True)


@nox_session()
def lock(session: Session) -> None:
    if not maybe_create_lock(session):
        session.warn(
            f"Not updating lock file. Remove {LOCK_FILE.relative_to(BUILD_ROOT)} to force updates."
        )


NOX_SUPPORT_DIR = BUILD_ROOT / "nox-support"


def python_session(
    include_project: bool = False,
    extra_reqs: Collection[str] = (),
) -> Callable[[Callable[[Session], T]], Callable[[Session], T]]:
    def wrapped(func: Callable[[Session], T]) -> Callable[[Session], T]:
        @wraps(func)
        def wrapper(session: Session) -> T:
            requirements = []
            for req in (func.__name__, *extra_reqs):
                session_reqs = NOX_SUPPORT_DIR / f"{req}-reqs.txt"
                if req in extra_reqs or session_reqs.is_file():
                    requirements.append(session_reqs)
            if include_project:
                requirements.append(BUILD_ROOT / "requirements.txt")
            if requirements:
                install_locked_requirements(session, input_reqs=requirements)
            return func(session)

        return nox_session()(wrapper)

    return wrapped


PATHS_TO_CHECK = ["science", "tests", "test-support", "noxfile.py", "docs"]


def run_black(session: Session, *args: str) -> None:
    session.run("black", "--color", *PATHS_TO_CHECK, *args, *session.posargs)


def run_isort(session: Session, *args: str) -> None:
    session.run("isort", *PATHS_TO_CHECK, *args, *session.posargs)


def run_autoflake(session: Session, *args: str) -> None:
    session.run("autoflake", "--quiet", "--recursive", *PATHS_TO_CHECK, *args, *session.posargs)


@python_session()
def fmt(session: Session) -> None:
    run_black(session)
    run_isort(session)
    run_autoflake(session, "--remove-all-unused-imports", "--in-place")


@python_session(extra_reqs=["fmt"])
def lint(session: Session) -> None:
    run_black(session, "--check", "--diff")
    run_isort(session, "--check-only")
    run_autoflake(session, "--check")


@python_session(include_project=True, extra_reqs=["doc", "test"])
def check(session: Session) -> None:
    check_lift_manifest(session)
    session.run("mypy", "--python-version", ".".join(map(str, MIN_NOX_PYTHON)), "noxfile.py")
    session.run(
        "mypy", "--python-version", REQUIRES_PYTHON_VERSION, *PATHS_TO_CHECK, *session.posargs
    )


DIST_DIR = BUILD_ROOT / "dist"
PACKAGED: Path | None = None


def create_zipapp(session: Session) -> Path:
    global PACKAGED
    if PACKAGED is None:
        venv_dir = Path(session.create_tmp()) / "science"
        if OperatingSystem.WINDOWS is OS:
            session.run("python", "-m", "venv", str(venv_dir))
            session.run(
                str(venv_dir / "Scripts" / "python.exe"),
                "-m",
                "pip",
                "install",
                "-r",
                str(BUILD_ROOT / "requirements.windows-amd64.lock.txt"),
                external=True,
            )
            session.run(
                str(venv_dir / "Scripts" / "python.exe"),
                "-m",
                "pip",
                "uninstall",
                "--yes",
                "pip",
                external=True,
            )
            site_packages = str(venv_dir / "Lib" / "site-packages")
        else:
            run_pex(
                session,
                "pex3",
                "venv",
                "create",
                "--force",
                "-d",
                str(venv_dir),
                "-r",
                str(BUILD_ROOT / "requirements.txt"),
                "--lock",
                str(LOCK_FILE),
            )
            site_packages = json.loads(
                cast(str, run_pex(session, "pex3", "venv", "inspect", str(venv_dir), silent=True))
            )["site_packages"]

        session.run("python", "-m", "pip", "install", "--prefix", str(venv_dir), "--no-deps", ".")

        DIST_DIR.mkdir(parents=True, exist_ok=True)
        dest = DIST_DIR / "science.pyz"
        session.run(
            "shiv",
            "-p",
            f"/usr/bin/env python{REQUIRES_PYTHON_VERSION}",
            "-c",
            "science",
            "--site-packages",
            str(site_packages),
            "--reproducible",
            "-o",
            str(dest),
        )
        PACKAGED = dest.resolve()
    return PACKAGED


@python_session(include_project=True, extra_reqs=["package"])
def test(session: Session) -> None:
    science_pyz = create_zipapp(session)
    test_env = {"BUILD_ROOT": str(BUILD_ROOT), "SCIENCE_TEST_PYZ_PATH": str(science_pyz)}
    session.run("pytest", "-n" "auto", *(session.posargs or ["-v"]), env=test_env)


def _run_sphinx(session: Session, builder_name: str) -> Path:
    docs_dir = BUILD_ROOT / "docs"
    build_dir = docs_dir / "build" / builder_name
    shutil.rmtree(build_dir, ignore_errors=True)
    session.run("sphinx-build", "-b", builder_name, "-aEW", str(docs_dir), str(build_dir))
    return build_dir


@python_session(include_project=True)
def doc(session: Session) -> Path:
    return _run_sphinx(session, builder_name="html")


@python_session(include_project=True, extra_reqs=["doc"])
def linkcheck(session: Session) -> None:
    _run_sphinx(session, builder_name="linkcheck")


@python_session()
def run(session: Session) -> None:
    science_pyz = create_zipapp(session)
    doc_path = doc(session)
    session.run(
        "python", str(science_pyz), *session.posargs, env={"SCIENCE_DOC_LOCAL": str(doc_path)}
    )


def _package(session: Session, docsite: Path, *extra_lift_args: str) -> None:
    science_pyz = create_zipapp(session)
    session.run(
        "python",
        str(science_pyz),
        "lift",
        "--file",
        f"science.pyz={science_pyz}",
        "--file",
        f"docsite={docsite}",
        "--include-provenance",
        *extra_lift_args,
        "build",
        "--hash",
        "sha256",
        "--use-platform-suffix",
        env={
            "SCIENCE_LIFT_BUILD_DEST_DIR": os.environ.get(
                "SCIENCE_LIFT_BUILD_DEST_DIR", str(DIST_DIR)
            )
        },
    )


@python_session(include_project=True, extra_reqs=["doc"])
def package(session: Session) -> None:
    docsite = _run_sphinx(session, builder_name="html")
    _package(session, docsite)
    _package(session, docsite, "--invert-lazy", "cpython", "--app-name", "science-fat")
