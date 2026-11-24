import shutil
from contextlib import closing
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from se import cli
from se.probe import Observation

FIXTURE = Path(__file__).parent / "fixtures" / "scene.xlsx"
EXAMPLE = FIXTURE.parents[2] / "config" / "networks.example.yaml"


class LocalProbe:
    def __init__(self):
        self.visited = []

    def observe(self, network, address):
        self.visited.append((network.name, address))
        if address == IPv4Address("10.70.0.20"):
            return Observation("used", "fixture reply")
        if address == IPv4Address("10.70.0.21"):
            return Observation("error", "fixture timeout")
        return Observation("no_reply", "fixture request without a reply")

    def close(self):
        pass


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    shutil.copyfile(EXAMPLE, config / "networks.yaml")
    source = tmp_path / "source.xlsx"
    shutil.copyfile(FIXTURE, source)
    output = tmp_path / "source_filled.xlsx"
    probe = LocalProbe()
    monkeypatch.setattr(cli, "SshProbe", lambda *_: probe)
    return config, source, output, probe


def apply_args(workspace):
    config, source, *_ = workspace
    return ["apply", str(source), "--config-dir", str(config)]


def test_inspect_shows_slots_without_network_configuration(workspace, capsys):
    _, source, output, probe = workspace
    assert cli.run(["inspect", str(source)]) == 0
    assert "193 slots, 186 unique IP requirements" in capsys.readouterr().out
    assert not probe.visited
    assert not output.exists()


def test_apply_reviews_full_plan_before_writing(workspace, monkeypatch, capsys):
    config, source, output, probe = workspace
    network_config = config / "networks.yaml"
    network_config.write_text(
        network_config.read_text().replace("gateway: 10.70.0.1", "gateway: 10.70.0.22")
    )
    original = source.read_bytes()

    def approve(_prompt):
        assert not output.exists()
        preview = capsys.readouterr()
        assert "Plan for 193 slots" in preview.out
        assert "tenant!Q2" in preview.out
        assert "fixture timeout" in preview.err
        return "y"

    monkeypatch.setattr("builtins.input", approve)
    assert cli.run(apply_args(workspace)) == 0
    assert source.read_bytes() == original
    assert "Wrote " in capsys.readouterr().out
    assert IPv4Address("10.70.0.22") not in {ip for _, ip in probe.visited}
    with (
        closing(load_workbook(source)) as before,
        closing(load_workbook(output)) as after,
    ):
        outside_scene = before["base_info"]["C33"].value
        assert ("external", IPv4Address(outside_scene)) in probe.visited
        changed = set()
        for sheet in before.sheetnames:
            for row in after[sheet].iter_rows():
                for cell in row:
                    prior = before[sheet][cell.coordinate]
                    assert prior.style_id == cell.style_id
                    if prior.value != cell.value:
                        changed.add((sheet, cell.coordinate))
        assert len(changed) == 189
        assert {sheet for sheet, _ in changed} == {"om", "tenant"}
        assert after["tenant"]["N7"].value == after["tenant"]["N8"].value
        assert after["tenant"]["N21"].value.startswith('{"sc-postgresql": ["10.')
        assert after["om"]["K4"].value == "N/A"
        assert after["om"]["Q2"].value == "untouched"
    assert sorted(path.name for path in output.parent.iterdir()) == [
        "config",
        "source.xlsx",
        "source_filled.xlsx",
    ]


@pytest.mark.parametrize(
    ("sheet_name", "headers", "vip", "filled_vip", "filled_ip"),
    [
        (
            "om",
            ("Service-VIP", "Service-IP", "External-IP"),
            None,
            "10.70.3.20",
            "10.70.3.21",
        ),
        (
            "tenant",
            ("OM-VIP", "OM-IP", "External-IP", "External-VIP"),
            '{"db": [""]}',
            '{"db": ["10.70.0.22"]}',
            "10.70.0.23",
        ),
    ],
)
def test_single_sheet_with_optional_network_columns(
    workspace, monkeypatch, capsys, sheet_name, headers, vip, filled_vip, filled_ip
):
    _, source, output, _ = workspace
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name
    sheet.append(["Other-IP", *headers])
    sheet.append(["untouched", vip, None, "N/A"])
    sheet["H1"] = "Node"
    sheet["H2"] = "node-1"
    sheet["A3"] = "This row has no node"
    sheet["B3"] = "invalid but ignored"
    sheet["AA1"] = "APP-OM-IP"
    book.save(source)

    assert cli.run(["inspect", str(source)]) == 0
    assert "3 slots" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.run(apply_args(workspace)) == 0
    with closing(load_workbook(output)) as filled:
        assert filled.sheetnames == [sheet_name]
        sheet = filled[sheet_name]
        assert sheet["A2"].value == "untouched"
        assert sheet["B2"].value == filled_vip
        assert sheet["C2"].value == filled_ip
        assert sheet["B3"].value == "invalid but ignored"
        assert sheet["D2"].value == "N/A"
        assert sheet["E2"].value is None
        assert sheet["AA2"].value == "10.70.1.20"


@pytest.mark.parametrize(
    ("sheet_name", "edit", "expected_error"),
    [
        ("other", {}, "Expected at least one worksheet named om or tenant"),
        ("om", {"A1": None}, "om!1: missing header Node"),
        ("om", {"AA1": "OM-IP"}, "om!J1, AA1: duplicate header OM-IP"),
        (
            "om",
            {"AA1": "Service-IP", "AB1": "Service-IP"},
            "om!AA1, AB1: duplicate header Service-IP",
        ),
        *(
            ("om", {"J2": marker}, "om!J2: expected an IPv4 address")
            for marker in ("NA", "NONE", "NULL", "-", " ")
        ),
        *(
            ("tenant", {"N2": marker}, "tenant!N2: invalid VIP JSON")
            for marker in ("NA", "NONE", "NULL", "-", " ")
        ),
    ],
)
def test_inspect_rejects_invalid_scene_format(
    workspace, capsys, sheet_name, edit, expected_error
):
    _, source, _, _ = workspace
    with closing(load_workbook(source)) as book:
        if sheet_name == "other":
            del book["om"]
            del book["tenant"]
        else:
            for cell, value in edit.items():
                book[sheet_name][cell] = value
        book.save(source)

    assert cli.run(["inspect", str(source)]) == 1
    assert expected_error in capsys.readouterr().err


@pytest.mark.parametrize("answer", ["", "n", None])
def test_decline_never_creates_output(workspace, monkeypatch, answer):
    _, _, output, _ = workspace

    def respond(_prompt):
        if answer is None:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", respond)
    assert cli.run(apply_args(workspace)) == 0
    assert not output.exists()


def test_shortage_stops_before_confirmation(workspace, monkeypatch, capsys):
    config, _, output, _ = workspace
    path = config / "networks.yaml"
    path.write_text(
        path.read_text().replace("usable_end: 10.70.0.240", "usable_end: 10.70.0.22")
    )
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("partial plan must not be approved")
    )
    assert cli.run(apply_args(workspace)) == 1
    assert "om: 4 more addresses needed" in capsys.readouterr().err
    assert not output.exists()


def test_output_created_during_review_is_preserved(workspace, monkeypatch):
    _, _, output, _ = workspace

    def approve(_prompt):
        output.write_bytes(b"existing file")
        return "y"

    monkeypatch.setattr("builtins.input", approve)
    assert cli.run(apply_args(workspace)) == 1
    assert output.read_bytes() == b"existing file"
