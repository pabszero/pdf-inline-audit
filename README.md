# pdf-inline-audit

Measure inline-image payload sizes across a PDF corpus with reproducible JSON
results. This research tool was motivated by the pypdf maintainers' request for
corpus statistics in [pypdf #4196](https://github.com/py-pdf/pypdf/issues/4196).
It does not add or recommend a hard image limit to pypdf.

## Install and run

Python 3.10 or newer:

```sh
python -m pip install .
pdf-inline-audit path/to/pdfs --output report.json
```

Multiple files or directories are accepted. Directory scanning is recursive and
case-insensitive for `.pdf`; duplicate resolved input paths are scanned once.
No PDFs, extracted pixels, text, credentials or document metadata are uploaded.
There is no network client in this tool. Review filenames in the report before
sharing it: relative directory names may still identify your documents.

```sh
pdf-inline-audit corpus --max-file-mib 50 --max-pages 2000 \
  --max-images 10000 --timeout 20 --output report.json
```

Exit status `0` means every selected PDF was scanned successfully. `1` means at
least one file is partial, skipped, encrypted, errored or timed out; valid results
are still saved. Invalid command-line arguments return `2`.

Use a fresh output filename: the tool never replaces an existing output file and
rejects an output path that aliases an input PDF.

## What is measured

| Field | Meaning |
| --- | --- |
| `parsed_encoded_payload_bytes` | Length of the payload returned by pypdf's inline-image content parser after the containing stream has been decompressed. |
| `estimated_packed_sample_bytes` | Metadata-derived sample storage estimate using width, height, bits per component and known component counts, including byte padding on each row. |
| `invocations` | Number of times this definition is encountered through page content and invoked Form XObjects. |
| `sha256` | Hash of the input PDF, for identifying the exact corpus version. |

**The first field is not an exact original PDF byte span.** pypdf's extraction
helpers may discard or retain some delimiter whitespace. The second field is
**not measured decoded pixels**, not an extracted PNG/JPEG file size, and not
allocated image memory. Device and Indexed color spaces can be resolved directly
or by name in the current page/Form `/Resources` dictionary. Indexed images use
one component per sample, regardless of the palette's base space. Other custom
spaces (including ICCBased, Separation and DeviceN), missing names, and cyclic
aliases remain explicitly unknown. Lookup is bounded to 32 steps and never
reads image pixels or decodes profiles. See PDF 32000-1, sections 7.8.3 and 8.9.7
([Adobe reference](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/PDF32000_2008.pdf)).
DCT/JPX payloads are never treated as decoded rasters.

Forms are traversed through actual `Do` instructions, including nested Forms.
An indirect Form reused three times contributes one definition and three
invocations. A Form's own resources take precedence over inherited page
resources. If reuse under different inherited resources produces different
estimates, the definition's estimate becomes unknown with reason
`context_dependent_color_space`; invocation counting still includes every use.
Cycles, excessive nesting/invocations, page limits and image limits
produce explicit partial results. Uninvoked resource entries are not scanned.
Top-level content is keyed per page; a content stream shared between pages is
therefore measured per page. This distinction is recorded here for corpus users.
Annotation appearances, tiling patterns and Type3 glyph programs are excluded;
this is not an exhaustive scan of all possible executable streams in a PDF.

The summary includes nearest-rank percentiles and counts strictly above 4096 and
8192 bytes. These are exploratory thresholds, **not conformance verdicts or a
proposal for parser policy**. Partial-file measurements are included in summaries;
always inspect `file_status_counts`, per-file errors and coverage before drawing
conclusions.

Each PDF runs in a separate worker with a configurable wall-time limit. This
protects the batch from a hung parser; it is not an OS sandbox or a hard memory
limit. Only analyze PDFs you are permitted to process in an appropriate environment.

## Reproduce the initial corpus

The initial corpus consists of the 71 PDFs under `resources/` at upstream pypdf
commit `cf2dabb9bd14aebbb04e924d0c0e5737a938b97f` (15,340,486 bytes).
The upstream `sample-files` submodule and remote test fixtures are not included.
PDF fixtures are not redistributed by this project.

A second smoke corpus is the 34 PDFs in
[`py-pdf/sample-files`](https://github.com/py-pdf/sample-files) at commit
`818dc013ad1f537198e9fcfae8a6b0dffe25ffa3`. Its separate report is in `examples/`:
32 readable files, one encrypted file and one parser error. Both smoke corpora
contain the same small ReportLab inline-image example (51 parser-retained payload
bytes; 768 estimated packed sample bytes). These are overlapping, test-oriented
samples, **not two independent observations or a representative size survey**.
They cannot establish a safe size threshold. A broader corpus is still needed.

```sh
git clone https://github.com/py-pdf/pypdf.git pypdf-source
git -C pypdf-source checkout cf2dabb9bd14aebbb04e924d0c0e5737a938b97f
pdf-inline-audit pypdf-source/resources --output resources-report.json
git clone https://github.com/py-pdf/sample-files.git sample-files
git -C sample-files checkout 818dc013ad1f537198e9fcfae8a6b0dffe25ffa3
pdf-inline-audit sample-files --output sample-files-report.json
```

The dependency is pinned to pypdf 6.21.1 because the content-parser representation
used by this tool is version-sensitive. Reassess the tests before upgrading it.

## Inline-regression research

[`research/inline-regressions.json`](research/inline-regressions.json) records a
separate, deliberately inline-heavy selection of eight public upstream regression
fixtures (11,475,722 downloaded bytes), with original URLs, SHA256 values,
per-file results and reproduction instructions. PDF bytes are not redistributed.

Seven files completed; one deliberately malformed DCT fixture is partial. The
collector found 15,453 inline definitions with parsed payloads from 1 to 3,996
bytes. Two DCT payloads of 3,056 and 2,079 bytes have estimated packed sample sizes
of 56,394 and 15,183 bytes respectively. Encoded and estimated sample limits
therefore answer different questions. The estimates remain metadata-derived.

The historical report was produced by version 0.1.0, with 1,684 unresolved
sample estimates. Version 0.2.0 resolves all 1,684 through named Indexed color
spaces in `plan-excerpt.pdf`. The comparison in
[`research/resource-color-spaces.json`](research/resource-color-spaces.json)
uses the same eight PDF hashes, statuses and 15,453 definitions; the encoded
payload distribution is unchanged. Reproduce it with `--max-images 20000`
(the default 10,000 limit truncates the largest file). No additional independent
sample is claimed.

One document contributes 13,591 definitions and dominates pooled percentiles.
The JPX control contains an XObject and does
not establish inline-JPX coverage. This convenience sample does not justify a
universal hard limit or estimate ecosystem prevalence. No upstream review or
acceptance is claimed.

## Tests

Tests use only the standard library and the pinned runtime dependency:

```sh
python -m unittest discover -s tests -v
```

Synthetic PDF fixtures test parser-backed payload measurements, filter arrays,
long-form keys, false-positive text strings, bit-packed rows, image masks, repeated
Forms, recursion, indirect named color spaces, Form resource precedence,
context-dependent reuse, cyclic aliases, unknown color spaces, corruption,
encryption, scan limits,
timeouts and the Windows-compatible spawn-based CLI.

## Contribution and provenance

The original tool is MIT licensed. pypdf is an external BSD-3-Clause dependency;
its source and test PDFs remain under their upstream terms. This project is not
affiliated with or endorsed by pypdf or Anthropic.

Implementation and tests were developed with OpenAI Codex assistance. No exact
model identifier is recorded here. Test results and the corpus report are local
evidence, not upstream review, acceptance, independent adoption, or eligibility
for any sponsorship program.

Before sharing a contribution with pypdf, read its
[AI policy](https://github.com/py-pdf/pypdf/blob/main/CONTRIBUTING.md#ai-policy).
Its maintainers permit coding assistance, but require contributors to understand
and describe their own work, write maintainer communications themselves, and
disclose AI assistance. This repository does not automate outreach or PR creation.
