"""Measure parser-retained inline image payloads; never decode image pixels."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import tempfile
from typing import Any

import pypdf
from pypdf import PdfReader
from pypdf.generic import ContentStream, DictionaryObject, NullObject

VERSION = "0.2.1"


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 2000
    max_images: int = 10000
    max_form_depth: int = 32
    max_form_invocations: int = 5000


class ScanLimit(Exception):
    """A caller-selected limit prevents complete measurement."""


def color_components(color: Any, resources: dict | None = None) -> int | None:
    """Resolve known device/Indexed spaces, without decoding profiles or pixels."""
    spaces = None
    seen = set()
    for _ in range(32):
        if hasattr(color, "get_object"):
            color = color.get_object()
        if isinstance(color, list):
            return 1 if color and str(color[0]) in ("/I", "/Indexed") else None
        if not isinstance(color, str):
            return None
        channels = {
            "/G": 1, "/DeviceGray": 1, "/RGB": 3, "/DeviceRGB": 3,
            "/CMYK": 4, "/DeviceCMYK": 4,
        }.get(color)
        if channels is not None:
            return channels
        if color in seen:
            return None
        if spaces is None:
            spaces = (resources or {}).get("/ColorSpace", {})
            if hasattr(spaces, "get_object"):
                spaces = spaces.get_object()
        if not isinstance(spaces, dict):
            return None
        seen.add(color)
        color = spaces.get(color)
    return None


def sample_estimate(settings: dict, resources: dict | None = None) -> tuple[int | None, str | None]:
    """Packed samples including row padding, not a measured decoded size."""
    width = settings.get("/W", settings.get("/Width"))
    height = settings.get("/H", settings.get("/Height"))
    # pypdf.BooleanObject(False) is truthy as a Python object; inspect its value.
    raw_mask = settings.get("/IM", settings.get("/ImageMask", False))
    mask = raw_mask is True or getattr(raw_mask, "value", None) is True
    bits = settings.get("/BPC", settings.get("/BitsPerComponent", 1 if mask else None))
    if mask and bits != 1:
        return None, "contradictory_image_mask_bits"
    color = settings.get("/CS", settings.get("/ColorSpace"))
    channels = 1 if mask else color_components(color, resources)
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
           for v in (width, height, bits)):
        return None, "missing_or_invalid_dimensions_or_bits"
    if bits not in (1, 2, 4, 8, 16):
        return None, "unsupported_bits_per_component"
    if channels is None:
        return None, "unresolved_or_unsupported_color_space"
    return height * ((width * channels * bits + 7) // 8), None


def filters_of(settings: dict) -> list[str]:
    filters = settings.get("/F", settings.get("/Filter", []))
    if not isinstance(filters, list):
        filters = [filters]
    return [str(value) for value in filters]


def scan_pdf(path: Path, limits: Limits = Limits()) -> dict[str, Any]:
    """Scan one file in-process. CLI adds process isolation and a timeout."""
    result: dict[str, Any] = {
        "status": "ok", "file_bytes": path.stat().st_size, "sha256": None,
        "pages_total": None, "pages_scanned": 0, "images": [], "errors": [],
    }
    if result["file_bytes"] > limits.max_file_bytes:
        result.update(status="skipped", errors=["file_size_limit"])
        return result
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256") if hasattr(hashlib, "file_digest") else None
        if digest is None:  # Python 3.10
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    definitions: dict[tuple[str, int], dict[str, Any]] = {}
    operations: dict[str, list] = {}
    direct_forms: dict[int, str] = {}
    form_invocations = 0

    def form_label(form: Any) -> str:
        ref = getattr(form, "indirect_reference", None)
        if ref is not None:
            return f"form:{ref.idnum}:{ref.generation}"
        return direct_forms.setdefault(id(form), f"direct-form:{len(direct_forms) + 1}")

    def walk(stream: Any, resources: Any, label: str, page: int,
             depth: int, active: frozenset[str]) -> None:
        nonlocal form_invocations
        if depth > limits.max_form_depth:
            raise ScanLimit("form_depth_limit")
        if label in active:
            raise ScanLimit(f"cyclic_form:{label}")
        active = active | {label}
        if label not in operations:
            operations[label] = ContentStream(stream, reader).operations
        for index, (operands, operator) in enumerate(operations[label]):
            if operator == b"INLINE IMAGE":
                key = (label, index)
                settings = operands["settings"]
                estimated, unknown = sample_estimate(settings, resources)
                if key in definitions:
                    previous = definitions[key]
                    previous["invocations"] += 1
                    if (previous["estimated_packed_sample_bytes"], previous["estimate_unknown_reason"]) != (estimated, unknown):
                        previous["estimated_packed_sample_bytes"] = None
                        previous["estimate_unknown_reason"] = "context_dependent_color_space"
                    continue
                if len(definitions) >= limits.max_images:
                    raise ScanLimit("image_count_limit")
                item = {
                    "stream": label, "operation_index": index, "first_page": page,
                    "invocations": 1,
                    "parsed_encoded_payload_bytes": len(operands["data"]),
                    "estimated_packed_sample_bytes": estimated,
                    "estimate_unknown_reason": unknown,
                    "filters": filters_of(settings),
                }
                definitions[key] = item
            elif operator == b"Do" and operands:
                xobjects = resources.get("/XObject", {})
                if hasattr(xobjects, "get_object"):
                    xobjects = xobjects.get_object()
                target = xobjects.get(operands[0])
                if target is None:
                    raise ValueError(f"missing_xobject:{operands[0]}")
                form = target.get_object()
                if form.get("/Subtype") != "/Form":
                    continue
                form_invocations += 1
                if form_invocations > limits.max_form_invocations:
                    raise ScanLimit("form_invocation_limit")
                nested_resources = form.get("/Resources", resources)
                if hasattr(nested_resources, "get_object"):
                    nested_resources = nested_resources.get_object()
                if isinstance(nested_resources, NullObject):
                    nested_resources = resources
                walk(form, nested_resources, form_label(form), page, depth + 1, active)

    try:
        with PdfReader(path, strict=False) as reader:
            if reader.is_encrypted and not reader.decrypt(""):
                result.update(status="encrypted", errors=["password_required"])
                return result
            result["pages_total"] = len(reader.pages)
            for number, page in enumerate(reader.pages):
                if number >= limits.max_pages:
                    result["errors"].append("page_count_limit")
                    break
                try:
                    content = page.get_contents()
                    resources = page.get("/Resources", DictionaryObject())
                    if hasattr(resources, "get_object"):
                        resources = resources.get_object()
                    if content is not None:
                        walk(content, resources, f"page:{number + 1}", number + 1, 0, frozenset())
                    result["pages_scanned"] += 1
                except Exception as error:
                    result["errors"].append(f"page:{number + 1}:{type(error).__name__}:{error}")
                    if isinstance(error, ScanLimit) or len(result["errors"]) >= 100:
                        break
    except Exception as error:
        result["status"] = "error"
        result["errors"].append(f"{type(error).__name__}:{error}")
    result["images"] = list(definitions.values())
    if result["status"] == "ok" and result["errors"]:
        result["status"] = "partial"
    return result


def _worker(path: str, limits: Limits, output: str) -> None:
    try:
        report = scan_pdf(Path(path), limits)
    except Exception as error:
        report = {"status": "error", "images": [], "errors": [f"{type(error).__name__}:{error}"]}
    Path(output).write_text(json.dumps(report, ensure_ascii=True), encoding="utf-8")


def isolated_scan(path: Path, limits: Limits, timeout: float) -> dict:
    """A timeout kills only this tool's worker. This is not a PDF sandbox."""
    with tempfile.TemporaryDirectory(prefix="pdf-inline-audit-") as scratch:
        output = Path(scratch) / "result.json"
        process = multiprocessing.get_context("spawn").Process(
            target=_worker, args=(str(path), limits, str(output)),
        )
        process.start()
        try:
            process.join(timeout)
            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
                return {"status": "timeout", "images": [], "errors": ["file_timeout"]}
            if process.exitcode != 0 or not output.exists():
                return {"status": "error", "images": [], "errors": ["worker_failed"]}
            return json.loads(output.read_text(encoding="utf-8"))
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            process.close()


