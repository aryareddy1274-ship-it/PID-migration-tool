import csv
import re
from collections import Counter
from pathlib import Path

import ezdxf
from lxml import etree


# These two class URIs were checked against the supplied, working
# Proteus reference XML; they are not drawing-specific pipe tags.
SYSTEM_URI = "http://data.posccaesar.org/rdl/RDS270359"
SEGMENT_URI = "http://data.posccaesar.org/rdl/RDS267704"


def point(parent, name, x, y):
    return etree.SubElement(
        parent, name,
        X=f"{x:.6f}", Y=f"{y:.6f}", Z="0.000000",
    )


def extent(parent, points):
    box = etree.SubElement(parent, "Extent")
    point(box, "Min",
          min(x for x, y in points),
          min(y for x, y in points))
    point(box, "Max",
          max(x for x, y in points),
          max(y for x, y in points))


def presentation(parent):
    etree.SubElement(
        parent, "Presentation",
        Layer="AS_PIPE", LineType="Solid",
        LineWeight="0.25", Color="Aqua",
        R="0", G="1", B="1",
    )


def generic(parent, set_name, pairs):
    group = etree.SubElement(
        parent, "GenericAttributes",
        Set=set_name, Number=str(len(pairs)),
    )
    for entry in pairs:
        name, value = entry[:2]
        attributes = {
            "Name": name,
            "Format": "string",
            "Value": str(value),
        }
        if len(entry) == 3:
            attributes["Units"] = entry[2]
        etree.SubElement(
            group, "GenericAttribute", **attributes
        )


def dexpi_attributes(parent, pairs):
    group = etree.SubElement(
        parent, "GenericAttributes",
        Set="DexpiAttributes", Number=str(len(pairs)),
    )
    for name, value in pairs:
        etree.SubElement(
            group, "GenericAttribute",
            Name=name, Format="string", Value=str(value),
            AttributeURI="http://sandbox.dexpi.org/rdl/" + name,
        )


def vertices(entity):
    kind = entity.dxftype()
    if kind == "LINE":
        return [
            (entity.dxf.start.x, entity.dxf.start.y),
            (entity.dxf.end.x, entity.dxf.end.y),
        ]
    if kind == "LWPOLYLINE":
        if any(
            abs(p[4]) > 1e-9
            for p in entity.get_points("xyseb")
        ):
            raise ValueError(
                f"Curved polyline not supported yet: "
                f"{entity.dxf.handle}"
            )
        result = [
            (p[0], p[1])
            for p in entity.get_points("xy")
        ]
        if len(result) >= 2:
            return result
    raise ValueError(
        f"Unsupported approved pipe geometry: "
        f"{entity.dxf.handle} ({kind})"
    )


