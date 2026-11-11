import csv
import re
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ezdxf import path as dxf_path


import ezdxf
import ezdwg
from lxml import etree
from openpyxl import load_workbook

APP_FOLDER = Path(__file__).resolve().parent
BUNDLED_XSD = APP_FOLDER / "ProteusSchema_4.0.1.xsd"

def norm_handle(value):
    if isinstance(value, int):
        return f"{value:X}"
    return str(value).upper().lstrip("0") or "0"

def flatten_entity(entity, tolerance=0.08):
    """Return paths drawn by a DXF entity, without inventing geometry."""
    kind = entity.dxftype()

    if kind == "LINE":
        return [[
            (entity.dxf.start.x, entity.dxf.start.y),
            (entity.dxf.end.x, entity.dxf.end.y),
        ]]

    if kind not in {
        "ARC", "ELLIPSE", "CIRCLE", "LWPOLYLINE", "POLYLINE"
    }:
        return []

    try:
        path = dxf_path.make_path(entity)
        points = [
            (vertex.x, vertex.y)
            for vertex in path.flattening(tolerance)
        ]
        if kind == "CIRCLE" and points and points[0] != points[-1]:
            points.append(points[0])
        return [points] if len(points) >= 2 else []
    except (TypeError, ValueError, AttributeError):
        return []

def read_mapping(path):
    book = load_workbook(path, read_only=True, data_only=True)
    rows = {}
    for row in book.active.iter_rows(min_row=2, values_only=True):
        if row[0]:
            rows[str(row[0]).strip().upper()] = {
                "class": str(row[3] or "").strip(),
                "uri": str(row[4] or "").strip(),
            }
    return rows


TAG_SHEETS = {
    "Equipment": {"Name", "Class", "HasNozzles"},
    "Pipe": {
        "Name", "Bore_mm", "Specification",
        "InsulationType", "InsulationThickness_mm",
    },
    "Instrument": {"Name"},
    "Valve": {"Name", "FailPosition"},
}


def read_tag_records(path):
    """Read the four tag sheets as named records, preserving exact tag text."""
    if not path:
        return []

    book = load_workbook(path, read_only=True, data_only=True)
    records = []

    for sheet_name, required_columns in TAG_SHEETS.items():
        if sheet_name not in book:
            raise ValueError(f"Tag workbook is missing sheet: {sheet_name}")

        sheet = book[sheet_name]
        rows = sheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        if not header_row:
            raise ValueError(f"Tag sheet is empty: {sheet_name}")

        headers = [
            str(value).strip() if value is not None else ""
            for value in header_row
        ]
        missing = required_columns - set(headers)
        if missing:
            raise ValueError(
                f"{sheet_name} is missing columns: "
                + ", ".join(sorted(missing))
            )

        for row_number, values in enumerate(rows, start=2):
            record = {
                header: (
                    str(value).strip() if value is not None else ""
                )
                for header, value in zip(headers, values)
                if header
            }
            if not record.get("Name"):
                continue
            record["sheet"] = sheet_name
            record["source_row"] = row_number
            records.append(record)

    return records


def write_csv(path, rows, fieldnames):
    with path.open("x", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file, fieldnames=fieldnames, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)



def dwg_items(path):
    doc = ezdwg.read(str(path))
    space = doc.modelspace()
    iterator = (
        space.iter_entities()
        if hasattr(space, "iter_entities")
        else space.query()
    )
    return {
        norm_handle(entity.handle): entity.dxftype
        for entity in iterator
    }


def dxf_items(path):
    doc = ezdxf.readfile(str(path))
    return doc, {
        norm_handle(entity.dxf.handle): entity.dxftype()
        for entity in doc.modelspace()
    }


