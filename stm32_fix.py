#!/usr/bin/env python3
"""
STM32 project fix script for macOS.

Run this from any STM32CubeMX project directory generated for VS Code to fix
the common issues when using Homebrew arm-none-eabi-gcc:

  1. Updates .stm32env (used by STM32Make.make / STM32-for-VSCode extension)
     to point ARM_GCC_PATH at an ARM GNU Toolchain that bundles newlib.

  2. Patches the CubeMX-generated Makefile so the -isystem flag points at a
     gcc that actually has a sysroot (so #include_next <stdint.h> resolves).

  3. Patches .vscode/c_cpp_properties.json so IntelliSense uses the correct
     compiler and finds all headers (eliminates red squiggles).
"""

import json
import re
import sys
import glob
import subprocess
from pathlib import Path

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


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
# .vscode/c_cpp_properties.json patching
# ---------------------------------------------------------------------------

def _collect_include_paths(project_dir: Path) -> list[str]:
    """
    Read C_INCLUDES from STM32Make.make (or Makefile) and return them as
    ${workspaceFolder}-prefixed strings suitable for c_cpp_properties.json.
    """
    for name in ("STM32Make.make", "Makefile", "makefile"):
        p = project_dir / name
        if not p.exists():
            continue
        content = p.read_text()
        # Extract -Ifoo/bar paths from C_INCLUDES block
        paths = re.findall(r"-I(\S+)", content)
        if paths:
            return [f"${{workspaceFolder}}/{path.lstrip('/')}" for path in paths]
    return []


def _collect_defines(project_dir: Path) -> list[str]:
    for name in ("STM32Make.make", "Makefile", "makefile"):
        p = project_dir / name
        if not p.exists():
            continue
        content = p.read_text()
        defines = re.findall(r"-D(\S+)", content)
        if defines:
            # deduplicate while preserving order
            seen: set[str] = set()
            result = []
            for d in defines:
                if d not in seen:
                    seen.add(d)
                    result.append(d)
            return result
    return []


def patch_stm32_config_yaml(project_dir: Path, gcc_bin_dir: Path) -> None:
    """
    Add the compiler's system include paths to STM32-for-VSCode.config.yaml
    so the extension carries them into every c_cpp_properties.json regeneration.
    Falls back to regex editing when the PyYAML library is not installed.
    """
    yaml_path = project_dir / "STM32-for-VSCode.config.yaml"
    if not yaml_path.exists():
        print("  STM32-for-VSCode.config.yaml: not found – skipping.")
        return

    system_paths = _system_include_paths(gcc_bin_dir)
    if not system_paths:
        print("  STM32-for-VSCode.config.yaml: could not detect system paths – skipping.")
        return

    content = yaml_path.read_text()

    # Check which paths are already present and only add missing ones.
    to_add = [p for p in system_paths if p not in content]
    if not to_add:
        print("  STM32-for-VSCode.config.yaml: system paths already present, no change needed.")
        return

    if _YAML_AVAILABLE:
        data = yaml.safe_load(content)
        dirs = data.get("includeDirectories") or []
        for p in to_add:
            if p not in dirs:
                dirs.append(p)
        data["includeDirectories"] = dirs
        yaml_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    else:
        # Regex fallback: append entries under the includeDirectories block.
        new_entries = "\n".join(f"  - {p}" for p in to_add)
        content = re.sub(
            r"(includeDirectories\s*:\s*\n(?:[ \t]+-[^\n]*\n)*)",
            lambda m: m.group(0) + new_entries + "\n",
            content,
        )
        yaml_path.write_text(content)

    print(f"  STM32-for-VSCode.config.yaml: added {len(to_add)} system include path(s).")


