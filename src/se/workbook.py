import posixpath
import re
from collections import defaultdict
from io import BytesIO
from xml.etree import ElementTree
from xml.parsers import expat
from xml.sax.saxutils import escape
from zipfile import ZipFile

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from .scene import SceneError

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TAG = re.compile(rb"""<(?P<name>[\w.:-]+)(?:[^"'<>]|"[^"]*"|'[^']*')*>""")
ATTRIBUTE = re.compile(rb"""\s+([\w.:-]+)\s*=\s*(?:"[^"]*"|'[^']*')""")
ENCODING = re.compile(rb"""encoding\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def worksheet_paths(source: ZipFile) -> dict[str, str]:
    workbook = ElementTree.fromstring(source.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(source.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in relationships
        if rel.attrib["Type"].endswith("/worksheet")
        and rel.get("TargetMode") != "External"
    }
    return {
        sheet.attrib["name"]: posixpath.normpath(
            target[1:] if target.startswith("/") else f"xl/{target}"
        )
        for sheet in workbook.findall(f".//{{{SHEET_NS}}}sheet")
        if (target := targets.get(sheet.get(f"{{{REL_NS}}}id"))) is not None
    }


def patch_sheet(data: bytes, changes: dict[str, str]) -> bytes:
    declaration = data[:256].split(b"?>", 1)[0].removeprefix(b"\xef\xbb\xbf")
    if declaration.startswith(b"<?xml"):
        encoding = ENCODING.search(declaration)
        if encoding and encoding[1].lower() not in (b"utf-8", b"utf8"):
            raise SceneError("Worksheet XML must use UTF-8")
    by_row = defaultdict(dict)
    for cell, value in changes.items():
        row, column = coordinate_to_tuple(cell)
        by_row[row][column] = (cell, value)

    parser = expat.ParserCreate(namespace_separator="|")
    path = []
    patches = []
    next_row = 1
    last_col = 0
    remaining = {}
    current = None
    row_tag = None
    row_start = 0
    prefix = b""
    visited = set()

    def tag_at(index):
        tag = TAG.match(data, index)
        if tag is None:
            raise SceneError("Unsupported worksheet XML encoding or tag")
        return tag

    def inline(value):
        xml = escape(value, {"\r": "&#13;"}).encode()
        return b'<%sis><%st xml:space="preserve">%s</%st></%sis>' % (
            prefix,
            prefix,
            xml,
            prefix,
            prefix,
        )

    def new_cell(cell, value):
        return b'<%sc r="%s" t="inlineStr">%s</%sc>' % (
            prefix,
            cell.encode(),
            inline(value),
            prefix,
        )

    def insert_before(index, columns):
        payload = b"".join(new_cell(*remaining.pop(column)) for column in columns)
        if payload:
            patches.append((index, index, payload))

    def start(name, attrs):
        nonlocal next_row, last_col, remaining, current, row_tag, row_start, prefix
        path.append(name.removeprefix(SHEET_NS + "|"))
        if path == ["worksheet", "sheetData", "row"]:
            row = int(attrs.get("r", next_row))
            next_row = row + 1
            last_col = 0
            remaining = dict(by_row.get(row, ()))
            if remaining:
                if row in visited:
                    raise SceneError("Duplicate worksheet row")
                visited.add(row)
                row_start = parser.CurrentByteIndex
                row_tag = tag_at(row_start)
                prefix = row_tag["name"][:-3]
        elif path == ["worksheet", "sheetData", "row", "c"] and remaining:
            last_col = (
                coordinate_to_tuple(attrs["r"])[1] if "r" in attrs else last_col + 1
            )
            index = parser.CurrentByteIndex
            insert_before(
                index, sorted(column for column in remaining if column < last_col)
            )
            if last_col in remaining:
                current = (tag_at(index), remaining.pop(last_col)[1])
        elif path == ["worksheet", "sheetData", "row", "extLst"] and remaining:
            insert_before(parser.CurrentByteIndex, sorted(remaining))
        elif path == ["worksheet", "mergeCells", "mergeCell"]:
            left, top, right, bottom = range_boundaries(attrs["ref"])
            if any(
                top <= row <= bottom and any(left <= col <= right for col in cells)
                for row, cells in by_row.items()
            ):
                raise SceneError(
                    f"Cannot write addresses to merged range {attrs['ref']}"
                )

    def end(_name):
        nonlocal current
        if path == ["worksheet", "sheetData", "row", "c"] and current is not None:
            tag, value = current
            head = tag.group()
            stop = (
                tag.end()
                if head.endswith(b"/>")
                else data.index(b">", parser.CurrentByteIndex) + 1
            )
            attributes_at = tag.end("name") - tag.start()
            attrs = b"".join(
                token.group()
                for token in ATTRIBUTE.finditer(head, attributes_at)
                if token[1] != b"t"
            )
            payload = b'<%s%s t="inlineStr">%s</%s>' % (
                tag["name"],
                attrs,
                inline(value),
                tag["name"],
            )
            patches.append((tag.start(), stop, payload))
            current = None
        elif (
            path == ["worksheet", "sheetData", "row"]
            and remaining
            and row_tag is not None
        ):
            index = parser.CurrentByteIndex
            if row_tag.group().endswith(b"/>"):
                payload = b"".join(
                    new_cell(*remaining.pop(column)) for column in sorted(remaining)
                )
                patches.append(
                    (
                        row_start,
                        row_tag.end(),
                        row_tag.group()[:-2]
                        + b">"
                        + payload
                        + b"</"
                        + row_tag["name"]
                        + b">",
                    )
                )
            else:
                insert_before(index, sorted(remaining))
        path.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(data, True)
    if visited != by_row.keys():
        raise SceneError("Target row is missing from worksheet XML")
    tokens = []
    cursor = 0
    for start_at, end_at, payload in sorted(
        patches, key=lambda item: (item[0], item[1])
    ):
        tokens.extend((data[cursor:start_at], payload))
        cursor = end_at
    return b"".join([*tokens, data[cursor:]])


def write_workbook(
    contents: bytes, changes: dict[str, dict[str, str]], destination
) -> None:
    if not changes:
        destination.write(contents)
        return
    with ZipFile(BytesIO(contents)) as source, ZipFile(destination, "w") as output:
        if any(name.startswith("_xmlsignatures/") for name in source.namelist()):
            raise SceneError("A digitally signed workbook cannot be filled")
        paths = worksheet_paths(source)
        if not changes.keys() <= paths.keys():
            raise SceneError("Cannot resolve target worksheet XML")
        by_path = {paths[sheet]: cells for sheet, cells in changes.items()}
        output.comment = source.comment
        for member in source.infolist():
            data = source.read(member)
            if member.filename in by_path:
                data = patch_sheet(data, by_path[member.filename])
            output.writestr(member, data)
