#!/usr/bin/env python3
"""
STM32 project fix script for macOS.

Run this from any STM32CubeMX project directory generated for VS Code to fix
the two most common build failures when using Homebrew arm-none-eabi-gcc:

  1. Updates .stm32env (used by STM32Make.make / STM32-for-VSCode extension)
     to point ARM_GCC_PATH at an ARM GNU Toolchain that bundles newlib.

  2. Patches the CubeMX-generated Makefile so the -isystem flag points at a
     gcc that actually has a sysroot (so #include_next <stdint.h> resolves).
"""

import os
import re
import sys
import glob
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Toolchain discovery
# ---------------------------------------------------------------------------

def _gcc_has_sysroot(gcc_bin: Path) -> bool:
    """Return True if this gcc reports a non-empty sysroot."""
    try:
        result = subprocess.run(
            [str(gcc_bin), "-print-sysroot"],
            capture_output=True, text=True, timeout=5
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def find_arm_toolchains() -> list[tuple[str, Path]]:
    """
    Return a list of (label, bin_dir) tuples for every arm-none-eabi-gcc
    found on the system, best first (official ARM releases before Homebrew).
    """
    found: list[tuple[str, Path]] = []

    # Official ARM GNU Toolchain – installed by the .pkg from developer.arm.com
    arm_base = Path("/Applications/ArmGNUToolchain")
    if arm_base.is_dir():
        for version_dir in sorted(arm_base.iterdir(), reverse=True):
            gcc = version_dir / "arm-none-eabi" / "bin" / "arm-none-eabi-gcc"
            if gcc.is_file():
                found.append((f"ARM GNU Toolchain {version_dir.name}", gcc.parent))

    # STM32CubeIDE ships its own bundled toolchain
    cubeide_pattern = (
        "/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/"
        "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*"
        "/tools/bin"
    )
    for path_str in glob.glob(cubeide_pattern):
        gcc = Path(path_str) / "arm-none-eabi-gcc"
        if gcc.is_file():
            found.append(("STM32CubeIDE bundled toolchain", Path(path_str)))

    # Homebrew – listed last because it lacks newlib
    homebrew_gcc = Path("/opt/homebrew/bin/arm-none-eabi-gcc")
    if homebrew_gcc.is_file():
        label = "Homebrew arm-none-eabi-gcc (WARNING: may lack newlib)"
        found.append((label, homebrew_gcc.parent))

    return found


# ---------------------------------------------------------------------------
# .stm32env patching
# ---------------------------------------------------------------------------

def patch_stm32env(env_path: Path, gcc_bin_dir: Path) -> None:
    content = env_path.read_text()
    new_line = f"ARM_GCC_PATH = {gcc_bin_dir}"

    if re.search(r"^ARM_GCC_PATH\s*=", content, re.MULTILINE):
        old = re.search(r"^ARM_GCC_PATH\s*=.*$", content, re.MULTILINE).group()
        if old == new_line:
            print(f"  .stm32env: ARM_GCC_PATH already correct, no change needed.")
            return
        content = re.sub(r"^ARM_GCC_PATH\s*=.*$", new_line, content, flags=re.MULTILINE)
        print(f"  .stm32env: updated ARM_GCC_PATH → {gcc_bin_dir}")
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"
        print(f"  .stm32env: added ARM_GCC_PATH = {gcc_bin_dir}")

    env_path.write_text(content)


# ---------------------------------------------------------------------------
# Makefile patching
# ---------------------------------------------------------------------------

ISYSTEM_RE = re.compile(
    r"^CFLAGS \+= -isystem \$\(shell [^\n]+\)/include[ \t]*$",
    re.MULTILINE,
)

def _isystem_line(gcc_bin_dir: Path) -> str:
    return f"CFLAGS += -isystem $(shell {gcc_bin_dir}/arm-none-eabi-gcc -print-sysroot)/include"


def patch_makefile(makefile_path: Path, gcc_bin_dir: Path) -> None:
    content = makefile_path.read_text()
    new_line = _isystem_line(gcc_bin_dir)

    # Case 1: line already exists – update in place
    if ISYSTEM_RE.search(content):
        existing = ISYSTEM_RE.search(content).group()
        if existing == new_line:
            print(f"  Makefile: -isystem flag already correct, no change needed.")
            return
        content = ISYSTEM_RE.sub(new_line, content)
        print(f"  Makefile: updated -isystem line.")
        makefile_path.write_text(content)
        return

    # Case 2: insert after the main CFLAGS += ... -ffunction-sections line
    main_cflags_re = re.compile(
        r"^(CFLAGS \+= .+-ffunction-sections[ \t]*)$",
        re.MULTILINE,
    )
    m = main_cflags_re.search(content)
    if m:
        insert_after = m.end()
        content = content[:insert_after] + f"\n{new_line}" + content[insert_after:]
        print(f"  Makefile: inserted -isystem line after main CFLAGS definition.")
        makefile_path.write_text(content)
        return

    # Case 3: fallback – append before the debug ifeq block
    debug_re = re.compile(r"^ifeq \(\$\(DEBUG\),\s*1\)", re.MULTILINE)
    m = debug_re.search(content)
    if m:
        content = content[:m.start()] + f"{new_line}\n\n" + content[m.start():]
        print(f"  Makefile: inserted -isystem line before debug block.")
        makefile_path.write_text(content)
        return

    print(f"  Makefile: WARNING – could not find an insertion point. Add manually:")
    print(f"    {new_line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    project_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Project directory: {project_dir}\n")

    # --- Discover toolchains ---
    toolchains = find_arm_toolchains()
    if not toolchains:
        print("ERROR: No arm-none-eabi-gcc toolchains found on this machine.")
        print("Download the ARM GNU Toolchain from:")
        print("  https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads")
        sys.exit(1)

    print("Available toolchains (best first):")
    for i, (label, path) in enumerate(toolchains):
        sysroot_ok = _gcc_has_sysroot(path / "arm-none-eabi-gcc")
        status = "OK" if sysroot_ok else "NO SYSROOT"
        print(f"  [{i}] [{status}] {label}")
        print(f"       {path}")
    print()

    # Default to index 0 (best available)
    default_idx = 0
    if len(toolchains) == 1:
        idx = 0
    else:
        raw = input(f"Select toolchain [default {default_idx}]: ").strip()
        idx = int(raw) if raw.isdigit() and int(raw) < len(toolchains) else default_idx

    chosen_label, chosen_bin_dir = toolchains[idx]
    print(f"\nUsing: {chosen_label}\n")

    # --- Patch .stm32env ---
    env_path = project_dir / ".stm32env"
    if env_path.exists():
        patch_stm32env(env_path, chosen_bin_dir)
    else:
        print(f"  .stm32env: not found – skipping "
              f"(only needed for the STM32-for-VSCode extension)")

    # --- Patch Makefile ---
    makefile_path = None
    for name in ("Makefile", "makefile"):
        candidate = project_dir / name
        if candidate.exists():
            makefile_path = candidate
            break

    if makefile_path:
        patch_makefile(makefile_path, chosen_bin_dir)
    else:
        print("  Makefile: not found – skipping")

    print("\nDone. Run your build task in VS Code or: make -f STM32Make.make -j8 DEBUG=1")


if __name__ == "__main__":
    main()