def _system_include_paths(gcc_bin_dir: Path) -> list[str]:
    """
    Ask the compiler itself which system include directories it uses, then
    return them as absolute path strings. This is the only reliable way to
    find newlib and GCC-internal headers for a cross-compiler.
    """
    try:
        result = subprocess.run(
            [str(gcc_bin_dir / "arm-none-eabi-gcc"),
             "-mcpu=cortex-m3", "-mthumb", "-E", "-x", "c", "-", "-v"],
            input="", capture_output=True, text=True, timeout=10
        )
        # The paths appear between "#include <...> search starts here:" and
        # "End of search list." in stderr
        stderr = result.stderr
        start = stderr.find("#include <...> search starts here:")
        end = stderr.find("End of search list.")
        if start == -1 or end == -1:
            return []
        block = stderr[start:end]
        paths = []
        for line in block.splitlines()[1:]:
            line = line.strip()
            p = Path(line).resolve()
            if p.is_dir() and str(p) not in paths:
                paths.append(str(p))
        return paths
    except Exception:
        return []


def patch_c_cpp_properties(project_dir: Path, gcc_bin_dir: Path) -> None:
    vscode_dir = project_dir / ".vscode"
    props_path = vscode_dir / "c_cpp_properties.json"

    gcc_path = str(gcc_bin_dir / "arm-none-eabi-gcc")
    include_paths = _collect_include_paths(project_dir)
    defines = _collect_defines(project_dir)

    # Fall back to a known-good set if the Makefile parse came up empty
    if not include_paths:
        include_paths = [
            "${workspaceFolder}/Core/Inc",
            "${workspaceFolder}/Drivers/CMSIS/Device/ST/STM32F1xx/Include",
            "${workspaceFolder}/Drivers/CMSIS/Include",
            "${workspaceFolder}/Drivers/STM32F1xx_HAL_Driver/Inc",
            "${workspaceFolder}/Drivers/STM32F1xx_HAL_Driver/Inc/Legacy",
            "${workspaceFolder}/Middlewares/ST/STM32_USB_Device_Library/Class/CDC/Inc",
            "${workspaceFolder}/Middlewares/ST/STM32_USB_Device_Library/Core/Inc",
            "${workspaceFolder}/USB_DEVICE/App",
            "${workspaceFolder}/USB_DEVICE/Target",
        ]
    if not defines:
        defines = ["STM32F103xB", "USE_HAL_DRIVER"]

    # Always append the compiler's own system headers — VS Code doesn't reliably
    # extract these from cross-compilers on its own.
    system_paths = _system_include_paths(gcc_bin_dir)
    if system_paths:
        include_paths = include_paths + system_paths
        print(f"  c_cpp_properties.json: added {len(system_paths)} system include paths.")
    else:
        print(f"  c_cpp_properties.json: WARNING – could not detect system include paths.")

    new_config = {
        "name": "STM32",
        "includePath": include_paths,
        "defines": defines,
        "compilerPath": gcc_path,
        "compilerArgs": ["-mcpu=cortex-m3", "-mthumb"],
        "intelliSenseMode": "gcc-arm",
        "cStandard": "c11",
        "cppStandard": "c++14",
    }

    if props_path.exists():
        try:
            data = json.loads(props_path.read_text())
        except json.JSONDecodeError:
            data = {"configurations": [], "version": 4}
    else:
        vscode_dir.mkdir(exist_ok=True)
        data = {"configurations": [], "version": 4}

    # Replace or add the STM32 configuration
    configs = data.get("configurations", [])
    for i, cfg in enumerate(configs):
        if cfg.get("name") == "STM32":
            configs[i] = new_config
            print(f"  c_cpp_properties.json: updated STM32 configuration.")
            break
    else:
        configs.append(new_config)
        print(f"  c_cpp_properties.json: added STM32 configuration.")

    data["configurations"] = configs
    props_path.write_text(json.dumps(data, indent=2) + "\n")


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

    # --- Patch STM32-for-VSCode.config.yaml (survives extension regeneration) ---
    patch_stm32_config_yaml(project_dir, chosen_bin_dir)

    # --- Patch .vscode/c_cpp_properties.json (immediate effect) ---
    patch_c_cpp_properties(project_dir, chosen_bin_dir)

    print("\nDone. Reload VS Code (Cmd+Shift+P → 'Developer: Reload Window') to pick up IntelliSense changes.")
    print("To build: make -f STM32Make.make -j8 DEBUG=1")


if __name__ == "__main__":
    main()