def scan(inputs):
    dxf = Path(inputs["DXF"])
    dwg = Path(inputs["DWG"]) if inputs["DWG"] else None
    mapping_file = Path(inputs["Mapping workbook"])
    xsd_file = Path(inputs["Proteus XSD"])
    tag_file = (
        Path(inputs["Tag workbook"])
        if inputs["Tag workbook"] else None
    )

    for path in (dxf, mapping_file, xsd_file):
        if not path.is_file():
            raise FileNotFoundError(path)
    for path in (dwg, tag_file):
        if path and not path.is_file():
            raise FileNotFoundError(path)

    # Validate that the selected XSD itself can be compiled.
    etree.XMLSchema(etree.parse(str(xsd_file)))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_folder = dxf.parent / f"{dxf.stem}_conversion_run_{stamp}"
    run_folder.mkdir(exist_ok=False)

    mappings = read_mapping(mapping_file)
    tags = read_tag_records(tag_file)
    doc, dxf_by_handle = dxf_items(dxf)
    dwg_by_handle = dwg_items(dwg) if dwg else {}

    review_rows = []
    for entity in doc.modelspace().query("INSERT"):
        handle = norm_handle(entity.dxf.handle)
        block = entity.dxf.name
        found = mappings.get(block.upper())
        attributes = {
            a.dxf.tag: a.dxf.text for a in entity.attribs
        }
        point = entity.dxf.insert
        review_rows.append({
            "source_dxf": dxf.name,
            "source_handle": handle,
            "block_name": block,
            "x": point.x,
            "y": point.y,
            "layer": entity.dxf.get("layer", ""),
            "source_attributes": repr(attributes),
            "also_in_dwg_by_handle": (
                "YES" if handle in dwg_by_handle
                else "NO" if dwg else "NOT_CHECKED"
            ),
            "exact_workbook_match": "YES" if found else "NO",
            "candidate_class": found["class"] if found else "",
            "candidate_uri": found["uri"] if found else "",
            "confirmed_tag": "",
            "confirmed_class": "",
            "confirmed_uri": "",
            "review_status": "REVIEW",
        })

    review_path = run_folder / "placed_symbols_REVIEW.csv"
    with review_path.open("x", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=list(review_rows[0])
        )
        writer.writeheader()
        writer.writerows(review_rows)

    if tag_file:
        tag_fields = [
            "sheet", "source_row", "Name", "Class", "HasNozzles",
            "Bore_mm", "Specification", "InsulationType",
            "InsulationThickness_mm", "FailPosition",
        ]
        write_csv(
            run_folder / "tag_records_REVIEW.csv",
            tags,
            tag_fields,
        )

    all_handles = sorted(set(dxf_by_handle) | set(dwg_by_handle))
    if dwg:
        differences = []
        for handle in all_handles:
            dxf_type = dxf_by_handle.get(handle, "")
            dwg_type = dwg_by_handle.get(handle, "")
            if dxf_type != dwg_type:
                differences.append({
                    "handle": handle,
                    "dxf_type": dxf_type,
                    "dwg_type": dwg_type,
                })

        difference_path = run_folder / "dwg_dxf_differences.csv"
        with difference_path.open(
            "x", newline="", encoding="utf-8-sig"
        ) as f:
            writer = csv.DictWriter(
                f, fieldnames=[
                    "handle", "dxf_type", "dwg_type"
                ]
            )
            writer.writeheader()
            writer.writerows(differences)
    else:
        differences = []

    report_lines = [
        "P&ID CONVERTER — INPUT SCAN",
        "This run has NOT generated intelligent XML.",
        "",
        f"DXF: {dxf}",
        f"DWG: {dwg or 'not supplied'}",
        f"Mapping workbook: {mapping_file}",
        f"Tag workbook: {tag_file or 'not supplied'}",
        f"Proteus XSD: {xsd_file}",
        "",
        f"DXF entities: {len(dxf_by_handle)}",
        f"DWG entities: {len(dwg_by_handle)}",
        f"Placed DXF blocks: {len(review_rows)}",
        f"Workbook mappings: {len(mappings)}",
        f"Structured tag records: {len(tags)}",
"Tag records by sheet: "
+ repr(dict(Counter(r["sheet"] for r in tags))),
        f"Block names matching workbook exactly: "
        f"{sum(r['exact_workbook_match'] == 'YES' for r in review_rows)}",
        f"DWG/DXF handle-or-type differences: {len(differences)}",
        "",
        "DXF entity types: "
        + repr(dict(Counter(dxf_by_handle.values()))),
        "DWG entity types: "
        + repr(dict(Counter(dwg_by_handle.values()))),
        "",
"Scan complete. Review the drawing items before exporting the XML.",
"After export, import the XML into AVEVA and check it against the original drawing.",


        
    ]
    (run_folder / "run_report.txt").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )
    return run_folder, "\n".join(report_lines)

def mapping_rows(path, class_filter=None):
    book = load_workbook(path, read_only=True, data_only=True)
    result = []

    try:
        for row in book.active.iter_rows(min_row=2, values_only=True):
            if len(row) < 5:
                continue

            symbol = str(row[0] or "").strip()
            class_name = str(row[3] or "").strip()
            uri = str(row[4] or "").strip()
            if not class_name or not uri:
                continue
            if class_filter and not class_filter(class_name):
                continue

            result.append({
                "symbol": symbol,
                "class": class_name,
                "uri": uri,
                "search": " ".join(
                    str(cell) for cell in row if cell is not None
                ).casefold(),
            })
    finally:
        book.close()

    return result


def searchable_mapping_picker(parent, records, on_select):
    """Open a searchable list; on_select receives the selected workbook record."""
    picker = tk.Toplevel(parent)
    picker.title("Choose approved class / URI")
    picker.geometry("1000x550")

    search = tk.StringVar()
    ttk.Label(picker, text="Search any cell in the mapping row").pack(
        anchor="w", padx=10, pady=(10, 0)
    )
    entry = ttk.Entry(picker, textvariable=search)
    entry.pack(fill="x", padx=10, pady=5)

    listing = tk.Listbox(
        picker, font=("Consolas", 10), exportselection=False
    )
    listing.pack(fill="both", expand=True, padx=10, pady=5)
    visible = []

    def refresh(*_):
        terms = search.get().casefold().split()
        listing.delete(0, "end")
        visible.clear()

        for record in records:
            if all(term in record["search"] for term in terms):
                visible.append(record)
                listing.insert(
                    "end",
                    f'{record["symbol"]} | '
                    f'{record["class"]} | {record["uri"]}',
                )

    def use_choice(event=None):
        selection = listing.curselection()
        if not selection:
            return
        on_select(visible[selection[0]])
        picker.destroy()

    search.trace_add("write", refresh)
    listing.bind("<Double-Button-1>", use_choice)
    ttk.Button(
        picker, text="Use selected mapping", command=use_choice
    ).pack(pady=8)
    refresh()
    entry.focus_set()