def add_approved_pipes(
    root, dxf_path, review_path, symbol_registry,
    component_review_path=None,
    mapping_path=None,
    add_symbol=None,
    flatten_entity=None,
):
    review_path = Path(review_path)
    if not review_path.is_file():
        return 0

    with review_path.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        rows = [
            row for row in csv.DictReader(file)
            if row["review_status"].strip().upper()
            == "APPROVED"
        ]
    if not rows:
        return 0

    doc = ezdxf.readfile(str(dxf_path))
    by_handle = {
        entity.dxf.handle.upper(): entity
        for entity in doc.modelspace()
    }
    used_handles = set()
    existing_ids = {
        node.get("ID")
        for node in root.iter()
        if node.get("ID")
    }

    # Insert networks before root-level Equipment, matching the
    # hierarchy/order seen in the working Proteus reference.
    insert_at = next(
        (
            index for index, child in enumerate(root)
            if child.tag == "Equipment"
        ),
        len(root),
    )
    systems = {}
    segment_numbers = Counter()
    segments_by_line = {}

    for row_number, row in enumerate(rows, start=1):
        tag = row["pipe_tag"].strip()
        specification = row["Specification"].strip()
        bore = row["Bore_mm"].strip()
        if not tag or not specification or not bore:
            raise ValueError(
                f"Pipe review row {row_number}: "
                "tag, specification and bore are required"
            )
        try:
            bore_value = float(bore)
        except ValueError as error:
            raise ValueError(
                f"{tag}: invalid bore {bore!r}"
            ) from error
        if bore_value <= 0:
            raise ValueError(f"{tag}: bore must be positive")

        handles = [
            handle.strip().upper()
            for handle in row["geometry_handles"].split("|")
            if handle.strip()
        ]
        if not handles:
            raise ValueError(f"{tag}: no reviewed DXF handles")
        if len(handles) != len(set(handles)):
            raise ValueError(f"{tag}: repeated handle in review")

        # Preserve the exact source pipe tag. The numerical suffix
        # is used only for an engineering Number attribute.
        match = re.fullmatch(r"(.+)-(\d+)", tag)
        if not match:
            raise ValueError(
                f"Pipe tag needs reviewed prefix/number parsing: {tag}"
            )
        fluid, number = match.groups()

        if tag not in systems:
            system_id = f"REVIEW_PIPE_SYSTEM_{len(systems) + 1}"
            if system_id in existing_ids:
                raise ValueError(f"Duplicate XML ID: {system_id}")
            system = etree.Element(
                "PipingNetworkSystem",
                ID=system_id,
                TagName=tag,
                ComponentClass="PipingNetworkSystem",
                ComponentClassURI=SYSTEM_URI,
                Specification=specification,
            )
            # Extent and presentation must precede segments.
            extent(system, [(0, 0), (0, 0)])
            presentation(system)
            etree.SubElement(
                system, "PersistentID",
                Identifier=system_id,
                Context=Path(dxf_path).stem,
            )
            generic(
                system, "PipingNetworkSystem",
                [
                    ("Fluid", fluid),
                    ("Number", number),
                    ("Specification", specification),
                ],
            )
            dexpi_attributes(
                system,
                [
                    ("LineNumberAssignmentClass", number),
                    ("PipingClassCodeAssignmentClass",
                     specification),
                    ("FluidCodeAssignmentClass", fluid),
                ],
            )

            

            root.insert(insert_at, system)
            insert_at += 1
            systems[tag] = {
                "element": system,
                "specification": specification,
                "points": [],
                "bores": set(),
            }

        saved = systems[tag]
        if saved["specification"] != specification:
            raise ValueError(
                f"{tag}: conflicting specifications in review rows"
            )
        saved["bores"].add(bore_value)

        # One XML segment per reviewed DXF entity. This preserves
        # separate islands; it creates NO Connection elements.
        for handle in handles:
            if handle in used_handles:
                raise ValueError(
                    f"Pipe DXF handle assigned twice: {handle}"
                )
            entity = by_handle.get(handle)
            if entity is None:
                raise ValueError(
                    f"{tag}: DXF handle not found: {handle}"
                )
            points = vertices(entity)
            used_handles.add(handle)
            saved["points"].extend(points)

            segment_numbers[tag] += 1
            sequence = segment_numbers[tag]
            segment_id = f"REVIEW_PIPE_SEG_{row_number}_{sequence}"
            if segment_id in existing_ids:
                raise ValueError(
                    f"Duplicate XML ID: {segment_id}"
                )
            segment = etree.SubElement(
                saved["element"], "PipingNetworkSegment",
                ID=segment_id,
                TagName=f"{tag}-{specification}-{bore}",

                ComponentClass="PipingNetworkSegment",
                ComponentClassURI=SEGMENT_URI,
                Specification=specification,
            )
            segments_by_line[(tag, handle)] = segment

            extent(segment, points)
            presentation(segment)
            etree.SubElement(
                segment, "PersistentID",
                Identifier=segment_id,
                Context=Path(dxf_path).stem,
            )
            segment_pairs = [
                ("Fluid", fluid),
                ("Number", number),
                ("Size", bore, "mm"),
                ("Specification", specification),
            ]
            insulation_type = row.get(
                "InsulationType", ""
            ).strip()
            insulation_thickness = row.get(
                "InsulationThickness_mm", ""
            ).strip()
            if insulation_type:
                segment_pairs.append(
                    ("Insulation_Table",
                     insulation_type + insulation_thickness)
                )

            generic(
                segment, "PipingNetworkSegment",
                segment_pairs,
            )
            segment_dexpi = [
                ("SegmentNumberAssignmentClass",
                 f"S{sequence}"),
                (
                    "NominalDiameterNumericalValueRepresentation"
                    "AssignmentClass",
                    bore,
                ),
                ("PipingClassCodeAssignmentClass",
                 specification),
                ("FluidCodeAssignmentClass", fluid),
            ]
            if insulation_type:
                segment_dexpi.append(
                    ("InsulationTypeAssignmentClass",
                     insulation_type)
                )
            dexpi_attributes(segment, segment_dexpi)


            centerline = etree.SubElement(

                segment, "CenterLine",
                NumPoints=str(len(points)),
            )
            presentation(centerline)
            extent(centerline, points)
            for x, y in points:
                point(centerline, "Coordinate", x, y)

            etree.SubElement(
                segment, "NominalDiameter",
                Value=f"{bore_value:.6f}", Units="mm",
            )

    for saved in systems.values():
        system = saved["element"]
        old_extent = system.find("Extent")
        system.remove(old_extent)
        new_extent = etree.Element("Extent")
        point(
            new_extent, "Min",
            min(x for x, y in saved["points"]),
            min(y for x, y in saved["points"]),
        )
        point(
            new_extent, "Max",
            max(x for x, y in saved["points"]),
            max(y for x, y in saved["points"]),
        )
        system.insert(0, new_extent)

        # The reference has a network-level diameter for a single
        # bore. A changing-bore network needs explicit review later.
        if len(saved["bores"]) == 1:
            single_bore = next(iter(saved["bores"]))
            network_generic = next(
                group for group in system.findall(
                    "GenericAttributes"
                )
                if group.get("Set")
                == "PipingNetworkSystem"
            )
            etree.SubElement(
                network_generic, "GenericAttribute",
                Name="Size", Format="string",
                Value=f"{single_bore:g}", Units="mm",
            )
            network_generic.set(
                "Number", str(len(network_generic))
            )

            etree.SubElement(
                system, "NominalDiameter",
                Value=f"{next(iter(saved['bores'])):.6f}",
                Units="mm",
            )
    if component_review_path is not None:
        add_approved_components(
            root=root,
            doc=doc,
            dxf_path=Path(dxf_path),
            review_path=Path(component_review_path),
            mapping_path=Path(mapping_path),
            segments_by_line=segments_by_line,
            add_symbol=add_symbol,
            flatten_entity=flatten_entity,
            symbol_registry=symbol_registry,
        )

    return len(rows)
