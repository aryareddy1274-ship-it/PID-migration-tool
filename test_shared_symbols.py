import csv
from collections import defaultdict
from pathlib import Path
from tkinter import Tk, filedialog
from shared_symbols import SymbolRegistry, check_placed_insert


import ezdxf

from shared_symbols import SymbolRegistry


def approved_rows(path):
    if not path.is_file():
        return []

    with path.open(newline="", encoding="utf-8-sig") as file:
        return [
            row for row in csv.DictReader(file)
            if row.get("review_status", "").strip().upper()
            == "APPROVED"
        ]


def main():
    window = Tk()
    window.withdraw()

    dxf_name = filedialog.askopenfilename(
        title="Select the DXF to test",
        filetypes=[("DXF drawing", "*.dxf")],
    )
    if not dxf_name:
        print("No DXF selected.")
        return

    run_name = filedialog.askdirectory(
        title="Select the folder containing the review CSVs"
    )
    if not run_name:
        print("No review folder selected.")
        return

    window.destroy()

    dxf_file = Path(dxf_name)
    run_folder = Path(run_name)
    doc = ezdxf.readfile(str(dxf_file))
    inserts = {
        item.dxf.handle.upper(): item
        for item in doc.modelspace().query("INSERT")
    }

    # Test only reviewed blocks with approved classes/URIs.
    requests = []

    for row in approved_rows(
        run_folder / "placed_symbols_REVIEW.csv"
    ):
        kind = row.get("item_kind", "Equipment").strip()
        element = (
            "ProcessInstrumentationFunction"
            if kind == "Instrument"
            else "Equipment"
            if kind == "Equipment"
            else None
        )
        if element:
            requests.append((
                row.get("source_handle", "").strip().upper(),
                element,
                row.get("confirmed_class", "").strip(),
                row.get("confirmed_uri", "").strip(),
            ))

    for row in approved_rows(run_folder / "nozzles_REVIEW.csv"):
        requests.append((
            row.get("source_handle", "").strip().upper(),
            "Nozzle",
            row.get("confirmed_class", "").strip(),
            row.get("confirmed_uri", "").strip(),
        ))

    skipped_components = 0
    for row in approved_rows(
        run_folder / "pipe_components_REVIEW.csv"
    ):
        handles = [
            part.strip().upper()
            for part in row.get("geometry_handles", "").split("|")
            if part.strip()
        ]
        if len(handles) == 1 and handles[0] in inserts:
            requests.append((
                handles[0],
                "PipingComponent",
                row.get("confirmed_class", "").strip(),
                row.get("confirmed_uri", "").strip(),
            ))
        else:
            # Loose or multi-entity components aren't DXF block INSERTs.
            skipped_components += 1

    registry = SymbolRegistry(tolerance=0.05)
    failures = []

    for handle, element, class_name, uri in requests:
        insert = inserts.get(handle)
        if insert is None:
            failures.append((handle, "INSERT not found in DXF"))
            continue

        try:
            placement = registry.add(
                insert, doc, element, class_name, uri
            )
            check_placed_insert(
                insert,
                placement,
                registry.definitions[placement.name]["paths"],
            )
        except Exception as error:
            failures.append((handle, str(error)))

    groups = defaultdict(list)
    for placement in registry.placements:
        groups[placement.name].append(placement)

    print(f"\nDXF: {dxf_file.name}")
    print(f"Review folder: {run_folder}")
    print(f"Approved block requests: {len(requests)}")
    print(f"Reconstructed successfully: {len(registry.placements)}")
    print(f"Proposed catalogue shapes: {len(registry.definitions)}")
    print(f"Loose/multi-entity components skipped: {skipped_components}")
    print(f"Failures: {len(failures)}")

    print("\nSHAPES USED BY MORE THAN ONE INSERT")
    repeated = 0
    for name, placements in sorted(groups.items()):
        if len(placements) < 2:
            continue
        repeated += 1
        print(
            f"{name}: "
            + ", ".join(
                f"{item.element} {item.handle} ({item.angle:g}°)"
                for item in placements
            )
        )
    if not repeated:
        print("None")

    if failures:
        print("\nFAILURES — DO NOT CONNECT THIS TO XML YET")
        for handle, reason in failures:
            print(f"{handle or '(blank handle)'}: {reason}")

    print("\nNo XML or review CSV was changed.")


if __name__ == "__main__":
    main()
