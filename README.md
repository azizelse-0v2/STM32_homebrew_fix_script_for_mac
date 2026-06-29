STM32 project fix script for macOS.

Run this from any STM32CubeMX project directory generated for VS Code to fix
the two most common build failures when using Homebrew arm-none-eabi-gcc:

  1. Updates .stm32env (used by STM32Make.make / STM32-for-VSCode extension)
     to point ARM_GCC_PATH at an ARM GNU Toolchain that bundles newlib.

  2. Patches the CubeMX-generated Makefile so the -isystem flag points at a
     gcc that actually has a sysroot (so #include_next <stdint.h> resolves).