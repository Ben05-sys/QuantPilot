#!/usr/bin/env python3
"""PilotMarkets launcher.

    python pilot.py                    # terminal at http://127.0.0.1:8900
    python pilot.py --refresh          # rebuild the snapshot, then exit
    python pilot.py --refresh --limit 500   # top 500 by cap (quick)
    python pilot.py --install-shortcut # Desktop + Start Menu shortcuts
    python pilot.py --port 9000
    python pilot.py --no-browser
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def refresh(args) -> int:
    """Rebuild the snapshot from the terminal, with a progress bar."""
    from app import store, universe

    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        if i + 1 < len(args):
            limit = int(args[i + 1])

    width, started = 44, time.time()

    def progress(message: str, frac: float) -> None:
        filled = int(width * max(0.0, min(1.0, frac)))
        bar = "#" * filled + "-" * (width - filled)
        sys.stdout.write(f"\r  [{bar}] {frac * 100:5.1f}%  {message[:44]:<44}")
        sys.stdout.flush()

    print(f"\n  PilotMarkets — rebuilding the universe"
          f"{f' (top {limit})' if limit else ''}\n")
    try:
        result = universe.refresh(progress=progress, limit=limit)
    except Exception as exc:  # noqa: BLE001
        print(f"\n\n  refresh failed: {exc}\n")
        return 1
    store.prune_snapshots(keep=90)
    print(f"\n\n  {result['rows']} symbols, {result['enriched']} enriched, "
          f"{time.time() - started:.1f}s")
    print(f"  written to {store.config.db_path()}\n")
    return 0


def install_shortcut(desktop_only: bool = False) -> int:
    """Put PilotMarkets on the Desktop and in the Start Menu.

    Built through WScript.Shell rather than a .lnk written by hand, so
    Windows gets a real shortcut with an icon and a working directory. No
    pywin32 needed — PowerShell is already there.
    """
    import os
    import subprocess

    root = Path(__file__).resolve().parent
    icon = root / "assets" / "pilotmarkets.ico"
    if not icon.is_file():
        print("  building the icon first…")
        subprocess.run([sys.executable, str(root / "tools" / "make_icon.py")],
                       check=False)

    # pythonw.exe runs without a console window, which is what you want for
    # something that lives in the tray.
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    target = pyw if pyw.is_file() else exe

    targets = [Path(os.path.expanduser("~/Desktop")) / "PilotMarkets.lnk"]
    if not desktop_only:
        start = Path(os.environ.get("APPDATA", "")) / (
            "Microsoft/Windows/Start Menu/Programs/PilotMarkets.lnk")
        if start.parent.is_dir():
            targets.append(start)

    made = []
    for link in targets:
        ps = (
            "$s = New-Object -ComObject WScript.Shell; "
            f"$l = $s.CreateShortcut('{link}'); "
            f"$l.TargetPath = '{target}'; "
            f"$l.Arguments = '\"{root / 'pilot.py'}\"'; "
            f"$l.WorkingDirectory = '{root}'; "
            f"$l.IconLocation = '{icon}'; "
            "$l.Description = 'PilotMarkets — local market terminal'; "
            "$l.Save()"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True)
        if result.returncode == 0 and link.is_file():
            made.append(link)
        else:
            print(f"  could not create {link}: "
                  f"{(result.stderr or '').strip()[:120]}")

    if not made:
        print("\n  no shortcuts created.\n")
        return 1
    print("\n  created:")
    for link in made:
        print(f"    {link}")
    print(f"\n  runs: {target.name} pilot.py")
    print("  double-click it, or press Ctrl+Alt+M once it's running.\n")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--refresh" in args:
        return refresh(args)
    if "--install-shortcut" in args:
        return install_shortcut("--desktop-only" in args)
    from app.server import main as web_main
    return web_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
