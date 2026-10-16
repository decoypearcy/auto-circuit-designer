"""The only module that knows kicad-cli flags.

Flag sources (checked 2026-10-16):
  * pcb drc, pcb export gerbers/drill/pos: KiCad 10 CLI reference
    https://docs.kicad.org/10.0/en/cli/cli.html (confirmed).
  * sch erc, sch export bom: the K10 page body could not be read through the
    fetch tool, so the KiCad 9 form is used and marked VERIFY-K10.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .config import KICAD_TIMEOUT_S, AcdError


class KiCadError(AcdError):
    pass


class KiCadCli:
    def __init__(self, cli: str, timeout: int = KICAD_TIMEOUT_S):
        self.cli = cli
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------
    def _command(self, args: Sequence[str]) -> list[str]:
        cli = self.cli
        # A .py "cli" (the test double) is run through this interpreter so the
        # same code path works on Windows, where scripts have no shebang.
        if cli.lower().endswith(".py"):
            return [sys.executable, cli, *args]
        return [cli, *args]

    def run(self, args: Sequence[str]) -> str:
        cmd = self._command([str(a) for a in args])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AcdError(
                f"kicad-cli not found at {self.cli!r} (set ACD_KICAD_CLI)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise KiCadError(
                f"kicad-cli timed out after {self.timeout}s: {' '.join(cmd[-4:])}"
            ) from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise KiCadError(
                f"kicad-cli exited with code {proc.returncode} "
                f"({' '.join(str(a) for a in args[:3])}): {tail}"
            )
        return proc.stdout

    # -- commands ---------------------------------------------------------
    def version(self) -> str:
        return self.run(["--version"]).strip()

    def erc(self, sch: Path, out_json: Path) -> None:
        # VERIFY-K10: sch erc flags taken from the KiCad 9 docs.
        self.run([
            "sch", "erc",
            "--format", "json",
            "--severity-all",
            "--units", "mm",
            "-o", out_json,
            sch,
        ])

    def drc(self, pcb: Path, out_json: Path) -> None:
        # Confirmed against the KiCad 10 CLI reference.
        self.run([
            "pcb", "drc",
            "--format", "json",
            "--schematic-parity",
            "--severity-all",
            "--units", "mm",
            "-o", out_json,
            pcb,
        ])

    def bom(self, sch: Path, out_csv: Path, *, fields: Sequence[str],
            labels: Sequence[str], group_by: Sequence[str] = (),
            sort_field: str = "Reference", exclude_dnp: bool = False) -> None:
        # VERIFY-K10: sch export bom flags taken from the KiCad 9 docs.
        args: list[object] = [
            "sch", "export", "bom",
            "--fields", ",".join(fields),
            "--labels", ",".join(labels),
            "--sort-field", sort_field,
            "--sort-asc",
            "--field-delimiter", ",",
            "--string-delimiter", '"',
        ]
        if group_by:
            args += ["--group-by", ",".join(group_by)]
        if exclude_dnp:
            args.append("--exclude-dnp")
        args += ["-o", out_csv, sch]
        self.run(args)

    def gerbers(self, pcb: Path, out_dir: Path, *, layers: Sequence[str],
                x2: bool, protel_ext: bool) -> None:
        # Confirmed against the KiCad 10 CLI reference.
        args: list[object] = ["pcb", "export", "gerbers", "-l", ",".join(layers)]
        if not x2:
            args.append("--no-x2")
        if not protel_ext:
            args.append("--no-protel-ext")
        args += ["-o", out_dir, pcb]
        self.run(args)

    def drill(self, pcb: Path, out_dir: Path, *, units: str = "mm",
              separate_th: bool = True) -> None:
        # Confirmed against the KiCad 10 CLI reference.
        args: list[object] = [
            "pcb", "export", "drill",
            "--format", "excellon",
            "--drill-origin", "absolute",
            "--excellon-units", units,
        ]
        if separate_th:
            args.append("--excellon-separate-th")
        args += ["-o", out_dir, pcb]
        self.run(args)

    def pos(self, pcb: Path, out_csv: Path) -> None:
        # Confirmed against the KiCad 10 CLI reference.
        self.run([
            "pcb", "export", "pos",
            "--format", "csv",
            "--units", "mm",
            "--side", "both",
            "--exclude-dnp",
            "-o", out_csv,
            pcb,
        ])