class App(tk.Tk):
    FILES = (
        ("DXF", "*.dxf"),
        ("DWG", "*.dwg"),
        ("Mapping workbook", "*.xlsx"),
        ("Tag workbook", "*.xlsx"),
        
    )

    def __init__(self):
        super().__init__()
        self.title("P&ID Converter — Input and Review")
        self.geometry("940x780")
        self.fields = {}

        panel = ttk.Frame(self, padding=16)
        panel.pack(fill="both", expand=True)

        for row, (label, pattern) in enumerate(self.FILES):
            ttk.Label(panel, text=label, width=21).grid(
                row=row, column=0, sticky="w", pady=5
            )
            variable = tk.StringVar()
            self.fields[label] = variable
            ttk.Entry(
                panel, textvariable=variable, width=82
            ).grid(row=row, column=1, padx=8, sticky="ew")
            ttk.Button(
                panel, text="Browse",
                command=lambda v=variable, p=pattern:
                    v.set(filedialog.askopenfilename(
                        filetypes=[("Selected file", p)]
                    ) or v.get())
            ).grid(row=row, column=2)

        ttk.Button(
            panel, text="Scan selected files",
            command=self.run_scan
        ).grid(row=6, column=0, columnspan=3, pady=15)

        self.output = tk.Text(panel, wrap="word", height=17)
        self.output.grid(
            row=7, column=0, columnspan=3, sticky="nsew"
        )
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(7, weight=1)
        ttk.Button(
            panel,
            text="Generate XML",
            command=self.run_export,
        ).grid(row=8, column=0, sticky="w", pady=8)
        ttk.Button(
            panel, text="Review placed equipment",
            command=self.open_review,
        ).grid(row=9, column=0, sticky="w", pady=5)

        ttk.Button(
            panel, text="Open existing scan folder",
            command=self.open_existing_run,
        ).grid(row=9, column=2, sticky="e", pady=5)

        ttk.Button(
            panel, text="Review pipe linework",
            command=self.open_pipe_review,
        ).grid(row=10, column=0, sticky="w", pady=5)


    def run_scan(self):
        try:
            inputs = {
                name: var.get().strip()
                for name, var in self.fields.items()
            }
            inputs["Proteus XSD"] = str(BUNDLED_XSD)
            folder, report = scan(inputs)
            self.latest_folder = folder
            self.output.delete("1.0", "end")
            self.output.insert(
                "end", report + f"\n\nSaved in:\n{folder}"
            )
            messagebox.showinfo(
                "Scan complete", f"Results saved in:\n{folder}"
            )
        except Exception:
            details = traceback.format_exc()
            self.output.delete("1.0", "end")
            self.output.insert("end", details)
            messagebox.showerror(
                "Scan failed",
                "See the error in the application window."
            )
    def open_existing_run(self):
        folder = filedialog.askdirectory(
            title="Select an existing conversion_run folder"
        )
        if not folder:
            return
        path = Path(folder)
        if not (path / "placed_symbols_REVIEW.csv").is_file():
            messagebox.showerror(
                "Wrong folder",
                "This folder has no placed_symbols_REVIEW.csv",
            )
            return
        self.latest_folder = path
        self.output.delete("1.0", "end")
        self.output.insert(
            "end", f"Active reviewed run:\n{path}\n"
            "Review decisions in this folder will be used for export."
        )

    def open_review(self):
        if not hasattr(self, "latest_folder"):
            messagebox.showerror(
                "No scan selected",
                "Scan first or open an existing scan folder.",
            )
            return

        path = self.latest_folder / "placed_symbols_REVIEW.csv"
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            fields = reader.fieldnames
            rows = list(reader)
        if "item_kind" not in fields:
            fields.append("item_kind")
        for row in rows:
            row["item_kind"] = row.get("item_kind") or "Equipment"
        if "loop_tag" not in fields:
            fields.append("loop_tag")
        for row in rows:
            row["loop_tag"] = row.get("loop_tag") or ""


        if not rows:
            messagebox.showerror("No blocks", "Review CSV has no rows.")
            return

        window = tk.Toplevel(self)
        window.title("Review DXF blocks — save decisions before export")
        window.state("zoomed")

        columns = ("handle", "block", "x", "y", "tag", "class", "status")
        table = ttk.Treeview(
            window, columns=columns, show="headings", height=8
        )
        for column, width in (
            ("handle", 80), ("block", 160), ("x", 100),
            ("y", 100), ("tag", 120), ("class", 220),
            ("status", 100),
        ):
            table.heading(column, text=column)
            table.column(column, width=width)
        table.pack(fill="x", expand=True, padx=10, pady=10)
        
        preview = tk.Canvas(
            window, bg="white", height=400,
            highlightthickness=1,
            highlightbackground="grey",
        )
        preview.pack(fill="both", padx=10, pady=(0, 8))

        dxf_file = Path(self.fields["DXF"].get().strip())
        preview_doc = ezdxf.readfile(str(dxf_file))
        preview_inserts = {
            insert.dxf.handle.upper(): insert
            for insert in preview_doc.modelspace().query("INSERT")
        }

        def draw_selected_block(event=None):
            preview.delete("all")
            selection = table.selection()
            if not selection:
                return

            row = rows[int(selection[0])]
            selected_handle = row["source_handle"].strip().upper()
            selected_insert = preview_inserts.get(selected_handle)
            if selected_insert is None:
                preview.create_text(
                    10, 10, anchor="nw",
                    text=f"INSERT {selected_handle} not found in selected DXF",
                    fill="red",
                )
                return

            # Draw the whole modelspace for spatial context.
            drawable = []
            for entity in preview_doc.modelspace():
                if entity.dxftype() == "INSERT":
                    paths = []
                    for virtual in entity.virtual_entities():
                        paths.extend(flatten_entity(virtual))
                else:
                    paths = flatten_entity(entity)

                for part in paths:
                    if len(part) >= 2:
                        drawable.append((
                            entity.dxf.handle.upper(),
                            part,
                        ))

            if not drawable:
                return

            points = [
                point for _, part in drawable for point in part
            ]
            min_x = min(x for x, y in points)
            max_x = max(x for x, y in points)
            min_y = min(y for x, y in points)
            max_y = max(y for x, y in points)

            width = max(preview.winfo_width(), 400)
            height = max(preview.winfo_height(), 220)
            margin = 15
            scale = min(
                (width - 2 * margin) / max(max_x - min_x, 1),
                (height - 2 * margin) / max(max_y - min_y, 1),
            )

            def coordinates(part):
                result = []
                for x, y in part:
                    result.extend((
                        margin + (x - min_x) * scale,
                        height - margin - (y - min_y) * scale,
                    ))
                return result

            # Other drawing geometry stays grey.
            for handle, part in drawable:
                if handle != selected_handle:
                    preview.create_line(
                        *coordinates(part),
                        fill="#c0c0c0", width=1,
                    )

            # Draw the selected block last, on top of its surroundings.
            for handle, part in drawable:
                if handle == selected_handle:
                    preview.create_line(
                        *coordinates(part),
                        fill="red", width=3,
                    )

            preview.create_text(
                10, 10, anchor="nw",
                text=(
                    f'Selected: {selected_handle} | '
                    f'{selected_insert.dxf.name}'
                ),
                fill="black",
            )
        def zoom_preview(event):
            if not event.delta:
                return
            factor = 1.25 if event.delta > 0 else 1 / 1.25
            x = preview.canvasx(event.x)
            y = preview.canvasy(event.y)
            preview.scale("all", x, y, factor, factor)
            preview.configure(scrollregion=preview.bbox("all"))

        def start_preview_pan(event):
            preview.scan_mark(event.x, event.y)

        def pan_preview(event):
            preview.scan_dragto(event.x, event.y, gain=1)

        preview.bind("<MouseWheel>", zoom_preview)
        preview.bind("<ButtonPress-2>", start_preview_pan)
        preview.bind("<B2-Motion>", pan_preview)



        def display(row):
            return (
                row["source_handle"], row["block_name"],
                row["x"], row["y"], row["confirmed_tag"],
                row["confirmed_class"], row["review_status"],
            )

        for index, row in enumerate(rows):
            table.insert("", "end", iid=str(index), values=display(row))

        form = ttk.Frame(window, padding=10)
        form.pack(fill="x")

        variables = {}
        editable = (
            ("confirmed_tag", "Tag"),
            ("confirmed_class", "Class"),
            ("confirmed_uri", "URI"),
        )
        for number, (key, label) in enumerate(editable):
            ttk.Label(form, text=label).grid(
                row=number, column=0, sticky="w", pady=3
            )
            variable = tk.StringVar()
            variables[key] = variable
            ttk.Entry(form, textvariable=variable, width=85).grid(
                row=number, column=1, sticky="ew", padx=8
            )

        status = tk.StringVar(value="REVIEW")
        ttk.Label(form, text="Status").grid(
            row=3, column=0, sticky="w"
        )
        ttk.Combobox(
            form, textvariable=status,
            values=("REVIEW", "APPROVED"),
            state="readonly", width=20,
        ).grid(row=3, column=1, sticky="w", padx=8)
        item_kind = tk.StringVar(value="Equipment")
        ttk.Label(form, text="Item kind").grid(
            row=3, column=2, sticky="w", padx=(20, 5)
        )
        ttk.Combobox(
            form, textvariable=item_kind,
            values=("Equipment", "Instrument"),
            state="readonly", width=18,
        ).grid(row=3, column=3, sticky="w")
        loop_tag = tk.StringVar()
        ttk.Label(form, text="Loop tag").grid(
            row=4, column=2, sticky="w", padx=(20, 5)
        )
        ttk.Entry(
            form, textvariable=loop_tag, width=30
        ).grid(row=4, column=3, sticky="w")

        
        form.columnconfigure(1, weight=1)
        mapping_path = self.fields["Mapping workbook"].get().strip()
        mapping_records = mapping_rows(mapping_path)

        def choose_workbook_mapping():
            try:
                selected_index()
            except ValueError as error:
                messagebox.showerror(
                    "Select a block", str(error), parent=window
                )
                return

            def accept(record):
                variables["confirmed_class"].set(record["class"])
                variables["confirmed_uri"].set(record["uri"])

            searchable_mapping_picker(
                window, mapping_records, accept
            )


        ttk.Button(
            form, text="Choose class/URI",
            command=choose_workbook_mapping,
        ).grid(row=4, column=1, sticky="w", pady=5)

        def selected_index():
            selection = table.selection()
            if not selection:
                raise ValueError("Select a block row first.")
            return int(selection[0])

        def load_selected(event=None):
            index = selected_index()
            row = rows[index]
            for key, variable in variables.items():
                variable.set(row.get(key, ""))
            status.set(row.get("review_status", "REVIEW"))
            item_kind.set(row.get("item_kind") or "Equipment")
            loop_tag.set(row.get("loop_tag") or "")



        def apply_selected():
            try:
                index = selected_index()
                proposed = rows[index].copy()
                for key, variable in variables.items():
                    proposed[key] = variable.get().strip()
                proposed["review_status"] = status.get()
                proposed["item_kind"] = item_kind.get()
                proposed["loop_tag"] = loop_tag.get().strip()



                if proposed["review_status"] == "APPROVED":
                    if not all(
                        proposed.get(key, "").strip()
                        for key in (
                            "confirmed_tag", "confirmed_class",
                            "confirmed_uri",
                        )
                    ):
                        raise ValueError(
                            "An approved equipment row needs tag, "
                            "class, and URI."
                        )

                rows[index] = proposed
                table.item(str(index), values=display(proposed))

            except ValueError as error:
                messagebox.showerror(
                    "Review incomplete", str(error), parent=window
                )

        def save_review():
            # Save to a temporary file first, then replace the CSV.
            temporary = path.with_name(path.name + ".tmp")
            try:
                with temporary.open(
                    "w", newline="", encoding="utf-8-sig"
                ) as file:
                    writer = csv.DictWriter(file, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                temporary.replace(path)
                messagebox.showinfo(
                    "Review saved",
                    f"Decisions saved to:\n{path}",
                    parent=window,
                )
            except Exception as error:
                messagebox.showerror(
                    "Could not save",
                    f"Close the CSV in Excel, then retry.\n{error}",
                    parent=window,
                )

        table.bind("<<TreeviewSelect>>", load_selected)
        table.bind("<<TreeviewSelect>>", draw_selected_block, add="+")

        ttk.Button(
            form, text="Apply to selected row",
            command=apply_selected,
        ).grid(row=5, column=1, sticky="w", pady=8)
        ttk.Button(
            form, text="Save review CSV",
            command=save_review,
        ).grid(row=6, column=1, sticky="w")
        ttk.Button(
            form,
            text="Assign nozzle blocks to selected equipment",
            command=lambda: self.assign_nozzles(
                rows[selected_index()]["confirmed_tag"].strip()
            ) if table.selection() else messagebox.showerror(
                "Select equipment",
                "Select an approved equipment row first.",
                parent=window,
            ),
        ).grid(row=7, column=1, sticky="w", pady=8)

    
    def review_fragments(self):
        if not hasattr(self, "latest_folder"):
            messagebox.showerror(
                "No run", "Scan or open an existing scan folder first."
            )
            return

        dxf_path = Path(self.fields["DXF"].get().strip())
        mapping_path = self.fields["Mapping workbook"].get().strip()
        if not dxf_path.is_file():
            messagebox.showerror("Missing DXF", str(dxf_path))
            return

        
        doc = ezdxf.readfile(str(dxf_path))
        candidates = []
        for entity in doc.modelspace():
            if entity.dxftype() not in {
                "LINE", "LWPOLYLINE", "POLYLINE",
                "ARC", "CIRCLE", "ELLIPSE",
            }:
                continue
            paths = flatten_entity(entity)
            points = [point for part in paths for point in part]
            if not points:
                continue
            candidates.append({
                "handle": entity.dxf.handle.upper(),
                "type": entity.dxftype(),
                "paths": paths,
                "min_x": min(x for x, y in points),
                "max_x": max(x for x, y in points),
                "min_y": min(y for x, y in points),
                "max_y": max(y for x, y in points),
            })

        if not candidates:
            messagebox.showerror(
                "No geometry", "No supported loose geometry found."
            )
            return

        book = load_workbook(
            mapping_path, read_only=True, data_only=True
        )
        mapping_records = mapping_rows(mapping_path)


        window = tk.Toplevel(self)
        window.title("Review fragmented equipment — DXF preview")
        window.geometry("1400x800")

        ttk.Label(
            window,
            text="Select strokes in the list (Ctrl-click), or click "
                 "them in the preview. RED = selected. "
                 "Inspect the complete outline before approval.",
        ).pack(anchor="w", padx=10, pady=5)

        main = ttk.Frame(window, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=3)
        main.rowconfigure(0, weight=1)

        listing = tk.Listbox(
            main, selectmode="extended",
            exportselection=False, font=("Consolas", 10),
        )
        listing.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        canvas = tk.Canvas(
            main, bg="white", highlightthickness=1,
            highlightbackground="grey",
        )
        canvas.grid(row=0, column=1, sticky="nsew")

        for item in candidates:
            cx = (item["min_x"] + item["max_x"]) / 2
            cy = (item["min_y"] + item["max_y"]) / 2
            listing.insert(
                "end",
                f'{item["handle"]:>6} {item["type"]:<11} '
                f'({cx:6.1f}, {cy:6.1f})',
            )

        # Fit all supported source strokes to the preview.
        all_min_x = min(item["min_x"] for item in candidates)
        all_max_x = max(item["max_x"] for item in candidates)
        all_min_y = min(item["min_y"] for item in candidates)
        all_max_y = max(item["max_y"] for item in candidates)
        canvas_items = {}

        def draw_preview(event=None):
            width = max(canvas.winfo_width(), 100)
            height = max(canvas.winfo_height(), 100)
            margin = 25
            scale = min(
                (width - 2 * margin)
                / max(all_max_x - all_min_x, 1),
                (height - 2 * margin)
                / max(all_max_y - all_min_y, 1),
            )

            def transform(x, y):
                return (
                    margin + (x - all_min_x) * scale,
                    height - margin - (y - all_min_y) * scale,
                )

            canvas.delete("all")
            canvas_items.clear()
            for index, item in enumerate(candidates):
                drawn = []
                for path in item["paths"]:
                    if len(path) < 2:
                        continue
                    coords = []
                    for x, y in path:
                        coords.extend(transform(x, y))
                    drawn.append(canvas.create_line(
                        *coords, fill="#888888", width=1,
                        tags=(f"source_{index}",),
                    ))
                canvas_items[index] = drawn
            highlight()

        def highlight(event=None):
            chosen = set(map(int, listing.curselection()))
            for index, drawn in canvas_items.items():
                selected = index in chosen
                for canvas_id in drawn:
                    canvas.itemconfigure(
                        canvas_id,
                        fill="red" if selected else "#888888",
                        width=3 if selected else 1,
                    )
                    if selected:
                        canvas.tag_raise(canvas_id)

        def click_preview(event):
            closest = canvas.find_closest(event.x, event.y)
            if not closest:
                return
            canvas_id = closest[0]
            x1, y1, x2, y2 = canvas.bbox(canvas_id)
            # Ignore clicks far from any source stroke.
            if not (
                x1 - 8 <= event.x <= x2 + 8
                and y1 - 8 <= event.y <= y2 + 8
            ):
                return

            for index, drawn in canvas_items.items():
                if canvas_id not in drawn:
                    continue
                if event.state & 0x0004:  # Ctrl toggles selection
                    if index in listing.curselection():
                        listing.selection_clear(index)
                    else:
                        listing.selection_set(index)
                else:
                    listing.selection_clear(0, "end")
                    listing.selection_set(index)
                listing.see(index)
                highlight()
                return

        listing.bind("<<ListboxSelect>>", highlight)
        canvas.bind("<Configure>", draw_preview)
        canvas.bind("<Button-1>", click_preview)
        def zoom_preview(event):
            if event.delta == 0:
                return
            factor = 1.2 if event.delta > 0 else 1 / 1.2

            # Zoom around the mouse pointer, not the drawing origin.
            x = canvas.canvasx(event.x)
            y = canvas.canvasy(event.y)
            canvas.scale("all", x, y, factor, factor)
            canvas.configure(scrollregion=canvas.bbox("all"))

        def start_pan(event):
            canvas.scan_mark(event.x, event.y)

        def pan_preview(event):
            canvas.scan_dragto(event.x, event.y, gain=1)

        canvas.bind("<MouseWheel>", zoom_preview)
        canvas.bind("<ButtonPress-2>", start_pan)
        canvas.bind("<B2-Motion>", pan_preview)


        form = ttk.Frame(window, padding=10)
        form.pack(fill="x")
        tag = tk.StringVar()
        mapping = tk.StringVar()

        ttk.Label(form, text="Confirmed tag").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(
            form, textvariable=tag, width=30
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(form, text="Approved mapping").grid(
            row=1, column=0, sticky="w"
        )

        selected_mapping = {"record": None}
        mapping_display = tk.StringVar()

        ttk.Entry(
            form, textvariable=mapping_display,
            width=105, state="readonly",
        ).grid(row=1, column=1, sticky="ew")

        def accept_mapping(record):
            selected_mapping["record"] = record
            mapping_display.set(
                f'{record["symbol"]} | '
                f'{record["class"]} | {record["uri"]}'
            )

        ttk.Button(
            form, text="Search class / URI",
            command=lambda: searchable_mapping_picker(
                window, mapping_records, accept_mapping
            ),
        ).grid(row=1, column=2, padx=5)

        
        form.columnconfigure(1, weight=1)

        def save_group():
            selected = listing.curselection()
            if not selected or not tag.get().strip():
                messagebox.showerror(
                    "Incomplete",
                    "Select the complete outline and enter its tag.",
                    parent=window,
                )
                return
            if selected_mapping["record"] is None:

                messagebox.showerror(
                    "Incomplete", "Choose a  mapping.",
                    parent=window,
                )
                return

            chosen = selected_mapping["record"]
            handles = [candidates[i]["handle"] for i in selected]
            review_path = (
                self.latest_folder / "fragmented_equipment_REVIEW.csv"
            )
            columns = (
                "source_dxf", "confirmed_tag",
                "confirmed_class", "confirmed_uri",
                "geometry_handles", "review_status",
            )
            previous = []
            if review_path.exists():
                with review_path.open(
                    newline="", encoding="utf-8-sig"
                ) as file:
                    previous = list(csv.DictReader(file))

            used = {
                handle.strip().upper()
                for row in previous
                if row["review_status"].upper() == "APPROVED"
                for handle in row["geometry_handles"].split("|")
            }
            if used.intersection(handles):
                messagebox.showerror(
                    "Already grouped",
                    "A selected stroke is already in an approved group.",
                    parent=window,
                )
                return

            previous.append({
                "source_dxf": dxf_path.name,
                "confirmed_tag": tag.get().strip(),
                "confirmed_class": chosen["class"],
                "confirmed_uri": chosen["uri"],
                "geometry_handles": "|".join(handles),
                "review_status": "APPROVED",
            })
            temporary = review_path.with_suffix(".csv.tmp")
            try:
                with temporary.open(
                    "w", newline="", encoding="utf-8-sig"
                ) as file:
                    writer = csv.DictWriter(
                        file, fieldnames=columns
                    )
                    writer.writeheader()
                    writer.writerows(previous)
                temporary.replace(review_path)
            except Exception as error:
                messagebox.showerror(
                    "Save failed", str(error), parent=window
                )
                return

            messagebox.showinfo(
                "Group saved",
                f"Approved {len(handles)} DXF strokes for "
                f"{tag.get().strip()}.\n{review_path}",
                parent=window,
            )
            window.destroy()

        ttk.Button(
            form, text="Approve and save selected geometry",
            command=save_group,
        ).grid(row=2, column=1, sticky="w", pady=8)
        
    def assign_nozzles(self, owner_tag):
        if not hasattr(self, "latest_folder"):
            messagebox.showerror("No run", "Open a run folder first.")
            return

        folder = self.latest_folder
        dxf_path = Path(self.fields["DXF"].get().strip())
        mapping_path = Path(
            self.fields["Mapping workbook"].get().strip()
        )
        if not dxf_path.is_file() or not mapping_path.is_file():
            messagebox.showerror(
                "Missing input", "Select the DXF and mapping workbook."
            )
            return

        approved_tags = set()
        for filename in (
            "placed_symbols_REVIEW.csv",
            "fragmented_equipment_REVIEW.csv",
        ):
            review_file = folder / filename
            if not review_file.is_file():
                continue
            with review_file.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                for row in csv.DictReader(file):
                    if (
                        row.get("review_status", "").strip().upper()
                        == "APPROVED"
                    ):
                        approved_tags.add(
                            row.get("confirmed_tag", "").strip()
                        )

        if owner_tag not in approved_tags:
            messagebox.showerror(
                "Equipment not saved",
                f"Approve and save {owner_tag} before assigning nozzles.",
            )
            return

        mapping_records = mapping_rows(
            mapping_path,
            class_filter=lambda class_name:
                "nozzle" in class_name.casefold(),
        )
        if not mapping_records:
            messagebox.showerror(
                "No mapping",
                "No nozzle class/URI found in the mapping workbook.",
            )
            return


        equipment_handles = set()
        placed_file = folder / "placed_symbols_REVIEW.csv"
        with placed_file.open(
            newline="", encoding="utf-8-sig"
        ) as file:
            for row in csv.DictReader(file):
                if (
                    row.get("review_status", "").strip().upper()
                    == "APPROVED"
                ):
                    equipment_handles.add(
                        row["source_handle"].strip().upper()
                    )

        doc = ezdxf.readfile(str(dxf_path))
        candidates = []
        for insert in doc.modelspace().query("INSERT"):
            handle = insert.dxf.handle.upper()
            if handle in equipment_handles:
                continue
            paths = []
            for virtual in insert.virtual_entities():
                paths.extend(flatten_entity(virtual))
            paths = [part for part in paths if len(part) >= 2]
            if paths:
                candidates.append({
                    "handle": handle,
                    "block": insert.dxf.name,
                    "paths": paths,
                })

        if not candidates:
            messagebox.showerror(
                "No candidates", "No remaining drawable blocks found."
            )
            return

        review_file = folder / "nozzles_REVIEW.csv"
        columns = (
            "source_dxf", "source_handle", "block_name",
            "owner_tag", "nozzle_name", "confirmed_class",
            "confirmed_uri", "review_status",
        )
        previous = []
        if review_file.exists():
            with review_file.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                previous = list(csv.DictReader(file))
        used = {
            row["source_handle"].strip().upper()
            for row in previous
            if row.get("review_status", "").strip().upper()
            == "APPROVED"
        }

        window = tk.Toplevel(self)
        window.title(f"Assign nozzle blocks — {owner_tag}")
        window.geometry("1300x780")
        ttk.Label(
            window,
            text=f"Owner: {owner_tag}. Click a nozzle block; "
                 "Ctrl-click selects several. Red = selected. "
                 "Verify each selection before saving.",
        ).pack(anchor="w", padx=10, pady=6)

        body = ttk.Frame(window, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        listing = tk.Listbox(
            body, selectmode="extended",
            exportselection=False, width=35,
            font=("Consolas", 10),
        )
        listing.grid(
            row=0, column=0, sticky="nsew", padx=(0, 8)
        )
        canvas = tk.Canvas(body, bg="white")
        canvas.grid(row=0, column=1, sticky="nsew")

        for item in candidates:
            saved = " [SAVED]" if item["handle"] in used else ""
            listing.insert(
                "end",
                f'{item["handle"]:>6}  {item["block"]}{saved}',
            )

        points = [
            point for item in candidates
            for part in item["paths"] for point in part
        ]
        min_x = min(x for x, y in points)
        max_x = max(x for x, y in points)
        min_y = min(y for x, y in points)
        max_y = max(y for x, y in points)
        canvas_items = {}

        def highlight(event=None):
            selected = set(listing.curselection())
            for index, ids in canvas_items.items():
                for canvas_id in ids:
                    canvas.itemconfigure(
                        canvas_id,
                        fill="red" if index in selected
                        else "#777777",
                        width=3 if index in selected else 1,
                    )
                    if index in selected:
                        canvas.tag_raise(canvas_id)

        def draw(event=None):
            width = max(canvas.winfo_width(), 100)
            height = max(canvas.winfo_height(), 100)
            margin = 25
            scale = min(
                (width - 2 * margin)
                / max(max_x - min_x, 1),
                (height - 2 * margin)
                / max(max_y - min_y, 1),
            )

            def transform(x, y):
                return (
                    margin + (x - min_x) * scale,
                    height - margin - (y - min_y) * scale,
                )

            canvas.delete("all")
            canvas_items.clear()
            for index, item in enumerate(candidates):
                ids = []
                for part in item["paths"]:
                    coordinates = []
                    for x, y in part:
                        coordinates.extend(transform(x, y))
                    ids.append(canvas.create_line(
                        *coordinates, fill="#777777", width=1
                    ))
                canvas_items[index] = ids
            highlight()

        def click(event):
            closest = canvas.find_closest(event.x, event.y)
            if not closest:
                return
            canvas_id = closest[0]
            box = canvas.bbox(canvas_id)
            if box is None or not (
                box[0] - 8 <= event.x <= box[2] + 8
                and box[1] - 8 <= event.y <= box[3] + 8
            ):
                return

            for index, ids in canvas_items.items():
                if canvas_id not in ids:
                    continue
                if event.state & 0x0004:
                    if index in listing.curselection():
                        listing.selection_clear(index)
                    else:
                        listing.selection_set(index)
                else:
                    listing.selection_clear(0, "end")
                    listing.selection_set(index)
                listing.see(index)
                highlight()
                break

        canvas.bind("<Configure>", draw)
        canvas.bind("<Button-1>", click)
        listing.bind("<<ListboxSelect>>", highlight)

        form = ttk.Frame(window, padding=10)
        form.pack(fill="x")
        name = tk.StringVar()
        selected_mapping = {"record": None}
        mapping_display = tk.StringVar()

        ttk.Label(form, text="Nozzle name (optional)").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(
            form, textvariable=name, width=32
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(form, text="Approved nozzle mapping").grid(
            row=1, column=0, sticky="w"
        )
        ttk.Entry(
            form, textvariable=mapping_display,
            width=100, state="readonly",
        ).grid(row=1, column=1, sticky="ew")

        def accept_mapping(record):
            selected_mapping["record"] = record
            mapping_display.set(
                f'{record["symbol"]} | '
                f'{record["class"]} | {record["uri"]}'
            )

        ttk.Button(
            form, text="Choose class / URI",
            command=lambda: searchable_mapping_picker(
                window, mapping_records, accept_mapping
            ),
        ).grid(row=1, column=2, padx=5)

        form.columnconfigure(1, weight=1)


        def save():
            selected = listing.curselection()
            if not selected or selected_mapping["record"] is None:
                messagebox.showerror(
                    "Incomplete",
                    "Select nozzle blocks and a nozzle mapping.",
                    parent=window,
                )
                return
            if len(selected) > 1 and name.get().strip():
                messagebox.showerror(
                    "Name applies to one nozzle",
                    "Select one block when entering a nozzle name.",
                    parent=window,
                )
                return

            chosen = [candidates[i] for i in selected]
            duplicates = [
                item["handle"] for item in chosen
                if item["handle"] in used
            ]
            if duplicates:
                messagebox.showerror(
                    "Already assigned",
                    "These handles are already approved: "
                    + ", ".join(duplicates),
                    parent=window,
                )
                return

            class_name = selected_mapping["record"]["class"]
            uri = selected_mapping["record"]["uri"]

            additions = [{
                "source_dxf": dxf_path.name,
                "source_handle": item["handle"],
                "block_name": item["block"],
                "owner_tag": owner_tag,
                "nozzle_name": name.get().strip(),
                "confirmed_class": class_name,
                "confirmed_uri": uri,
                "review_status": "APPROVED",
            } for item in chosen]

            temporary = review_file.with_name(
                review_file.name + ".tmp"
            )
            try:
                with temporary.open(
                    "w", newline="", encoding="utf-8-sig"
                ) as file:
                    writer = csv.DictWriter(
                        file, fieldnames=columns
                    )
                    writer.writeheader()
                    writer.writerows(previous + additions)
                temporary.replace(review_file)
            except Exception as error:
                messagebox.showerror(
                    "Save failed",
                    f"Close the CSV in Excel and retry.\n{error}",
                    parent=window,
                )
                return

            messagebox.showinfo(
                "Assignments saved",
                f"Saved {len(additions)} nozzle(s) under "
                f"{owner_tag}.\n{review_file}",
                parent=window,
            )
            window.destroy()

        ttk.Button(
            form, text="Approve selected nozzle blocks",
            command=save,
        ).grid(row=2, column=1, sticky="w", pady=8)


    def open_pipe_review(self):
        from pipe_review import open_pipe_review
        open_pipe_review(self)

    def run_export(self):
        try:
            from reviewed_equipment_export import export

            if not hasattr(self, "latest_folder"):
                raise ValueError("Run Scan selected files first")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = self.latest_folder / f"equipment_demo_{stamp}.xml"
            inputs = {
                name: variable.get().strip()
                for name, variable in self.fields.items()
            }
            inputs["Proteus XSD"] = str(BUNDLED_XSD)


            path, count = export(
                inputs["DXF"],
                self.latest_folder / "placed_symbols_REVIEW.csv",
                inputs["Mapping workbook"],
                inputs["Proteus XSD"],
                output,
            )
            messagebox.showinfo(
                "XSD validation passed",
                f"Generated {count} approved equipment item(s).\n"
                f"XML: {path}\n\nAVEVA import still needs testing.",
            )
        except Exception:
            details = traceback.format_exc()
            self.output.delete("1.0", "end")
            self.output.insert("end", details)
            messagebox.showerror(
                "Export stopped",
                "No validated XML was released. See the app window.",
            )


if __name__ == "__main__":
    App().mainloop()
