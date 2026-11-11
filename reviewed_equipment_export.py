import re
import xml.etree.ElementTree as ET
import csv
import shutil
import sys
import tempfile
import math
import ezdxf
from datetime import datetime
from pathlib import Path
from ezdxf import path as dxf_path

from lxml import etree
from openpyxl import load_workbook



def approved_pairs(workbook_path):
    book = load_workbook(workbook_path, read_only=True, data_only=True)
    pairs = set()
    for row in book.active.iter_rows(min_row=2, values_only=True):
        if len(row) > 4:
            class_name = str(row[3] or "").strip()
            uri = str(row[4] or "").strip()
            if class_name and uri:
                pairs.add((class_name, uri))
    return pairs

def symbol_sub(parent, name, **attributes):
    return ET.SubElement(
        parent, name,
        {key: str(value) for key, value in attributes.items()},
    )


def symbol_xyz(x, y, z=0.0):
    return {"X": f"{x:.6f}", "Y": f"{y:.6f}", "Z": f"{z:.6f}"}


def symbol_extent(parent, points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    box = symbol_sub(parent, "Extent")
    symbol_sub(box, "Min", **symbol_xyz(min(xs), min(ys)))
    symbol_sub(box, "Max", **symbol_xyz(max(xs), max(ys)))


def symbol_presentation(parent, layer):
    lime = layer in {"AS_EQU", "AS_INST"}
    symbol_sub(
        parent, "Presentation",
        Layer=layer, LineType="Solid", LineWeight="0.25",
        Color="Lime" if lime else "Aqua",
        R="0", G="1",
        B="0" if lime else "1",
    )


def symbol_position(parent, x, y, angle=0):
    position = symbol_sub(parent, "Position")
    symbol_sub(position, "Location", **symbol_xyz(x, y))
    symbol_sub(position, "Axis", **symbol_xyz(0, 0, 1))
    radians = math.radians(angle)
    symbol_sub(
        position, "Reference",
        **symbol_xyz(math.cos(radians), math.sin(radians)),
    )


def symbol_polyline(parent, points, layer):
    polyline = symbol_sub(parent, "PolyLine", NumPoints=len(points))
    symbol_presentation(polyline, layer)
    symbol_extent(polyline, points)
    for x, y in points:
        symbol_sub(polyline, "Coordinate", **symbol_xyz(x, y))


def add_symbol(
    catalogue, parent, tag, component_class, uri,
    component_name, world_paths, origin, element_name,
):
    """Build definition and instance from reviewed DXF-derived paths."""
    if not world_paths:
        return False

    ox, oy = origin
    local_paths = [
        [(x - ox, y - oy) for x, y in part]
        for part in world_paths
    ]
    local_points = [
        point for part in local_paths for point in part
    ]
    world_points = [
        point for part in world_paths for point in part
    ]
    layer = (
        "AS_INST" if element_name == "ProcessInstrumentationFunction"
        else "AS_EQU" if element_name in {"Equipment", "Nozzle"}
        else "AS_PIPE"
    )


    definition = symbol_sub(
        catalogue, element_name,
        ID="CAT_" + component_name,
        ComponentName=component_name,
        ComponentClass=component_class,
        ComponentClassURI=uri,
    )
    symbol_extent(definition, local_points)
    symbol_presentation(definition, layer)
    symbol_position(definition, 0, 0)
    for part in local_paths:
        symbol_polyline(definition, part, layer)

    attributes = {
        "ID": "INST_" + component_name,
        "ComponentName": component_name,
        "ComponentClass": component_class,
        "ComponentClassURI": uri,
    }
    if tag:
        attributes["TagName"] = tag

    instance = symbol_sub(parent, element_name, **attributes)
    symbol_extent(instance, world_points)
    if tag:
        generic = symbol_sub(
            instance, "GenericAttributes",
            Set=component_class, Number="1",
        )
        symbol_sub(
            generic, "GenericAttribute",
            Name="Tag", Format="string", Value=tag,
        )
    symbol_presentation(instance, layer)
    symbol_position(instance, ox, oy)
    symbol_sub(instance, "Scale", **symbol_xyz(1, 1, 1))
    return True
    
def flatten_entity(entity, tolerance=0.08):
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

def block_symbol_source(insert, doc):
    """Return one block-local shape and this INSERT's placement.

    Never use virtual_entities() of the placed INSERT as catalogue
    geometry: those entities have already been transformed.
    """
    name = insert.dxf.name
    if name not in doc.blocks:
        raise ValueError(f"INSERT {insert.dxf.handle}: missing block {name!r}")

    if (
        insert.dxf.get("row_count", 1) != 1
        or insert.dxf.get("column_count", 1) != 1
    ):
        raise ValueError(
            f"INSERT {insert.dxf.handle}: MINSERT arrays need "
            "separate placement handling"
        )

    extrusion = insert.dxf.get("extrusion", (0, 0, 1))
    if (
        abs(extrusion[0]) > 1e-9
        or abs(extrusion[1]) > 1e-9
        or abs(extrusion[2] - 1) > 1e-9
    ):
        raise ValueError(
            f"INSERT {insert.dxf.handle}: non-default extrusion "
            "is not supported yet"
        )

    block = doc.blocks[name]
    base = block.block.dxf.base_point
    paths = []

    for entity in block:
        if entity.dxftype() == "INSERT":
            # Expand a nested block in the OUTER block's coordinate
            # system. Do not use the placed outer INSERT here.
            for virtual in entity.virtual_entities():
                paths.extend(flatten_entity(virtual))
        else:
            paths.extend(flatten_entity(entity))

    local_paths = [
        [(x - base.x, y - base.y) for x, y in part]
        for part in paths
        if len(part) >= 2
    ]
    if not local_paths:
        raise ValueError(
            f"INSERT {insert.dxf.handle}: block {name!r} has "
            "no supported drawable geometry"
        )

    placement = {
        "x": float(insert.dxf.insert.x),
        "y": float(insert.dxf.insert.y),
        "rotation": float(insert.dxf.get("rotation", 0)),
        "xscale": float(insert.dxf.get("xscale", 1)),
        "yscale": float(insert.dxf.get("yscale", 1)),
    }
    if placement["xscale"] == 0 or placement["yscale"] == 0:
        raise ValueError(
            f"INSERT {insert.dxf.handle}: zero scale is invalid"
        )

    return name, local_paths, placement

def make_block_symbol(
    registry, insert, doc, tag, component_class, uri,
    element_name,
):
    """Return (new definition or None, unique placed instance).

    Identical block-local shape/class combinations share one
    ComponentName. Every INSERT keeps a unique instance ID.
    """
    import hashlib

    block_name, local_paths, placement = block_symbol_source(
        insert, doc
    )
    handle = insert.dxf.handle.upper()

    # Include the actual local geometry: anonymous/dynamic blocks
    # must not be merged merely because their names look similar.
    shape_key = (
        element_name,
        block_name,
        component_class,
        uri,
        tuple(
            tuple((round(x, 6), round(y, 6)) for x, y in part)
            for part in local_paths
        ),
    )
    digest = hashlib.sha256(
        repr(shape_key).encode("utf-8")
    ).hexdigest()[:20]
    component_name = "DXF_SHAPE_" + digest

    layer = (
        "AS_INST" if element_name == "ProcessInstrumentationFunction"
        else "AS_EQU" if element_name in {"Equipment", "Nozzle"}
        else "AS_PIPE"
    )

    definition = None
    if shape_key not in registry:
        definition = ET.Element(
            element_name,
            {
                "ID": "CAT_" + component_name,
                "ComponentName": component_name,
                "ComponentClass": component_class,
                "ComponentClassURI": uri,
            },
        )
        local_points = [
            point for part in local_paths for point in part
        ]
        symbol_extent(definition, local_points)
        symbol_presentation(definition, layer)
        symbol_position(definition, 0, 0)
        for part in local_paths:
            symbol_polyline(definition, part, layer)
        registry[shape_key] = component_name

    angle = math.radians(placement["rotation"])
    cosine, sine = math.cos(angle), math.sin(angle)

    def to_world(x, y):
        sx = x * placement["xscale"]
        sy = y * placement["yscale"]
        return (
            placement["x"] + sx * cosine - sy * sine,
            placement["y"] + sx * sine + sy * cosine,
        )

    world_points = [
        to_world(x, y)
        for part in local_paths for x, y in part
    ]

    attributes = {
        "ID": "INST_DXF_BLOCK_" + handle,
        "ComponentName": component_name,
        "ComponentClass": component_class,
        "ComponentClassURI": uri,
    }
    if tag:
        attributes["TagName"] = tag

    instance = ET.Element(element_name, attributes)
    symbol_extent(instance, world_points)
    symbol_presentation(instance, layer)
    symbol_position(
        instance,
        placement["x"],
        placement["y"],
        placement["rotation"],
    )
    symbol_sub(
        instance, "Scale",
        **symbol_xyz(
            placement["xscale"], placement["yscale"], 1
        ),
    )

    return definition, instance


def generate_base_xml(dxf_file, blocks, approved_handles, output_file):
    """Build base XML from approved blocks and actual DXF geometry."""
    doc = ezdxf.readfile(str(dxf_file))
    model = doc.modelspace()

    root = ET.Element(
        "PlantModel",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation":
                "ProteusPIDSchema_4.0.1.xsd",
        },
    )
    information = symbol_sub(
        root, "PlantInformation",
        SchemaVersion="4.0.1",
        OriginatingSystem="Reviewed DXF converter",
        Is3D="no", Discipline="PID", Units="mm",
    )
    symbol_sub(information, "UnitsOfMeasure", Distance="mm")
    symbol_extent(root, [(0, 0), (830, 580)])

    drawing = symbol_sub(
        root, "Drawing", Name=dxf_file.stem, Type="PID"
    )
    symbol_presentation(drawing, "0")
    symbol_extent(drawing, [(0, 0), (830, 580)])
    catalogue = symbol_sub(
        root, "ShapeCatalogue", Name="DXF-derived shapes"
    )

    produced = set()
    for insert in model.query("INSERT"):
        handle = insert.dxf.handle.upper()
        if handle not in approved_handles:
            continue

        block = insert.dxf.name
        if block not in blocks:
            raise ValueError(
                f"Approved handle {handle}: no approved block mapping "
                f"for {block}"
            )

        kind, class_name, uri = blocks[block]
        if kind != "Equipment":
            raise ValueError(
                f"Approved handle {handle} is not mapped as Equipment"
            )

        paths = []
        for virtual in insert.virtual_entities():
            paths.extend(flatten_entity(virtual))
        component_name = "DXF_BLOCK_" + handle
        origin = (insert.dxf.insert.x, insert.dxf.insert.y)

        if not add_symbol(
            catalogue, root,
            approved_handles[handle],
            class_name, uri, component_name,
            paths, origin, "Equipment",
        ):
            raise ValueError(
                f"Approved block {handle} has no drawable DXF geometry"
            )
        produced.add(handle)

    missing = set(approved_handles) - produced
    if missing:
        raise ValueError(
            "Approved INSERT handles absent from DXF: "
            + ", ".join(sorted(missing))
        )

    # Preserve loose source geometry as drawing-only graphics.
    for entity in model:
        if entity.dxftype() not in {
            "LINE", "LWPOLYLINE", "POLYLINE",
            "CIRCLE", "ARC", "ELLIPSE",
        }:
            continue
        for part in flatten_entity(entity):
            symbol_polyline(drawing, part, "0")

    # Preserve source text. Exact tag matches are moved under equipment
    # below, as in the previously working exporter.
    for entity in model:
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        raw = (
            entity.dxf.text if entity.dxftype() == "TEXT"
            else entity.text
        )
        if not raw:
            continue
        point = entity.dxf.insert
        height = (
            entity.dxf.get("height", 2.5)
            if entity.dxftype() == "TEXT"
            else entity.dxf.get("char_height", 2.5)
        )
        text = symbol_sub(
            drawing, "Text",
            String=raw, Font="Arial",
            Justification="LeftBottom",
            Width="1.000000",
            Height=f"{height:.6f}",
            TextAngle=f'{entity.dxf.get("rotation", 0):.6f}',
            SlantAngle="0.000000",
        )
        symbol_presentation(text, "AS_LAB")
        symbol_extent(text, [
            (point.x, point.y),
            (point.x + max(len(raw), 1) * 2, point.y + 3),
        ])
        symbol_position(text, point.x, point.y)

    for equipment in root.findall("Equipment"):
        tag = equipment.get("TagName", "")
        match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", tag)
        if not match:
            raise ValueError(
                f"Approved equipment tag needs reviewed parsing: {tag}"
            )
        prefix, number = match.groups()
        component_name = equipment.get("ComponentName")
        equipment.set("StockNumber", component_name)

        provisional = equipment.find("GenericAttributes")
        if provisional is not None:
            equipment.remove(provisional)

        attributes = symbol_sub(
            equipment, "GenericAttributes",
            Set=equipment.get("ComponentClass"), Number="4",
        )
        for name, value in (
            ("Number", number),
            ("TagType", prefix),
            ("TagSequenceNo", number),
            ("SymbolName", component_name),
        ):
            symbol_sub(
                attributes, "GenericAttribute",
                Name=name, Format="string", Value=value,
            )

        dexpi = symbol_sub(
            equipment, "GenericAttributes",
            Set="DexpiAttributes", Number="2",
        )
        for name, value in (
            ("TagNameSequenceNumberAssignmentClass", number),
            ("TagNamePrefixAssignmentClass", prefix),
        ):
            symbol_sub(
                dexpi, "GenericAttribute",
                Name=name, Format="string", Value=value,
                AttributeURI="http://sandbox.dexpi.org/rdl/" + name,
            )

        symbol_sub(
            equipment, "PersistentID",
            Identifier=equipment.get("ID"),
            Context=dxf_file.stem,
        )

        candidates = [
            text for text in drawing.findall("Text")
            if text.get("String") == tag
        ]
        if candidates:
            location = equipment.find("./Position/Location")
            ex = float(location.get("X"))
            ey = float(location.get("Y"))

            def distance_squared(text):
                point = text.find("./Position/Location")
                return (
                    (float(point.get("X")) - ex) ** 2
                    + (float(point.get("Y")) - ey) ** 2
                )

            label = min(candidates, key=distance_squared)
            drawing.remove(label)
            equipment.append(label)

    ET.ElementTree(root).write(
        str(output_file),
        encoding="utf-8",
        xml_declaration=True,
    )
    
