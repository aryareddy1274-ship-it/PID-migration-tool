import re
from lxml import etree
from openpyxl import load_workbook

_TEMPLATE = re.compile(r"\{(\w+)\}")


def load_definitions(workbook_path):
    book = load_workbook(str(workbook_path), read_only=True, data_only=True)
    try:
        if "AttributeDefinitions" not in book.sheetnames:
            raise ValueError(
                "Attribute workbook has no AttributeDefinitions sheet: "
                + str(workbook_path)
            )

        sheet = book["AttributeDefinitions"]
        rows = sheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            raise ValueError("AttributeDefinitions sheet is empty.")

        header = [str(h).strip() if h is not None else "" for h in header_row]
        missing = {"class_name", "attribute_set", "attribute_name", "format"} - set(header)
        if missing:
            raise ValueError(
                "AttributeDefinitions missing columns: " + ", ".join(sorted(missing))
            )

        definitions = {}
        seen = set()
        skipped = []

        for row_index, raw in enumerate(rows, start=2):
            record = {
                col: (str(v).strip() if v is not None else "")
                for col, v in zip(header, raw)
            }
            class_name     = record.get("class_name",     "")
            attribute_set  = record.get("attribute_set",  "")
            attribute_name = record.get("attribute_name", "")

            if not class_name or not attribute_set or not attribute_name:
                continue

            triple = (class_name, attribute_set, attribute_name)
            if triple in seen:
                skipped.append(
                    f"Row {row_index}: {class_name} / {attribute_set} / {attribute_name}"
                )
                continue

            seen.add(triple)
            definitions.setdefault((class_name, attribute_set), []).append(record)

        return definitions, skipped
    finally:
        book.close()


def _fill(template, values):
    if not template:
        return None
    result = template
    for match in _TEMPLATE.finditer(template):
        key = match.group(1)
        replacement = values.get(key, "")
        if not replacement:
            return None
        result = result.replace("{" + key + "}", str(replacement))
    return result or None


def _resolve(row, values):
    source_field  = row.get("source_field",  "")
    default_value = row.get("default_value", "")

    if source_field:
        raw = values.get(source_field, "")
        if raw:
            value = _fill(str(raw), values) if "{" in str(raw) else str(raw)
        else:
            value = _fill(default_value, values) if default_value else None
    else:
        value = _fill(default_value, values) if default_value else None

    return value or None


def write_attributes(parent, class_name, attribute_set, values, definitions):
    for set_name in (attribute_set, "DexpiAttributes"):
        rows = definitions.get((class_name, set_name), [])
        resolved = [(row, _resolve(row, values) or "") for row in rows]

        if not resolved:
            continue

        group = etree.SubElement(
            parent, "GenericAttributes",
            Set=set_name,
            Number=str(len(resolved)),
        )

        for row, value in resolved:
            attribs = {
                "Name":   row["attribute_name"],
                "Format": row.get("format", "string") or "string",
                "Value":  value,
            }
            if row.get("units"):
                attribs["Units"] = row["units"]
            if row.get("attribute_uri"):
                attribs["AttributeURI"] = row["attribute_uri"]
            if row.get("value_uri"):
                filled = _fill(row["value_uri"], values)
                if filled:
                    attribs["ValueURI"] = filled

            etree.SubElement(group, "GenericAttribute", **attribs)
