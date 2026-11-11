# P&ID Migration Tool

A Python desktop application for converting reviewed P&ID drawing data into Proteus 4.0.1 XML for testing in AVEVA Unified Engineering.

The tool is designed to reduce manual migration work. It does **not** automatically turn every DWG or PDF into a fully intelligent P&ID: users review symbol classifications, tags, piping, and ownership before export.

## Current workflow

1. Select the drawing and supporting mapping/tag workbooks in the application.
2. Scan the drawing and review detected items.
3. Approve equipment, nozzles, instruments, pipe linework, and piping components.
4. Generate Proteus XML from the approved decisions.
5. Validate the XML against the bundled Proteus 4.0.1 XSD.
6. Import the result into AVEVA Unified Engineering and check its appearance and engineering objects against the source drawing.

Review decisions are saved in CSV files so they can be inspected and reused within the conversion workflow.

## Current status

The exporter has been tested with reviewed equipment, nozzles, instrument bubbles, piping networks, and piping components.

Not yet complete:

- Signal-line export and confirmed signal endpoints
- Reuse of one ShapeCatalogue definition across identical placed symbols
- General conversion of arbitrary DWG/PDF files without preprocessing or review

## Requirements

- Python 3
- `ezdxf`
- `lxml`
- `openpyxl`
- Tkinter support for the desktop interface

Install the Python dependencies:

```powershell
python -m pip install ezdxf lxml openpyxl
```

## Run

Keep the Python modules and `ProteusSchema_4.0.1.xsd` together, then run:

```powershell
python pid_converter_app.py
```

Use the application to select the requested input files and complete the review stages before generating XML.

## Important limitations

- A successful XSD validation confirms XML structure, **not** that AVEVA has created every intended intelligent item or connection.
- The tool must not infer unreviewed engineering classes, URIs, ownership, or connectivity.
- Keep the source drawing, review CSVs, generated XML, and AVEVA import results together for traceability.

## Project goal

Develop a reusable, human-in-the-loop desktop converter that recognizes known P&ID symbols, asks users to resolve uncertain items, remembers approved decisions, and produces XML that can be imported to Aveva Unified Engineering.