def add_approved_nozzles(
    root, source_doc, review_file, mapping_path, dxf_path
):
    """Add reviewed DXF block nozzles inside their approved equipment."""
    review_file = Path(review_file)
    if not review_file.is_file():
        return 0

    allowed = {
        (
            "".join(filter(str.isalnum, class_name)).casefold(),
            uri,
        )
        for class_name, uri in approved_pairs(mapping_path)
    }
    inserts = {
        entity.dxf.handle.upper(): entity
        for entity in source_doc.modelspace().query("INSERT")
    }
    equipment_by_tag = {}
    for equipment in root.findall("Equipment"):
        tag = equipment.get("TagName", "")
        if tag in equipment_by_tag:
            raise ValueError(
                f"Cannot assign nozzles: duplicate equipment tag {tag}"
            )
        equipment_by_tag[tag] = equipment

    catalogue = root.find("ShapeCatalogue")
    if catalogue is None:
        raise ValueError("Cannot add nozzles: no ShapeCatalogue")

    used_handles = set()
    count = 0

    with review_file.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        for row in csv.DictReader(file):
            if (
                row.get("review_status", "").strip().upper()
                != "APPROVED"
            ):
                continue

            handle = row.get("source_handle", "").strip().upper()
            block_name = row.get("block_name", "").strip()
            owner_tag = row.get("owner_tag", "").strip()
            nozzle_name = row.get("nozzle_name", "").strip()
            class_name = row.get("confirmed_class", "").strip()
            uri = row.get("confirmed_uri", "").strip()

            if not all((
                handle, block_name, owner_tag, class_name, uri
            )):
                raise ValueError(
                    f"Incomplete approved nozzle row: {handle or row}"
                )
            if row.get("source_dxf", "").strip() != dxf_path.name:
                raise ValueError(
                    f"Nozzle {handle} belongs to a different DXF"
                )
            if handle in used_handles:
                raise ValueError(
                    f"Nozzle block approved more than once: {handle}"
                )
            used_handles.add(handle)

            approved_pair = (
                "".join(filter(str.isalnum, class_name)).casefold(),
                uri,
            )
            if approved_pair not in allowed:
                raise ValueError(
                    f"Nozzle {handle}: class/URI is not in SAMTEST"
                )

            owner = equipment_by_tag.get(owner_tag)
            if owner is None:
                raise ValueError(
                    f"Nozzle {handle}: approved owner equipment "
                    f"{owner_tag!r} is absent from generated XML"
                )

            insert = inserts.get(handle)
            if insert is None:
                raise ValueError(
                    f"Nozzle INSERT handle absent from DXF: {handle}"
                )
            if insert.dxf.name != block_name:
                raise ValueError(
                    f"Nozzle {handle}: block name changed from "
                    f"{block_name!r} to {insert.dxf.name!r}"
                )

            paths = []
            for virtual in insert.virtual_entities():
                paths.extend(flatten_entity(virtual))

            component_name = "DXF_BLOCK_" + handle
            temporary_catalogue = ET.Element("TemporaryCatalogue")
            temporary_parent = ET.Element("TemporaryParent")
            origin = (
                insert.dxf.insert.x,
                insert.dxf.insert.y,
            )

            if not add_symbol(
                temporary_catalogue,
                temporary_parent,
                nozzle_name, class_name, uri,
                component_name, paths, origin, "Nozzle",
            ):
                raise ValueError(
                    f"Nozzle {handle} has no drawable DXF geometry"
                )

            definition = etree.fromstring(
                ET.tostring(temporary_catalogue[0])
            )
            instance = etree.fromstring(
                ET.tostring(temporary_parent[0])
            )



            etree.SubElement(
                instance, "PersistentID",
                Identifier=instance.get("ID"),
                Context=dxf_path.stem,
            )

            catalogue.append(definition)
            owner.append(instance)
            count += 1

    return count

