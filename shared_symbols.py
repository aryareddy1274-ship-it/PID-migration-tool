import hashlib
import json
import math

from dataclasses import dataclass
from ezdxf import path as dxf_path


@dataclass(frozen=True)
class Placement:
    handle: str
    name: str
    element: str
    class_name: str
    uri: str
    x: float
    y: float
    angle: float
    sx: float
    sy: float


def quarter_turn(point, turns):
    x, y = point
    turns %= 4

    if turns == 0:
        return x, y
    if turns == 1:
        return -y, x
    if turns == 2:
        return -x, -y
    return y, -x


def turn_paths(paths, turns):
    return [
        [quarter_turn(point, turns) for point in line]
        for line in paths
    ]


def strokes(paths):
    """Split paths into line segments; ignore drawing order."""
    result = []

    for line in paths:
        for a, b in zip(line, line[1:]):
            a = tuple(0.0 if abs(v) < 1e-12 else v for v in a)
            b = tuple(0.0 if abs(v) < 1e-12 else v for v in b)

            if a != b:
                result.append((a, b))

    return result


def signature(paths, tolerance):
    def rounded(point):
        return tuple(round(value / tolerance) for value in point)

    result = []

    for a, b in strokes(paths):
        result.append(tuple(sorted((rounded(a), rounded(b)))))

    return tuple(sorted(result))


def same_geometry(left, right, tolerance):
    """Compare actual segment endpoints, not only rounded signatures."""
    remaining = strokes(right)

    for a, b in strokes(left):
        found = None

        for index, (c, d) in enumerate(remaining):
            forward = (
                math.dist(a, c) <= tolerance
                and math.dist(b, d) <= tolerance
            )
            backward = (
                math.dist(a, d) <= tolerance
                and math.dist(b, c) <= tolerance
            )

            if forward or backward:
                found = index
                break

        if found is None:
            return False

        remaining.pop(found)

    return not remaining


def flatten(entity, tolerance=0.08):
    kind = entity.dxftype()

    if kind == "LINE":
        return [[
            (entity.dxf.start.x, entity.dxf.start.y),
            (entity.dxf.end.x, entity.dxf.end.y),
        ]]

    if kind not in {
        "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE"
    }:
        return []

    path = dxf_path.make_path(entity)
    points = [
        (vertex.x, vertex.y)
        for vertex in path.flattening(tolerance)
    ]

    if kind == "CIRCLE" and points and points[0] != points[-1]:
        points.append(points[0])

    return [points] if len(points) >= 2 else []


def block_paths(insert, doc):
    """Read the block definition, before this INSERT's transform."""
    if insert.dxf.name not in doc.blocks:
        raise ValueError(
            f"{insert.dxf.handle}: missing block {insert.dxf.name}"
        )

    if (
        insert.dxf.get("row_count", 1) != 1
        or insert.dxf.get("column_count", 1) != 1
    ):
        raise ValueError(
            f"{insert.dxf.handle}: INSERT arrays are not supported"
        )

    block = doc.blocks[insert.dxf.name]
    base = block.block.dxf.base_point
    result = []

    for entity in block:
        if entity.dxftype() == "INSERT":
            for child in entity.virtual_entities():
                result.extend(flatten(child))
        else:
            result.extend(flatten(entity))

    result = [
        [(x - base.x, y - base.y) for x, y in line]
        for line in result
        if len(line) >= 2
    ]

    if not result:
        raise ValueError(
            f"{insert.dxf.handle}: block has no supported geometry"
        )

    return result


def placed_paths(paths, x, y, angle, sx, sy):
    angle = math.radians(angle)
    c, s = math.cos(angle), math.sin(angle)

    return [
        [
            (x + px * sx * c - py * sy * s,
             y + px * sx * s + py * sy * c)
            for px, py in line
        ]
        for line in paths
    ]


