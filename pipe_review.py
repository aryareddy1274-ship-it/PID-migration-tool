import csv
from collections import Counter, defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import ezdxf
from openpyxl import load_workbook
from ezdxf import path as ezdxf_path
from pid_converter_app import mapping_rows, searchable_mapping_picker



COLUMNS = (
    "source_dxf", "pipe_tag", "geometry_handles",
    "Bore_mm", "Specification", "InsulationType",
    "InsulationThickness_mm", "endpoint_tolerance",
    "continuity_warning", "review_status",
)
SIGNAL_COLUMNS = (
    "source_dxf", "geometry_handles", "signal_type",
    "logical_start", "logical_end", "review_status",
)


def open_pipe_review(app):
    if not hasattr(app, "latest_folder"):
        messagebox.showerror(
            "No run", "Scan or open an existing scan folder first."
        )
        return

    dxf_path = Path(app.fields["DXF"].get().strip())
    tag_path = Path(app.fields["Tag workbook"].get().strip())
    if not dxf_path.is_file() or not tag_path.is_file():
        messagebox.showerror(
            "Missing input", "Select a valid DXF and tag workbook."
        )
        return

    book = load_workbook(tag_path, read_only=True, data_only=True)
    if "Pipe" not in book:
        messagebox.showerror(
            "Missing sheet", "Tag workbook has no Pipe sheet."
        )
        return

    sheet = book["Pipe"]
    values = sheet.iter_rows(values_only=True)
    headers = [str(x or "").strip() for x in next(values)]
    pipe_records = {}
    for row_number, cells in enumerate(values, start=2):
        record = {
            heading: str(value).strip() if value is not None else ""
            for heading, value in zip(headers, cells)
        }
        if not record.get("Name"):
            continue
        label = (
            f'{record["Name"]} | Bore {record.get("Bore_mm", "")} mm'
            f' | Spec {record.get("Specification", "")}'
            f' | workbook row {row_number}'
        )
        pipe_records[label] = record

    doc = ezdxf.readfile(str(dxf_path))
    lines = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "LINE":
            vertices = [
                (entity.dxf.start.x, entity.dxf.start.y),
                (entity.dxf.end.x, entity.dxf.end.y),
            ]
        elif kind == "LWPOLYLINE":
            # This review uses the DXF's vertices. Curved/bulged
            # polylines require separate handling before XML export.
            if any(abs(p[4]) > 1e-9 for p in
                   entity.get_points("xyseb")):
                continue
            vertices = [
                (p[0], p[1])
                for p in entity.get_points("xy")
            ]
        else:
            continue

        if len(vertices) < 2:
            continue
        lines.append({
            "handle": entity.dxf.handle.upper(),
            "vertices": vertices,
            "start": vertices[0],
            "end": vertices[-1],
        })

    if not lines or not pipe_records:
        messagebox.showerror(
            "Nothing to review",
            "No straight lines/polylines or Pipe tags found.",
        )
        return

    window = tk.Toplevel(app)
    window.title("Assign reviewed DXF linework to pipe tags")
    window.geometry("1400x800")

    top = ttk.Frame(window, padding=8)
    top.pack(fill="x")
    ttk.Label(top, text="Linework type").pack(side="left")
    linework_type = tk.StringVar(value="Pipe")
    mode_picker = ttk.Combobox(
        top, textvariable=linework_type,
        values=("Pipe", "Signal"),
        state="readonly", width=10,
    )
    mode_picker.pack(side="left", padx=8)

    pipe_label = ttk.Label(top, text="Pipe tag")
    pipe_label.pack(side="left")
    selected_tag = tk.StringVar()
    pipe_picker = ttk.Combobox(
        top, textvariable=selected_tag,
        values=sorted(pipe_records),
        state="readonly", width=32,
    )
    pipe_picker.pack(side="left", padx=8)

    signal_label = ttk.Label(top, text="Confirmed signal type")
    signal_type = tk.StringVar()
    signal_entry = ttk.Entry(
        top, textvariable=signal_type, width=25
    )

    ttk.Label(
        top,
        text="Ctrl-click lines to add/remove. Red = selected. "
             "Crossings are NOT assumed connected.",
    ).pack(side="left", padx=12)

    def change_mode(event=None):
        listing.selection_clear(0, "end")
        highlight()
        if linework_type.get() == "Signal":
            pipe_label.pack_forget()
            pipe_picker.pack_forget()
            signal_label.pack(side="left")
            signal_entry.pack(side="left", padx=8)
            approve_button.configure(
                text="Approve selected signal linework"
            )
        else:
            signal_label.pack_forget()
            signal_entry.pack_forget()
            pipe_label.pack(side="left")
            pipe_picker.pack(side="left", padx=8)
            approve_button.configure(
                text="Approve selected lines for this pipe tag"
            )

    mode_picker.bind("<<ComboboxSelected>>", change_mode)


    body = ttk.Frame(window, padding=8)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.columnconfigure(1, weight=3)
    body.rowconfigure(0, weight=1)

    listing = tk.Listbox(
        body, selectmode="extended",
        exportselection=False, font=("Consolas", 10),
    )
    listing.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    canvas = tk.Canvas(body, bg="white")
    canvas.grid(row=0, column=1, sticky="nsew")

    for item in lines:
        a, b = item["start"], item["end"]
        listing.insert(
            "end", f'{item["handle"]:>6}  '
                   f'({a[0]:.1f},{a[1]:.1f}) → '
                   f'({b[0]:.1f},{b[1]:.1f})'
        )

    xs = [x for item in lines for x, y in item["vertices"]]
    ys = [y for item in lines for x, y in item["vertices"]]
    bounds = min(xs), max(xs), min(ys), max(ys)
    drawn_ids = {}

    def highlight(event=None):
        selected = set(map(int, listing.curselection()))
        for index, canvas_id in drawn_ids.items():
            active = index in selected
            canvas.itemconfigure(
                canvas_id,
                fill="red" if active else "#888888",
                width=3 if active else 1,
            )
            if active:
                canvas.tag_raise(canvas_id)

    def draw(event=None):
        width = max(canvas.winfo_width(), 100)
        height = max(canvas.winfo_height(), 100)
        min_x, max_x, min_y, max_y = bounds
        margin = 20
        scale = min(
            (width - 2 * margin) / max(max_x - min_x, 1),
            (height - 2 * margin) / max(max_y - min_y, 1),
        )
        canvas.delete("all")
        drawn_ids.clear()
        for index, item in enumerate(lines):
            points = []
            for x, y in item["vertices"]:
                points.extend((
                    margin + (x - min_x) * scale,
                    height - margin - (y - min_y) * scale,
                ))
            drawn_ids[index] = canvas.create_line(
                *points, fill="#888888", width=1,
            )
        highlight()

    def click(event):
        nearest = canvas.find_closest(event.x, event.y)
        if not nearest:
            return
        canvas_id = nearest[0]
        box = canvas.bbox(canvas_id)
        if not box or not (
            box[0] - 7 <= event.x <= box[2] + 7
            and box[1] - 7 <= event.y <= box[3] + 7
        ):
            return
        index = next(
            (i for i, item_id in drawn_ids.items()
             if item_id == canvas_id),
            None,
        )
        if index is None:
            return
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

    listing.bind("<<ListboxSelect>>", highlight)
    canvas.bind("<Configure>", draw)
    canvas.bind("<Button-1>", click)
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


    status = tk.StringVar(value="No pipe selected yet.")
    ttk.Label(
        window, textvariable=status, wraplength=1300,
    ).pack(anchor="w", padx=10, pady=5)

    def save_selection():
        choice = selected_tag.get()
        chosen_indexes = listing.curselection()
        if choice not in pipe_records or not chosen_indexes:

            messagebox.showerror(
                "Incomplete",
                "Choose a pipe tag and select its linework.",
                parent=window,
            )
            return
        info = pipe_records[choice]
        tag = info["Name"]

        chosen = [lines[i] for i in chosen_indexes]
        tolerance = 0.05  # DXF drawing units; recorded in CSV.
        def key(point):
            return (
                round(point[0] / tolerance),
                round(point[1] / tolerance),
            )

        endpoint_counts = Counter()
        adjacency = defaultdict(set)
        for item in chosen:
            a, b = key(item["start"]), key(item["end"])
            endpoint_counts[a] += 1
            endpoint_counts[b] += 1
            adjacency[a].add(b)
            adjacency[b].add(a)

        unvisited = set(adjacency)
        islands = 0
        while unvisited:
            islands += 1
            pending = [unvisited.pop()]
            while pending:
                node = pending.pop()
                for neighbour in adjacency[node] & unvisited:
                    unvisited.remove(neighbour)
                    pending.append(neighbour)

        loose_ends = sum(
            count == 1 for count in endpoint_counts.values()
        )
        branches = sum(
            count > 2 for count in endpoint_counts.values()
        )
        warning = (
            f"{islands} endpoint-connected group(s); "
            f"{loose_ends} free end(s); "
            f"{branches} branch point(s). "
            "Equipment/valve/nozzle connections UNRESOLVED."
        )
        status.set(warning)

        if islands > 1 or branches:
            if not messagebox.askyesno(
                "Review continuity",
                warning + "\n\nApprove this selection anyway?",
                parent=window,
            ):
                return

        path = app.latest_folder / "pipes_REVIEW.csv"
        previous = []
        if path.exists():
            with path.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                previous = list(csv.DictReader(file))

        new_handles = {item["handle"] for item in chosen}
        signal_file = app.latest_folder / "signals_REVIEW.csv"
        if signal_file.exists():
            with signal_file.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                for signal_row in csv.DictReader(file):
                    if (
                        signal_row.get("review_status", "")
                        .strip().upper() != "APPROVED"
                    ):
                        continue
                    signal_handles = {
                        value.strip().upper()
                        for value in signal_row.get(
                            "geometry_handles", ""
                        ).split("|")
                        if value.strip()
                    }
                    if new_handles & signal_handles:
                        messagebox.showerror(
                            "Handle already assigned",
                            "A selected line is already an "
                            "approved signal.",
                            parent=window,
                        )
                        return

        for row in previous:
            if row["review_status"] != "APPROVED":
                continue
            
            used = set(row["geometry_handles"].split("|"))
            if used & new_handles:
                messagebox.showerror(
                    "Handle already assigned",
                    "A selected line belongs to another approved pipe.",
                    parent=window,
                )
                return

        
        previous.append({
            "source_dxf": dxf_path.name,
            "pipe_tag": tag,
            "geometry_handles": "|".join(sorted(new_handles)),
            "Bore_mm": info.get("Bore_mm", ""),
            "Specification": info.get("Specification", ""),
            "InsulationType": info.get("InsulationType", ""),
            "InsulationThickness_mm":
                info.get("InsulationThickness_mm", ""),
            "endpoint_tolerance": str(tolerance),
            "continuity_warning": warning,
            "review_status": "APPROVED",
        })
        temporary = path.with_suffix(".csv.tmp")
        with temporary.open(
            "w", newline="", encoding="utf-8-sig"
        ) as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(previous)
        temporary.replace(path)
        messagebox.showinfo(
            "Pipe selection saved",
            f"{tag}: {len(chosen)} DXF entities.\n{warning}\n\n"
            f"Saved to: {path}",
            parent=window,
        )
        listing.selection_clear(0, "end")
        highlight()
    def save_signal():
        chosen_indexes = listing.curselection()
        confirmed_type = signal_type.get().strip()

        if not chosen_indexes or not confirmed_type:
            messagebox.showerror(
                "Incomplete signal review",
                "Select signal strokes and enter their confirmed "
                "signal type (for example, ELECTRIC only if verified).",
                parent=window,
            )
            return

        selected_handles = {
            lines[index]["handle"] for index in chosen_indexes
        }

        pipe_file = app.latest_folder / "pipes_REVIEW.csv"
        if pipe_file.exists():
            with pipe_file.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                for pipe_row in csv.DictReader(file):
                    if (
                        pipe_row.get("review_status", "")
                        .strip().upper() != "APPROVED"
                    ):
                        continue
                    pipe_handles = {
                        value.strip().upper()
                        for value in pipe_row.get(
                            "geometry_handles", ""
                        ).split("|")
                        if value.strip()
                    }
                    if selected_handles & pipe_handles:
                        messagebox.showerror(
                            "Handle already assigned",
                            "A selected line is already an "
                            "approved pipe.",
                            parent=window,
                        )
                        return

        output = app.latest_folder / "signals_REVIEW.csv"
        previous = []
        if output.exists():
            with output.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                previous = list(csv.DictReader(file))

        for row in previous:
            if (
                row.get("review_status", "").strip().upper()
                != "APPROVED"
            ):
                continue
            used = {
                value.strip().upper()
                for value in row.get(
                    "geometry_handles", ""
                ).split("|")
                if value.strip()
            }
            if selected_handles & used:
                messagebox.showerror(
                    "Signal already assigned",
                    "A selected stroke belongs to another "
                    "approved signal.",
                    parent=window,
                )
                return

        previous.append({
            "source_dxf": dxf_path.name,
            "geometry_handles": "|".join(
                sorted(selected_handles)
            ),
            "signal_type": confirmed_type,
            "logical_start": "UNRESOLVED",
            "logical_end": "UNRESOLVED",
            "review_status": "APPROVED",
        })

        temporary = output.with_name(output.name + ".tmp")
        try:
            with temporary.open(
                "w", newline="", encoding="utf-8-sig"
            ) as file:
                writer = csv.DictWriter(
                    file, fieldnames=SIGNAL_COLUMNS
                )
                writer.writeheader()
                writer.writerows(previous)
            temporary.replace(output)
        except Exception as error:
            messagebox.showerror(
                "Save failed", str(error), parent=window
            )
            return

        messagebox.showinfo(
            "Signal geometry saved",
            "Signal strokes saved; logical endpoints remain "
            f"UNRESOLVED.\n{output}",
            parent=window,
        )
        listing.selection_clear(0, "end")
        highlight()

    def approve_linework():
        if linework_type.get() == "Signal":
            save_signal()
        else:
            save_selection()

    approve_button = ttk.Button(
        window,
        text="Approve selected lines for this pipe tag",
        command=approve_linework,
    )
    approve_button.pack(pady=8)


    ttk.Button(
        window,
        text="Review Piping Components on approved pipes",
        command=lambda: review_pipe_components(app, window),
    ).pack(pady=5)
    def open_connections():
        from connection_review import open_connection_review
        open_connection_review(app, window)

    ttk.Button(
        window,
        text="Review candidate pipe connections",
        command=open_connections,
    ).pack(pady=5)