def distribution(values: list[int]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered), "minimum": min(ordered) if ordered else None,
        "maximum": max(ordered) if ordered else None,
        "p50_nearest_rank": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95_nearest_rank": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "greater_than_4096": sum(v > 4096 for v in ordered),
        "greater_than_8192": sum(v > 8192 for v in ordered),
    }


def summarize(files: list[dict]) -> dict:
    images = [image for item in files for image in item["images"]]
    return {
        "file_status_counts": dict(Counter(item["status"] for item in files)),
        "unique_inline_definitions": len(images),
        "inline_invocations": sum(image["invocations"] for image in images),
        "parsed_encoded_payload_bytes": distribution(
            [image["parsed_encoded_payload_bytes"] for image in images]),
        "estimated_packed_sample_bytes": distribution(
            [image["estimated_packed_sample_bytes"] for image in images
             if image["estimated_packed_sample_bytes"] is not None]),
        "unknown_sample_estimates": sum(image["estimated_packed_sample_bytes"] is None
                                        for image in images),
    }


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF files or recursive directories")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=positive_int, default=20, help="seconds per file")
    parser.add_argument("--max-file-mib", type=positive_int, default=50)
    parser.add_argument("--max-pages", type=positive_int, default=2000)
    parser.add_argument("--max-images", type=positive_int, default=10000)
    args = parser.parse_args(argv)
    paths: dict[Path, str] = {}
    for root in args.inputs:
        if not root.exists():
            parser.error(f"input does not exist: {root}")
        candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in candidates:
            if path.is_file() and path.suffix.lower() == ".pdf":
                label = f"{root.name}/{path.relative_to(root).as_posix()}" if root.is_dir() else path.name
                paths.setdefault(path.resolve(), label)
    if not paths:
        parser.error("no PDF files found")
    output_resolved = args.output.resolve()
    if output_resolved in paths or (
        args.output.exists() and any(args.output.samefile(path) for path in paths)
    ):
        parser.error("output must not overwrite any input PDF (including aliases)")
    if args.output.exists():
        parser.error("output already exists; choose a fresh filename")
    limits = Limits(max_file_bytes=args.max_file_mib * 1024 * 1024,
                    max_pages=args.max_pages, max_images=args.max_images)
    files = []
    for path, label in paths.items():
        record = isolated_scan(path, limits, args.timeout)
        record["source"] = label
        files.append(record)
    report = {
        "schema_version": 1, "tool_version": VERSION, "pypdf_version": pypdf.__version__,
        "limits": asdict(limits), "timeout_seconds_per_file": args.timeout,
        "measurement_notes": [
            "Sizes are parser-retained encoded inline payloads, not exact original byte spans.",
            "Sample sizes are metadata-derived estimates with byte-aligned scanlines, not decoded measurements.",
            "No threshold is a proposed policy or a PDF conformance verdict.",
            "Unique definitions use per-page top-level streams and document-wide Form references plus operation index; repeated Form placements counted separately.",
            "Only page content and invoked Form XObjects are traversed; annotation appearances, patterns and Type3 glyph programs are excluded.",
            "Partial, skipped, encrypted, errored and timed-out inputs remain explicit in the report.",
        ],
        "summary": summarize(files), "files": files,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse files created since the initial collision check as well.
    with args.output.open("x", encoding="utf-8") as destination:
        destination.write(json.dumps(report, indent=2, ensure_ascii=True) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 1 if any(item["status"] != "ok" for item in files) else 0


if __name__ == "__main__":
    raise SystemExit(main())
