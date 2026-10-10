"""Extraction checks and the desktop workflow command."""

import argparse
import json
import os
import platform
import shutil
import subprocess

from .extract import extract_order, runtime_versions


def doctor():
    tesseract = shutil.which("tesseract")
    checks = [{"name": "python", "passed": tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 11),
               "observed": platform.python_version()}]
    try:
        installed = subprocess.run([tesseract, "--version"], capture_output=True, text=True,
                                   check=True, timeout=5).stdout.splitlines()[0] if tesseract else None
        languages = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True,
                                   check=True, timeout=5).stdout.splitlines() if tesseract else []
        checks.append({"name": "tesseract_english", "passed": "eng" in languages,
                       "path": tesseract, "observed": installed})
    except (OSError, subprocess.SubprocessError):
        checks.append({"name": "tesseract_english", "passed": False, "path": tesseract})
    versions = runtime_versions()
    checks += [{"name": name, "passed": versions[name] is not None, "observed": versions[name]}
               for name in ("pydantic", "Pillow", "numpy")]
    return {"status": "ready" if all(c["passed"] for c in checks) else "failed",
            "scope": "extraction dependencies only; desktop readiness is checked when running the workflow",
            "checks": checks, "optional_openai_sdk": versions["openai"]}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read order images and run the Fakturama workflow.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check extraction dependencies")
    extract = sub.add_parser("extract", help="Preserve, transcribe and validate an order image")
    extract.add_argument("image")
    extract.add_argument("--output", default="runs", help="Parent of a new evidence directory per invocation")
    extract.add_argument("--extractor", choices=["ocr", "openai"], default="ocr")
    extract.add_argument("--name-order", choices=["given-family"],
                         help="Treat a two-part contact name as first name followed by last name")
    extract.add_argument("--model", help="Required for OpenAI, or set OPENAI_MODEL")
    run = sub.add_parser("run", help="Validate an image and run the guarded Fakturama workflow")
    run.add_argument("image")
    run.add_argument("--workspace", required=True, help="Fakturama working directory")
    run.add_argument("--output", default="runs", help="Parent of a new evidence directory per invocation")
    run.add_argument("--extractor", choices=["ocr", "openai"], default="ocr")
    run.add_argument("--name-order", choices=["given-family"],
                     help="Treat a two-part contact name as first name followed by last name")
    run.add_argument("--model", help="Required for OpenAI, or set OPENAI_MODEL")
    run.add_argument("--workarounds", choices=["fakturama-2.2.0"],
                     help="Use the 2.2.0 payment/setup workarounds in a new empty workspace")
    run.add_argument("--demonstration", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--original-image",
                     help="Original image retained for comparison when using --workarounds")
    run.add_argument("--allow-demo-payment-sequence", action="store_true", help=argparse.SUPPRESS)
    demo_environment = run.add_mutually_exclusive_group()
    demo_environment.add_argument('--fresh-demo', action='store_true', help=argparse.SUPPRESS)
    demo_environment.add_argument('--prepared-empty-demo', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.workarounds:
            if not args.original_image:
                parser.error("--workarounds requires --original-image")
            if args.fresh_demo:
                parser.error("--workarounds uses a prepared workspace; omit --fresh-demo")
            args.demonstration = True
            args.allow_demo_payment_sequence = True
            args.prepared_empty_demo = True
        if not args.workspace.strip():
            parser.error("--workspace must name a Fakturama working directory")
        if args.demonstration and not args.original_image:
            parser.error("--demonstration requires --original-image")
        if args.original_image and not args.demonstration:
            parser.error("--original-image requires --demonstration")
        if args.allow_demo_payment_sequence and not args.demonstration:
            parser.error("--allow-demo-payment-sequence requires --demonstration")
        if args.fresh_demo and not args.demonstration:
            parser.error('--fresh-demo requires --demonstration')
        if args.prepared_empty_demo and not args.demonstration:
            parser.error('--prepared-empty-demo requires --demonstration')
    try:
        if args.command == "doctor":
            report = doctor()
        elif args.command == "extract":
            report = extract_order(
                args.image, output_dir=args.output, extractor=args.extractor,
                name_order=args.name_order, model=args.model or os.environ.get("OPENAI_MODEL"))
        else:
            # Extraction and doctor remain usable without optional desktop dependencies.
            from .workflow import run_image

            options = {'fresh_demo': True} if args.fresh_demo else {}
            if args.prepared_empty_demo:
                options['prepared_empty_demo'] = True
            report = run_image(
                args.image, workspace=args.workspace, output_dir=args.output,
                extractor=args.extractor, name_order=args.name_order,
                model=args.model or os.environ.get("OPENAI_MODEL"),
                demonstration=args.demonstration, original_image=args.original_image,
                allow_demo_payment_sequence=args.allow_demo_payment_sequence, **options)
    except OSError as exc:
        report = {"status": "failed", "issues": [{"code": "evidence_write",
                  "message": f"Cannot write the evidence directory ({type(exc).__name__}). Choose a writable --output."}]}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    successful = {"verified"} if args.command == "run" else {"ready", "validated"}
    return 0 if report["status"] in successful else 2


if __name__ == "__main__":
    raise SystemExit(main())
