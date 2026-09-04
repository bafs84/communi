from __future__ import annotations

"""Small command dispatcher for the three CKW workflows."""

import argparse
import sys

from .vzev_invoice_documents import main as invoice_main
from .vzev_operator_overview import main as overview_main


def _serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ckw serve", description="Start the CKW web portal.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("ckw_script.portal_api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _print_help() -> None:
    print(
        "CKW Energieportal und vZEV-Abrechnung\n\n"
        "Befehle:\n"
        "  ckw serve [--reload]        Webportal starten\n"
        "  ckw invoices CSV [Optionen] Editierbare Teilnehmerrechnungen erstellen\n"
        "  ckw overview CSV [Optionen] Betreiberübersicht erstellen\n\n"
        "Mit 'ckw <Befehl> --help' werden alle Optionen angezeigt."
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        _print_help()
        return 0

    command, command_args = args[0], args[1:]
    if command == "serve":
        return _serve(command_args)
    if command == "invoices":
        return invoice_main(command_args)
    if command == "overview":
        return overview_main(command_args)

    print(f"Unbekannter Befehl: {command}\n", file=sys.stderr)
    _print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
