from xml.etree import ElementTree

import pytest

from se.scene import SceneError
from se.workbook import SHEET_NS, patch_sheet


def sheet_xml(content: str) -> bytes:
    return (f'<m:worksheet xmlns:m="{SHEET_NS}">{content}</m:worksheet>').encode()


def test_sheet_patch_only_splices_target_cells():
    original = sheet_xml(
        '<m:sheetData><m:row r="2">'
        '<m:c r="A2" t="s"><m:v>1</m:v></m:c>'
        '<m:c r="C2" s="1" />'
        '<m:c r="E2"><m:f>2+2</m:f><m:v>4</m:v></m:c>'
        "<m:extLst/>"
        "</m:row></m:sheetData>"
    )
    output = patch_sheet(original, {"B2": "new", "C2": 'json "a & b"\r\n', "F2": "end"})
    assert b'<m:c r="E2"><m:f>2+2</m:f><m:v>4</m:v></m:c>' in output
    assert b"</m:c><m:extLst/>" in output
    cells = ElementTree.fromstring(output).findall(f".//{{{SHEET_NS}}}c")
    assert [cell.attrib["r"] for cell in cells] == ["A2", "B2", "C2", "E2", "F2"]
    assert cells[2].attrib["s"] == "1"
    assert cells[2].find(f".//{{{SHEET_NS}}}t").text == 'json "a & b"\r\n'


def test_sheet_patch_refuses_merged_target():
    original = sheet_xml(
        '<m:sheetData><m:row r="2"><m:c r="A2"/></m:row></m:sheetData>'
        '<m:mergeCells><m:mergeCell ref="A2:B2"/></m:mergeCells>'
    )
    with pytest.raises(SceneError, match="merged range A2:B2"):
        patch_sheet(original, {"B2": "10.1.1.1"})