def add_approved_instruments(
    root, source_doc, instrument_rows, mapping_path, dxf_path
):
    """Create reviewed instrument bubbles from placed DXF blocks."""
    if not instrument_rows:
        return 0

    allowed = {
        (
            "".join(filter(str.isalnum, name)).casefold(),
            uri,
        )
        for name, uri in approved_pairs(mapping_path)
    }
    inserts = {
        entity.dxf.handle.upper(): entity
        for entity in source_doc.modelspace().query("INSERT")
    }
    catalogue = root.find("ShapeCatalogue")
    drawing = root.find("Drawing")
    if catalogue is None or drawing is None:
        raise ValueError("Missing ShapeCatalogue or Drawing")

    existing_ids = {
        element.get("ID") for element in root.iter()
        if element.get("ID")
    }
    used_handles = set()
    used_tags = set()
    loops_by_tag = {}

    for row in instrument_rows:
        handle = row["source_handle"].strip().upper()
        tag = row["confirmed_tag"].strip()
        class_name = row["confirmed_class"].strip()
        uri = row["confirmed_uri"].strip()
        block_name = row["block_name"].strip()

        if not all((handle, tag, class_name, uri, block_name)):
            raise ValueError(
                f"Incomplete approved instrument row: {handle or row}"
            )
        if row.get("source_dxf", "").strip() != dxf_path.name:
            raise ValueError(
                f"Instrument {handle} belongs to a different DXF"
            )
        if handle in used_handles or tag in used_tags:
            raise ValueError(
                f"Duplicate instrument handle or tag: {handle}, {tag}"
            )
        used_handles.add(handle)
        used_tags.add(tag)

        normalized = "".join(
            filter(str.isalnum, class_name)
        ).casefold()
        if normalized != "processinstrumentationfunction":
            raise ValueError(
                f"{tag}: this stage supports only "
                "ProcessInstrumentationFunction bubbles"
            )
        if (normalized, uri) not in allowed:
            raise ValueError(
                f"{tag}: class/URI is not in the mapping workbook"
            )

        insert = inserts.get(handle)
        if insert is None:
            raise ValueError(
                f"Instrument INSERT handle absent from DXF: {handle}"
            )
        if insert.dxf.name != block_name:
            raise ValueError(
                f"{tag}: reviewed block {block_name!r} differs "
                f"from DXF block {insert.dxf.name!r}"
            )

        paths = []
        for virtual in insert.virtual_entities():
            paths.extend(flatten_entity(virtual))
        if not paths:
            raise ValueError(
                f"{tag}: block {handle} has no drawable geometry"
            )

        component_name = "DXF_INSTRUMENT_" + handle
        instance_id = "INST_" + component_name
        catalogue_id = "CAT_" + component_name
        if instance_id in existing_ids or catalogue_id in existing_ids:
            raise ValueError(f"Duplicate instrument XML ID: {handle}")
        existing_ids.update((instance_id, catalogue_id))

        temp_catalogue = ET.Element("TemporaryCatalogue")
        temp_parent = ET.Element("TemporaryParent")
        origin = (insert.dxf.insert.x, insert.dxf.insert.y)

        if not add_symbol(
            temp_catalogue, temp_parent,
            tag, class_name, uri, component_name,
            paths, origin, "ProcessInstrumentationFunction",
        ):
            raise ValueError(f"{tag}: symbol generation failed")

        definition = etree.fromstring(
            ET.tostring(temp_catalogue[0])
        )
        instrument = etree.fromstring(
            ET.tostring(temp_parent[0])
        )
        instrument.set("StockNumber", component_name)

        # add_symbol's provisional Tag group precedes Presentation.
        # Replace it with attributes in the reference's instance order.
        provisional = instrument.find("GenericAttributes")
        if provisional is not None:
            instrument.remove(provisional)

        etree.SubElement(
            instrument, "PersistentID",
            Identifier=instrument.get("ID"),
            Context=dxf_path.stem,
        )

        match = re.fullmatch(r"([A-Za-z]+)-(.+)", tag)
        attributes = [("Symbol_Name", component_name)]
        if match:
            prefix, number = match.groups()
            attributes.extend((
                ("Number", number),
                ("TagType", prefix),
                ("Type",prefix),
                ("TagSequenceNo", number),
            ))

        generic = etree.SubElement(
            instrument, "GenericAttributes",
            Set="ProcessInstrumentationFunction",
            Number=str(len(attributes)),
        )
        for name, value in attributes:
            etree.SubElement(
                generic, "GenericAttribute",
                Name=name, Format="string", Value=value,
            )

        if match:
            dexpi = etree.SubElement(
                instrument, "GenericAttributes",
                Set="DexpiAttributes", Number="1",
            )
            etree.SubElement(
                dexpi, "GenericAttribute",
                Name="ProcessInstrumentationFunctionNumberAssignmentClass",
                Format="string",
                Value=number,
                AttributeURI=(
                    "http://sandbox.dexpi.org/rdl/"
                    "ProcessInstrumentationFunctionNumberAssignmentClass"
                ),
            )

        # Associate only an exact source text match. Split labels
        # require a separate review; do not invent their positions.
        label = next(
            (
                text for text in drawing.findall("Text")
                if text.get("String") == tag
            ),
            None,
        )
        if label is not None:
            drawing.remove(label)
            instrument.append(label)
        loop_tag = row.get("loop_tag", "").strip()
        if loop_tag:
            loop = loops_by_tag.get(loop_tag)

            if loop is None:
                loop_match = re.fullmatch(r"([A-Za-z]+)-(.+)", loop_tag)
                if not loop_match:
                    raise ValueError(
                        f"{tag}: review the loop tag {loop_tag!r}; "
                        "expected a prefix, hyphen, and number"
                    )
                loop_number = loop_match.group(2)

                loop_id = f"REVIEW_INSTRUMENT_LOOP_{len(loops_by_tag) + 1}"
                if loop_id in existing_ids:
                    raise ValueError(f"Duplicate loop XML ID: {loop_id}")
                existing_ids.add(loop_id)

                # Class and URI are taken from the supplied working
                # Proteus reference, not inferred from the tag.
                loop = etree.Element(
                    "InstrumentationLoopFunction",
                    ID=loop_id,
                    TagName=loop_tag,
                    ComponentClass="InstrumentationLoopFunction",
                    ComponentClassURI=(
                        "http://sandbox.dexpi.org/rdl/"
                        "InstrumentationLoopFunction"
                    ),
                )
                etree.SubElement(
                    loop, "PersistentID",
                    Identifier=loop_id,
                    Context=dxf_path.stem,
                )
                loop_attrs = etree.SubElement(
                    loop, "GenericAttributes",
                    Set="DexpiAttributes", Number="1",
                )
                etree.SubElement(
                    loop_attrs, "GenericAttribute",
                    Name="InstrumentationLoopFunctionNumberAssignmentClass",
                    Format="string",
                    Value=loop_number,
                    AttributeURI=(
                        "http://sandbox.dexpi.org/rdl/"
                        "InstrumentationLoopFunctionNumberAssignmentClass"
                    ),
                )
                loops_by_tag[loop_tag] = loop
                root.append(loop)

            # The reference associates loop -> bubble and bubble -> loop.
            loop.insert(
                0,
                etree.Element(
                    "Association",
                    Type="is a collection including",
                    ItemID=instrument.get("ID"),
                    TagName=tag,
                ),
            )
            instrument.insert(
                0,
                etree.Element(
                    "Association",
                    Type="is a part of",
                    ItemID=loop.get("ID"),
                    TagName=loop_tag,
                ),
            )

        catalogue.append(definition)
        root.append(instrument)

    return len(instrument_rows)