def add_approved_components(
    root, doc, dxf_path, review_path, mapping_path,
    segments_by_line, add_symbol, flatten_entity, symbol_registry,
):
    """Export reviewed components without inventing connections."""
    review_path = Path(review_path)
    if not review_path.is_file():
        return 0
    if add_symbol is None or flatten_entity is None:
        raise ValueError("Component XML geometry functions unavailable")

    import xml.etree.ElementTree as ET
    from openpyxl import load_workbook
    from reviewed_equipment_export import add_shared_block_symbol

    book = load_workbook(
        mapping_path, read_only=True, data_only=True
    )
    try:
        allowed = {
            (
                "".join(filter(str.isalnum, str(row[3] or "")))
                .casefold(),
                str(row[4] or "").strip(),
            )
            for row in book.active.iter_rows(
                min_row=2, values_only=True
            )
            if len(row) >= 5 and row[3] and row[4]
        }
    finally:
        book.close()

    with review_path.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        decisions = [
            row for row in csv.DictReader(file)
            if row.get("review_status", "").strip().upper()
            == "APPROVED"
        ]

    catalogue = root.find("ShapeCatalogue")
    if catalogue is None:
        raise ValueError("No ShapeCatalogue for piping components")

    by_handle = {
        entity.dxf.handle.upper(): entity
        for entity in doc.modelspace()
    }
    used_geometry = set()
    count = 0

    for row_number, row in enumerate(decisions, start=2):
        tag = row.get("pipe_tag", "").strip()
        owner_handle = row.get(
            "owner_line_handle", ""
        ).strip().upper()
        class_name = row.get(
            "confirmed_class", ""
        ).strip()
        uri = row.get("confirmed_uri", "").strip()
        label = row.get("component_label", "").strip()
        raw_handles = [
            value.strip().upper()
            for value in row.get(
                "geometry_handles", ""
            ).split("|")
            if value.strip()
        ]

        if row.get("source_dxf", "").strip() != dxf_path.name:
            raise ValueError(
                f"Component row {row_number}: source DXF differs"
            )
        if not all((
            tag, owner_handle, class_name, uri, raw_handles
        )):
            raise ValueError(
                f"Component row {row_number}: incomplete approval"
            )
        if len(raw_handles) != len(set(raw_handles)):
            raise ValueError(
                f"Component row {row_number}: duplicate geometry"
            )

        approved_pair = (
            "".join(filter(str.isalnum, class_name))
            .casefold(),
            uri,
        )
        if approved_pair not in allowed:
            raise ValueError(
                f"Component row {row_number}: class/URI "
                "not in mapping workbook"
            )

        segment = segments_by_line.get((tag, owner_handle))
        if segment is None:
            raise ValueError(
                f"Component row {row_number}: {tag} / "
                f"{owner_handle} has no reviewed pipe segment"
            )

        entities = []
        for handle in raw_handles:
            if handle in used_geometry:
                raise ValueError(
                    f"Component geometry reused: {handle}"
                )
            if handle == owner_handle:
                raise ValueError(
                    f"Pipe line {handle} cannot also be "
                    "component geometry"
                )
            entity = by_handle.get(handle)
            if entity is None:
                raise ValueError(
                    f"Component DXF handle absent: {handle}"
                )
            entities.append(entity)

        # Every approved single-block INSERT uses the same registry,
        # regardless of its engineering class or block name.
        if len(entities) == 1 and entities[0].dxftype() == "INSERT":
            temporary_parent = etree.Element("TemporaryParent")
            instance = add_shared_block_symbol(
                catalogue,
                temporary_parent,
                symbol_registry,
                entities[0],
                doc,
                label,
                "PipingComponent",
                class_name,
                uri,
            )
        else:
            # A collection of loose strokes is not one DXF block.
            # Preserve the existing reviewed-geometry behaviour.
            paths = []
            for entity in entities:
                if entity.dxftype() == "INSERT":
                    for virtual in entity.virtual_entities():
                        paths.extend(flatten_entity(virtual))
                else:
                    paths.extend(flatten_entity(entity))

            paths = [
                part for part in paths if len(part) >= 2
            ]
            if not paths:
                raise ValueError(
                    f"Component row {row_number}: no supported "
                    "drawable DXF geometry"
                )

            points = [
                point for part in paths for point in part
            ]
            origin = (
                (min(x for x, y in points)
                 + max(x for x, y in points)) / 2,
                (min(y for x, y in points)
                 + max(y for x, y in points)) / 2,
            )
            component_name = (
                "DXF_PIPE_COMPONENT_" + "_".join(raw_handles)
            )

            temp_catalogue = ET.Element("TempCatalogue")
            temp_parent = ET.Element("TempParent")
            if not add_symbol(
                temp_catalogue,
                temp_parent,
                label, class_name, uri,
                component_name, paths, origin,
                "PipingComponent",
            ):
                raise ValueError(
                    f"Component row {row_number}: symbol failed"
                )

            definition = etree.fromstring(
                ET.tostring(temp_catalogue[0])
            )
            instance = etree.fromstring(
                ET.tostring(temp_parent[0])
            )
            catalogue.append(definition)

        used_geometry.update(raw_handles)

        instance.set(
            "Specification",
            segment.get("Specification", ""),
        )

        etree.SubElement(
            instance, "PersistentID",
            Identifier=instance.get("ID"),
            Context=dxf_path.stem,
        )

        segment_generic = next(
            (
                group for group in segment.findall(
                    "GenericAttributes"
                )
                if group.get("Set")
                == "PipingNetworkSegment"
            ),
            None,
        )
        values = {}
        if segment_generic is not None:
            values = {
                item.get("Name"): item.get("Value")
                for item in segment_generic.findall(
                    "GenericAttribute"
                )
            }

        attributes = [
            ("Fluid", values.get("Fluid", "")),
            ("Size", values.get("Size", "")),
            ("Specification",
             segment.get("Specification", "")),
        ]
        generic_group = etree.SubElement(
            instance, "GenericAttributes",
            Set=class_name,
            Number=str(len(attributes)),
        )
        for name, value in attributes:
            options = {
                "Name": name,
                "Format": "string",
                "Value": value,
            }
            if name == "Size" and value:
                options["Units"] = "mm"
            etree.SubElement(
                generic_group, "GenericAttribute", **options
            )

        dexpi = etree.SubElement(
            instance, "GenericAttributes",
            Set="DexpiAttributes", Number="2",
        )
        for name, value in (
            ("PipingClassCodeAssignmentClass",
             segment.get("Specification", "")),
            ("FluidCodeAssignmentClass",
             values.get("Fluid", "")),
        ):
            etree.SubElement(
                dexpi, "GenericAttribute",
                Name=name, Format="string",
                Value=value,
                AttributeURI=(
                    "http://sandbox.dexpi.org/rdl/" + name
                ),
            )

        # The existing segment ends in NominalDiameter.
        diameter = segment.find("NominalDiameter")
        if diameter is None:
            raise ValueError(
                f"{tag}: segment has no NominalDiameter"
            )
        segment.insert(
            list(segment).index(diameter),
            instance,
        )
        count += 1

    return count
