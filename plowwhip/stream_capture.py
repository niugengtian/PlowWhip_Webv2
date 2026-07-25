from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO


if __package__:
    from .secret_policy import redact_secret
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from plowwhip.secret_policy import redact_secret


def append_redacted_stream(source: BinaryIO, destination: Path) -> None:
    """Append complete redacted records without rewriting a Cold segment."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.touch(mode=0o600, exist_ok=True)
    destination.chmod(0o600)
    with destination.open("ab", buffering=0) as output:
        while True:
            record = source.readline()
            if not record:
                return
            output.write(redact_secret(record.decode(errors="replace")).encode())


def _pump(source: BinaryIO | None, destination: Path) -> None:
    if source is None:
        return
    try:
        append_redacted_stream(source, destination)
    finally:
        source.close()


def _parse_args(argv: list[str]) -> tuple[Path, Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    values, command = parser.parse_known_args(argv)
    if not command or command[0] != "--" or len(command) == 1:
        raise ValueError("stream capture requires a command after --")
    return Path(values.stdout), Path(values.stderr), command[1:]


def main(argv: list[str] | None = None) -> int:
    stdout_path, stderr_path, command = _parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=False,
    )
    stdout_thread = threading.Thread(
        target=_pump, args=(process.stdout, stdout_path), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump, args=(process.stderr, stderr_path), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    if process.stdin is not None:
        try:
            while True:
                chunk = sys.stdin.buffer.read(65_536)
                if not chunk:
                    break
                process.stdin.write(chunk)
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