class SymbolRegistry:
    def __init__(self, tolerance=0.05):
        if tolerance <= 0:
            raise ValueError("Tolerance must be positive")

        self.tolerance = tolerance
        self.definitions = {}
        self.placements = []

    def add(self, insert, doc, element, class_name, uri):
        if not class_name or not uri:
            raise ValueError(
                f"{insert.dxf.handle}: approved class and URI required"
            )

        source = block_paths(insert, doc)
        options = []

        for turns in range(4):
            rotated = turn_paths(source, turns)
            options.append((
                signature(rotated, self.tolerance),
                turns,
                rotated,
            ))

        shape_key, turns, candidate = min(
            options, key=lambda option: (option[0], option[1])
        )

        identity = (element, class_name, uri, shape_key)
        digest = hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        name = "DXF_SHAPE_" + digest

        master = self.definitions.get(name)

        if master is None:
            self.definitions[name] = {
                "element": element,
                "class_name": class_name,
                "uri": uri,
                "paths": candidate,
            }
            master = self.definitions[name]
        elif not same_geometry(
            master["paths"], candidate, self.tolerance
        ):
            raise ValueError(
                f"{insert.dxf.handle}: similar fingerprint, "
                "but geometry does not match"
            )

        x = float(insert.dxf.insert.x)
        y = float(insert.dxf.insert.y)
        source_angle = float(insert.dxf.get("rotation", 0))
        source_sx = float(insert.dxf.get("xscale", 1))
        source_sy = float(insert.dxf.get("yscale", 1))

        if source_sx <= 0 or source_sy <= 0:
            raise ValueError(
                f"{insert.dxf.handle}: mirrored or zero scale "
                "needs separate handling"
            )

        # A quarter-turn swaps the local X and Y axes.
        if turns % 2:
            sx, sy = source_sy, source_sx
        else:
            sx, sy = source_sx, source_sy

        angle = (source_angle - 90 * turns) % 360

        expected = placed_paths(
            source, x, y, source_angle, source_sx, source_sy
        )
        actual = placed_paths(
            master["paths"], x, y, angle, sx, sy
        )

        world_tolerance = self.tolerance * max(
            source_sx, source_sy
        )
        if not same_geometry(
            expected, actual, world_tolerance
        ):
            raise ValueError(
                f"{insert.dxf.handle}: shared symbol does not "
                "reconstruct the source placement"
            )

        placement = Placement(
            handle=insert.dxf.handle.upper(),
            name=name,
            element=element,
            class_name=class_name,
            uri=uri,
            x=x,
            y=y,
            angle=angle,
            sx=sx,
            sy=sy,
        )
        self.placements.append(placement)
        return placement
def _distance_to_segment(point, a, b):
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy

    if length_squared == 0:
        return math.dist(point, a)

    fraction = (
        ((px - ax) * dx + (py - ay) * dy) / length_squared
    )
    fraction = max(0.0, min(1.0, fraction))
    nearest = (ax + fraction * dx, ay + fraction * dy)
    return math.dist(point, nearest)


def _points_follow_lines(points, lines, tolerance):
    line_segments = strokes(lines)
    if not line_segments:
        return False

    for line in points:
        for point in line:
            if min(
                _distance_to_segment(point, a, b)
                for a, b in line_segments
            ) > tolerance:
                return False

    return True


def check_placed_insert(
    insert, placement, master_paths, tolerance=0.2
):
    """Compare the XML placement with ezdxf's placed INSERT geometry."""
    expected = []

    for entity in insert.virtual_entities():
        expected.extend(flatten(entity))

    if not expected:
        raise ValueError(
            f"{insert.dxf.handle}: placed INSERT has no "
            "supported drawable geometry"
        )

    actual = placed_paths(
        master_paths,
        placement.x,
        placement.y,
        placement.angle,
        placement.sx,
        placement.sy,
    )

    # Compare in both directions: neither drawing may have extra
    # strokes outside the other.
    if not (
        _points_follow_lines(expected, actual, tolerance)
        and _points_follow_lines(actual, expected, tolerance)
    ):
        raise ValueError(
            f"{insert.dxf.handle}: generated master/placement "
            "does not match the placed DXF INSERT"
        )
