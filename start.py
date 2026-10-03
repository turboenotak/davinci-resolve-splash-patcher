#!/usr/bin/env python3
"""Resolve Splash Patcher launcher for Windows and Linux.

Detects the operating system, checks Python and Pillow, then starts splash_patcher.py:
  Windows  Pillow is installed for the current user; the interface starts without a console window.
  Linux    Pillow is installed into a local .venv (system Python stays untouched, PEP 668).
Any command line option (--apply, --auto, --restore, --check, ...) is passed on to the patcher and
runs in the console.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "splash_patcher.py")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
VENV = os.path.join(HERE, ".venv")
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def fail(msg):
    print(msg, file=sys.stderr)
    if IS_WIN:
        input("Press Enter to close...")      # a double-clicked window would vanish otherwise
    sys.exit(1)


def has_pillow(python):
    return subprocess.run([python, "-c", "import PIL"], capture_output=True).returncode == 0


def venv_python():
    sub = ("Scripts", "python.exe") if IS_WIN else ("bin", "python")
    return os.path.join(VENV, *sub)


def ensure_pillow():
    """Returns the Python interpreter that has Pillow."""
    if has_pillow(sys.executable):
        return sys.executable
    if IS_LINUX:
        py = venv_python()
        if not os.path.isfile(py):
            print("Creating .venv and installing Pillow...")
            if subprocess.run([sys.executable, "-m", "venv", VENV]).returncode != 0:
                fail("Could not create a virtual environment.\n"
                     "Install it with: sudo apt install python3-venv   (Arch: it is part of python)")
        if not has_pillow(py):
            if subprocess.run([py, "-m", "pip", "install", "-r", REQUIREMENTS]).returncode != 0:
                fail("Failed to install Pillow.")
        return py
    print("Installing Pillow...")
    if subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-r", REQUIREMENTS]).returncode != 0:
        fail("Failed to install Pillow.")
    return sys.executable


def windowless(python):
    """Windows: pythonw.exe next to python.exe runs the interface without a console window."""
    cand = os.path.join(os.path.dirname(python), "pythonw.exe")
    return cand if os.path.isfile(cand) else python


def main():
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 or newer is required (found {sys.version.split()[0]}).")
    if not (IS_WIN or IS_LINUX):
        fail(f"Unsupported platform: {sys.platform}. Windows and Linux are supported.")
    if not os.path.isfile(SCRIPT):
        fail(f"Not found: {SCRIPT}")

    python = ensure_pillow()
    args = sys.argv[1:]
    cli = any(a.startswith("--") and a != "--no-window" for a in args)

    if IS_WIN and not cli:
        # interface: detached, no console window
        subprocess.Popen([windowless(python), SCRIPT, *args], cwd=HERE, close_fds=True,
                         creationflags=0x00000008 | 0x00000200)      # DETACHED_PROCESS | NEW_PROCESS_GROUP
        return 0
    if IS_WIN:
        return subprocess.call([python, SCRIPT, *args], cwd=HERE)
    os.chdir(HERE)
    os.execv(python, [python, SCRIPT, *args])      # Linux: replace this process


if __name__ == "__main__":
    sys.exit(main())