def review_pipe_components(app, parent):
    folder = Path(app.latest_folder)
    dxf_path = Path(app.fields["DXF"].get().strip())
    mapping_path = Path(
        app.fields["Mapping workbook"].get().strip()
    )
    pipe_file = folder / "pipes_REVIEW.csv"

    if not pipe_file.is_file():
        messagebox.showerror(
            "No approved pipes",
            "Review and save pipe linework first.",
            parent=parent,
        )
        return

    with pipe_file.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        pipe_rows = [
            row for row in csv.DictReader(file)
            if row.get("review_status", "").strip().upper()
            == "APPROVED"
        ]

    pipe_choices = {}
    for row in pipe_rows:
        for handle in row["geometry_handles"].split("|"):
            handle = handle.strip().upper()
            if handle:
                label = f'{row["pipe_tag"]} | line handle {handle}'
                pipe_choices[label] = (
                    row["pipe_tag"], handle
                )

    if not pipe_choices:
        messagebox.showerror(
            "No approved lines",
            "The pipe review contains no approved line handles.",
            parent=parent,
        )
        return

    mapping_records = mapping_rows(mapping_path)  # No class filter.
    mappings = [
        (record["symbol"], record["class"], record["uri"])
        for record in mapping_records
    ]



    doc = ezdxf.readfile(str(dxf_path))
    approved_pipe_handles = {
        handle for _, handle in pipe_choices.values()
    }

    excluded_handles = set(approved_pipe_handles)

    for filename, column in (
        ("placed_symbols_REVIEW.csv", "source_handle"),
        ("nozzles_REVIEW.csv", "source_handle"),
        ("fragmented_equipment_REVIEW.csv", "geometry_handles"),
    ):
        path = folder / filename
        if not path.is_file():
            continue
        with path.open(
            newline="", encoding="utf-8-sig"
        ) as file:
            for row in csv.DictReader(file):
                if (
                    row.get("review_status", "").strip().upper()
                    != "APPROVED"
                ):
                    continue
                excluded_handles.update(
                    value.strip().upper()
                    for value in row.get(column, "").split("|")
                    if value.strip()
                )

    def entity_paths(entity):
        if entity.dxftype() == "CIRCLE":
            import math

            cx = entity.dxf.center.x
            cy = entity.dxf.center.y
            radius = entity.dxf.radius
            steps = 64
            return [[
                (
                    cx + radius * math.cos(2 * math.pi * i / steps),
                    cy + radius * math.sin(2 * math.pi * i / steps),
                )
                for i in range(steps + 1)
            ]]

        if entity.dxftype() == "LINE":
            return [[
                (entity.dxf.start.x, entity.dxf.start.y),
                (entity.dxf.end.x, entity.dxf.end.y),
            ]]
        if entity.dxftype() not in {
            "ARC", "CIRCLE", "ELLIPSE",
            "LWPOLYLINE", "POLYLINE",
        }:
            return []
        try:
            path = ezdxf_path.make_path(entity)
            vertices = [
                (point.x, point.y)
                for point in path.flattening(0.08)
            ]
            return [vertices] if len(vertices) >= 2 else []
        except (TypeError, ValueError, AttributeError):
            return []

    candidates = []
    for entity in doc.modelspace():
        handle = entity.dxf.handle.upper()
        if handle in excluded_handles:
            continue

        paths = []
        if entity.dxftype() == "INSERT":
            for virtual in entity.virtual_entities():
                paths.extend(entity_paths(virtual))
        else:
            paths = entity_paths(entity)

        paths = [part for part in paths if len(part) >= 2]
        if paths:
            candidates.append({
                "handle": handle,
                "type": entity.dxftype(),
                "name": (
                    entity.dxf.name
                    if entity.dxftype() == "INSERT" else ""
                ),
                "paths": paths,
            })

    if not candidates:
        messagebox.showerror(
            "No geometry", "No drawable component candidates.",
            parent=parent,
        )
        return

    dialog = tk.Toplevel(parent)
    dialog.title("Review pipe components — select DXF geometry")
    dialog.geometry("1350x800")

    top = ttk.Frame(dialog, padding=8)
    top.pack(fill="x")
    ttk.Label(top, text="Owning reviewed pipe line").pack(
        side="left"
    )
    pipe_choice = tk.StringVar()
    pipe_picker = ttk.Combobox(
        top, textvariable=pipe_choice,
        values=sorted(pipe_choices),
        state="readonly", width=55,
    )
    pipe_picker.pack(side="left", padx=8)


    body = ttk.Frame(dialog, padding=8)
    body.pack(fill="both", expand=True)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)

    listing = tk.Listbox(
        body, selectmode="extended",
        exportselection=False, width=33,
        font=("Consolas", 10),
    )
    listing.grid(
        row=0, column=0, sticky="nsew", padx=(0, 8)
    )
    canvas = tk.Canvas(body, bg="white")
    canvas.grid(row=0, column=1, sticky="nsew")

    for item in candidates:
        listing.insert(
            "end",
            f'{item["handle"]:>6} {item["type"]:<12} '
            f'{item["name"]}',
        )

    all_points = [
        point for item in candidates
        for part in item["paths"] for point in part
    ]
    min_x = min(x for x, y in all_points)
    max_x = max(x for x, y in all_points)
    min_y = min(y for x, y in all_points)
    max_y = max(y for x, y in all_points)
    canvas_items = {}
    pipe_canvas_items = {}

    def highlight(event=None):
        selected = set(listing.curselection())
        for index, ids in canvas_items.items():
            for canvas_id in ids:
                canvas.itemconfigure(
                    canvas_id,
                    fill="red" if index in selected
                    else "#888888",
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
        pipe_canvas_items.clear()
        for handle in approved_pipe_handles:
            entity = doc.entitydb.get(handle)
            if entity is None:
                continue
            ids = []
            for part in entity_paths(entity):
                if len(part) < 2:
                    continue
                coordinates = []
                for x, y in part:
                    coordinates.extend(transform(x, y))
                ids.append(canvas.create_line(
                    *coordinates, fill="#c8c8c8", width=2
                ))
            pipe_canvas_items[handle] = ids

        for index, item in enumerate(candidates):
            ids = []
            for part in item["paths"]:
                coordinates = []
                for x, y in part:
                    coordinates.extend(transform(x, y))
                ids.append(canvas.create_line(
                    *coordinates, fill="#888888", width=1
                ))
            canvas_items[index] = ids
        highlight()
        highlight_owner_line()
    
    def highlight_owner_line(event=None):
        selected = pipe_choices.get(pipe_choice.get())
        selected_handle = selected[1] if selected else None

        for handle, ids in pipe_canvas_items.items():
            for canvas_id in ids:
                active = handle == selected_handle
                canvas.itemconfigure(
                    canvas_id,
                    fill="#0066cc" if active else "#c8c8c8",
                    width=4 if active else 2,
                )
                if active:
                    canvas.tag_raise(canvas_id)
    pipe_picker.bind(
        "<<ComboboxSelected>>", highlight_owner_line
    )


    def click(event):
        nearest = canvas.find_closest(event.x, event.y)
        if not nearest:
            return
        canvas_id = nearest[0]
        box = canvas.bbox(canvas_id)
        if box is None or not (
            box[0] - 8 <= event.x <= box[2] + 8
            and box[1] - 8 <= event.y <= box[3] + 8
        ):
            return

        for index, ids in canvas_items.items():
            if canvas_id not in ids:
                continue
            if event.state & 0x0004:  # Ctrl-click toggles
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

    def zoom_components(event):
        if event.delta == 0:
            return
        factor = 1.2 if event.delta > 0 else 1 / 1.2
        x = canvas.canvasx(event.x)
        y = canvas.canvasy(event.y)
        canvas.scale("all", x, y, factor, factor)
        canvas.configure(scrollregion=canvas.bbox("all"))

    def start_component_pan(event):
        canvas.scan_mark(event.x, event.y)

    def pan_components(event):
        canvas.scan_dragto(event.x, event.y, gain=1)

    canvas.bind("<MouseWheel>", zoom_components)
    canvas.bind("<ButtonPress-2>", start_component_pan)
    canvas.bind("<B2-Motion>", pan_components)

    form = ttk.Frame(dialog, padding=10)
    form.pack(fill="x")

    label = tk.StringVar()
    selected_mapping = {"record": None}
    mapping_display = tk.StringVar()

    ttk.Label(form, text="Source label (optional)").grid(
        row=0, column=0, sticky="w"
    )
    ttk.Entry(form, textvariable=label, width=30).grid(
        row=0, column=1, sticky="w"
    )

    ttk.Label(form, text="Approved class / URI mapping").grid(
        row=1, column=0, sticky="w"
    )
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
        form, text="Choose class / URI",
        command=lambda: searchable_mapping_picker(
            dialog, mapping_records, accept_mapping
        ),
    ).grid(row=1, column=2, padx=5)

    form.columnconfigure(1, weight=1)

    def save():
        selected = listing.curselection()
        if (
            pipe_choice.get() not in pipe_choices
            or selected_mapping["record"] is None

            or not selected
        ):
            messagebox.showerror(
                "Incomplete",
                "Select an owning pipe line, geometry, "
                "and approved mapping.",
                parent=dialog,
            )
            return

        pipe_tag, line_handle = pipe_choices[
            pipe_choice.get()
        ]
        class_name = selected_mapping["record"]["class"]
        uri = selected_mapping["record"]["uri"]

        selected_handles = [
            candidates[i]["handle"] for i in selected
        ]

        output = folder / "pipe_components_REVIEW.csv"
        columns = (
            "source_dxf", "pipe_tag",
            "owner_line_handle", "component_label",
            "geometry_handles", "confirmed_class",
            "confirmed_uri", "review_status",
        )
        previous = []
        if output.exists():
            with output.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                previous = list(csv.DictReader(file))

        already_used = {
            handle.strip().upper()
            for row in previous
            if row.get("review_status", "").upper()
            == "APPROVED"
            for handle in row["geometry_handles"].split("|")
        }
        if already_used.intersection(selected_handles):
            messagebox.showerror(
                "Geometry already assigned",
                "A selected DXF handle belongs to another "
                "approved component.",
                parent=dialog,
            )
            return

        previous.append({
            "source_dxf": dxf_path.name,
            "pipe_tag": pipe_tag,
            "owner_line_handle": line_handle,
            "component_label": label.get().strip(),
            "geometry_handles": "|".join(selected_handles),
            "confirmed_class": class_name,
            "confirmed_uri": uri,
            "review_status": "APPROVED",
        })
        temporary = output.with_name(output.name + ".tmp")
        try:
            with temporary.open(
                "w", newline="", encoding="utf-8-sig"
            ) as file:
                writer = csv.DictWriter(
                    file, fieldnames=columns
                )
                writer.writeheader()
                writer.writerows(previous)
            temporary.replace(output)
        except Exception as error:
            messagebox.showerror(
                "Save failed", str(error), parent=dialog
            )
            return

        messagebox.showinfo(
            "Component saved",
            f"Saved {class_name} for {pipe_tag}, "
            f"line handle {line_handle}.\n{output}",
            parent=dialog,
        )
        dialog.destroy()

    ttk.Button(
        form, text="Approve selected component geometry",
        command=save,
    ).grid(row=3, column=1, sticky="w", pady=8)
    