def export(dxf_path, review_path, mapping_path, xsd_path, output_path):
    dxf_path = Path(dxf_path)
    review_path = Path(review_path)
    mapping_path = Path(mapping_path)
    xsd_path = Path(xsd_path)
    output_path = Path(output_path)

    for path in (dxf_path, review_path, mapping_path, xsd_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output_path.exists():
        raise FileExistsError(
            f"Output already exists; it will not be overwritten: {output_path}"
        )

    allowed = approved_pairs(mapping_path)
    blocks = {}
    handles = {}
    instrument_rows = []
    with review_path.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            if row["review_status"].strip().upper() != "APPROVED":
                continue
            item_kind = row.get("item_kind", "Equipment").strip()
            if item_kind == "Instrument":
                instrument_rows.append(row)
                continue

            if item_kind != "Equipment":
                raise ValueError(
                    f"Unsupported approved item_kind {item_kind!r} "
                    f"for handle {row.get('source_handle', '')}"
                )

            handle = row["source_handle"].strip().upper()
            block = row["block_name"].strip()
            tag = row["confirmed_tag"].strip()
            class_name = row["confirmed_class"].strip()
            uri = row["confirmed_uri"].strip()

            if not all((handle, block, tag, class_name, uri)):
                raise ValueError(
                    f"Approved row {handle or '(no handle)'} is incomplete"
                )
            if (
                "".join(filter(str.isalnum, class_name)).casefold(),
                uri,
            ) not in {
                ("".join(filter(str.isalnum, name)).casefold(), approved_uri)
                for name, approved_uri in allowed
            }:
                raise ValueError(
                    f"{handle}: class/URI pair is not in mapping workbook"
                )
            decision = ("Equipment", class_name, uri)
            if block in blocks and blocks[block] != decision:
                raise ValueError(
                    f"Conflicting approved classes for block {block}"
                )
            if handle in handles:
                raise ValueError(f"Duplicate approved handle: {handle}")
            blocks[block] = decision
            handles[handle] = tag

        fragment_path = (
        review_path.parent / "fragmented_equipment_REVIEW.csv"
        )
        fragments = []
        if fragment_path.exists():
            with fragment_path.open(
                newline="", encoding="utf-8-sig"
            ) as file:
                fragments = [
                    row for row in csv.DictReader(file)
                    if row["review_status"].strip().upper() == "APPROVED"
                ]

    if not handles and not fragments:
        raise ValueError("No approved equipment or geometry groups")


    
    # v2 writes beside its input and refuses to overwrite files.
    # A temporary DXF copy makes each run independent and preserves
    # the original DXF and previous outputs.
    with tempfile.TemporaryDirectory(prefix="pid_export_") as directory:
        work = Path(directory)
        temporary_dxf = work / dxf_path.name
        shutil.copy2(dxf_path, temporary_dxf)

        empty_groups = work / "empty_groups.csv"
        empty_groups.write_text(
            "confirmed_component_class,verified_class_uri,"
            "candidate_geometry_handles,center_x,center_y\n",
            encoding="utf-8",
        )

        generate_base_xml(
            temporary_dxf,
            blocks,
            handles,
            work / (
                temporary_dxf.stem + "_IMPORT_TEST_v2.xml"
            ),
        )


        generated = work / (
            temporary_dxf.stem + "_IMPORT_TEST_v2.xml"
        )
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.parse(str(generated), parser)
        root = tree.getroot()

        # These are required by the supplied Proteus 4.0.1 XSD.
        information = root.find("PlantInformation")
        if information is None:
            raise ValueError("Generator omitted PlantInformation")
        now = datetime.now()
        information.set("Date", now.strftime("%Y-%m-%d"))
        information.set("Time", now.strftime("%H:%M:%S"))

        # v2 classifies *all* inserts of an approved block name.
        # Remove instances whose individual handles were not approved,
        # including any unapproved nozzles.
        for element in list(root):
            if element.tag not in ("Equipment", "Nozzle"):
                continue
            component_name = element.get("ComponentName", "")
            handle = component_name.removeprefix("DXF_BLOCK_").upper()
            if handle not in handles or element.tag != "Equipment":
                root.remove(element)

        # Also remove catalogue definitions not used by retained instances.
        used_names = {
            element.get("ComponentName")
            for element in root.findall("Equipment")
        }
        catalogue = root.find("ShapeCatalogue")
        if catalogue is not None:
            for element in list(catalogue):
                if (element.get("ComponentName", "").startswith("DXF_BLOCK_")
                        and element.get("ComponentName") not in used_names):
                    catalogue.remove(element)

        found = {
            element.get("TagName")
            for element in root.findall("Equipment")
        }
        if not set(handles.values()).issubset(found):
            raise ValueError(
                "One or more approved tags did not produce an "
                "equipment instance with drawable DXF geometry"
            )
        # Convert reviewed loose DXF geometry into equipment. This uses
        # selected source handles, not drawing-specific Python constants.
        source_doc = ezdxf.readfile(str(temporary_dxf))
        by_handle = {
            entity.dxf.handle.upper(): entity
            for entity in source_doc.modelspace()
        }
        used_fragment_handles = set()

        for group_number, group in enumerate(fragments, start=1):
            tag = group["confirmed_tag"].strip()
            class_name = group["confirmed_class"].strip()
            uri = group["confirmed_uri"].strip()
            raw_handles = [
                value.strip().upper()
                for value in group["geometry_handles"].split("|")
                if value.strip()
            ]
            normalized = (
                "".join(filter(str.isalnum, class_name)).casefold(),
                uri,
            )
            allowed_normalized = {
                ("".join(filter(str.isalnum, name)).casefold(), item_uri)
                for name, item_uri in allowed
            }
            if normalized not in allowed_normalized:
                raise ValueError(
                    f"Fragmented group {tag}: unverified class/URI"
                )
            match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", tag)
            if not match:
                raise ValueError(
                    f"Fragmented group needs reviewed tag parsing: {tag}"
                )
            if not raw_handles or len(raw_handles) != len(set(raw_handles)):
                raise ValueError(
                    f"Fragmented group {tag}: missing/duplicate handles"
                )

            paths = []
            for handle in raw_handles:
                if handle in used_fragment_handles:
                    raise ValueError(
                        f"Geometry handle reused by groups: {handle}"
                    )
                entity = by_handle.get(handle)
                if entity is None:
                    raise ValueError(
                        f"{tag}: DXF handle not found: {handle}"
                    )
                parts = flatten_entity(entity)
                if not parts:
                    raise ValueError(
                        f"{tag}: handle has no supported geometry: "
                        f"{handle}"
                    )
                paths.extend(parts)
                used_fragment_handles.add(handle)

            points = [point for part in paths for point in part]
            origin = (
                (min(x for x, y in points)
                 + max(x for x, y in points)) / 2,
                (min(y for x, y in points)
                 + max(y for x, y in points)) / 2,
            )
            component_name = f"DXF_GROUP_{group_number}"
            # v2.add_symbol builds with standard-library ElementTree.
            # Build separately, then append its XML to the lxml tree.
            temporary_root = ET.Element("Temporary")
            temporary_catalogue = ET.Element("TemporaryCatalogue")
            if not add_symbol(
                temporary_catalogue, temporary_root,
                tag, class_name, uri, component_name,
                paths, origin, "Equipment",
            ):
                raise ValueError(f"No drawable geometry for {tag}")

            definition = etree.fromstring(
                ET.tostring(temporary_catalogue[0])
            )
            equipment = etree.fromstring(
                ET.tostring(temporary_root[0])
            )
            catalogue.append(definition)

            prefix, number = match.groups()
            equipment.set("StockNumber", component_name)
            provisional = equipment.find("GenericAttributes")
            if provisional is not None:
                equipment.remove(provisional)

            attrs = etree.SubElement(
                equipment, "GenericAttributes",
                Set=class_name, Number="5",
            )
            for name, value in (
                ("Number", number),
                ("TagType", prefix),
                ("Type", prefix),
                ("TagSequenceNo", number),
                ("SymbolName", component_name),
            ):
                etree.SubElement(
                    attrs, "GenericAttribute",
                    Name=name, Format="string", Value=value,
                )
            dexpi = etree.SubElement(
                equipment, "GenericAttributes",
                Set="DexpiAttributes", Number="2",
            )
            for name, value in (
                ("TagNameSequenceNumberAssignmentClass", number),
                ("TagNamePrefixAssignmentClass", prefix),
            ):
                etree.SubElement(
                    dexpi, "GenericAttribute",
                    Name=name, Format="string", Value=value,
                    AttributeURI="http://sandbox.dexpi.org/rdl/" + name,
                )
            etree.SubElement(
                equipment, "PersistentID",
                Identifier=equipment.get("ID"),
                Context=dxf_path.stem,
            )

            # If the source DXF has an exact text label, associate it.
            drawing = root.find("Drawing")
            label = next(
                (
                    text for text in drawing.findall("Text")
                    if text.get("String") == tag
                ),
                None,
            )
            if label is not None:
                drawing.remove(label)
                equipment.append(label)

            root.append(equipment)

        expected = set(handles.values()) | {
            group["confirmed_tag"].strip() for group in fragments
        }
        produced = {
            item.get("TagName") for item in root.findall("Equipment")
        }
        if not expected.issubset(produced):
            raise ValueError("Approved equipment missing from generated XML")
        # Test: avoid supplying the same equipment prefix in both
        # TagType and Type. Preserve TagName and the DEXPI attributes.
        for equipment in root.findall("Equipment"):
            for group in equipment.findall("GenericAttributes"):
                tag_type = next(
                    (
                        item for item in group.findall("GenericAttribute")
                        if item.get("Name") == "TagType"
                    ),
                    None,
                )
                if tag_type is None:
                    continue
                for item in list(group.findall("GenericAttribute")):
                    if (
                        item.get("Name") == "Type"
                        and item.get("Value") == tag_type.get("Value")
                    ):
                        group.remove(item)
                        group.set(
                            "Number",
                            str(len(group.findall("GenericAttribute"))),
                        )
        approved_nozzles = add_approved_nozzles(
            root,
            source_doc,
            review_path.parent / "nozzles_REVIEW.csv",
            mapping_path,
            dxf_path,
        )
        approved_instruments = add_approved_instruments(
            root,
            source_doc,
            instrument_rows,
            mapping_path,
            dxf_path,
        )

        from pipe_xml import add_approved_pipes

        approved_pipe_rows = add_approved_pipes(
            root,
            temporary_dxf,
            review_path.parent / "pipes_REVIEW.csv",
            component_review_path=(
                review_path.parent
                / "pipe_components_REVIEW.csv"
            ),
            mapping_path=mapping_path,
            add_symbol=add_symbol,
            flatten_entity=flatten_entity,
        )


        schema = etree.XMLSchema(etree.parse(str(xsd_path)))
        if not schema.validate(tree):
            errors = "\n".join(
                str(error) for error in list(schema.error_log)[:12]
            )
            raise ValueError(
                "XML was generated but FAILED Proteus XSD validation. "
                "It was NOT released as output.\n" + errors
            )

        etree.indent(tree, space="  ")
        tree.write(
            str(output_path),
            encoding="utf-8",
            xml_declaration=True,
            pretty_print=True,
        )

    return output_path, len(handles) + len(fragments) + approved_instruments


if __name__ == "__main__":
    if len(sys.argv) != 6:
        raise SystemExit(
            'Usage: python reviewed_equipment_export.py '
            '"drawing.dxf" "placed_symbols_REVIEW.csv" '
            '"SAMTEST_SYM_MAPPING_URI.xlsx" '
            '"ProteusSchema_4.0.1.xsd" "equipment_demo.xml"'
        )
    try:
        path, count = export(*sys.argv[1:])
        print(f"XSD-VALID equipment-only XML: {path}")
        print(f"Approved equipment decisions: {count}")
        print("AVEVA import behaviour still requires testing.")
    except Exception as exc:
        raise SystemExit(f"EXPORT STOPPED: {exc}")
