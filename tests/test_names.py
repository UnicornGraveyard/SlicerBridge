"""File names: the printer named in full, with whatever a shop puts before or after it."""
from pathlib import Path

from slicerbridge.cli import _default_name, main
from slicerbridge.printers import out_name, printer_of, split_name


def test_a_suffix_goes_after_the_printer():
    assert out_name("Dragon", "xl", suffix="My Shop") == "Dragon - Prusa XL - My Shop.3mf"
    assert out_name("Dragon", "xl", suffix=" - My Shop") == "Dragon - Prusa XL - My Shop.3mf"
    assert out_name("Dragon", "xl", plate=" - Plate 2", suffix="My Shop") == "Dragon - Prusa XL - Plate 2 - My Shop.3mf"
    assert out_name("Dragon", "xl") == "Dragon - Prusa XL.3mf"


def test_the_printer_is_read_back_with_or_without_a_suffix():
    assert split_name("Dragon - Creality K2 Plus.3mf") == ("Dragon", "k2plus")
    assert split_name("Dragon - Creality K2 Plus - My Shop.3mf") == ("Dragon", "k2plus")
    assert split_name("Shop - Dragon - Prusa XL - Plate 2 - My Shop.3mf") == ("Shop - Dragon", "xl")
    assert split_name("Dragon - Snapmaker U1 - My Shop.3mf") == ("Dragon", "u1")
    assert printer_of(Path("IronJaw - Bambu Lab A1 - Unicorn Graveyard.3mf")) == "a1"


def test_a_label_counts_only_as_a_whole_field():
    assert split_name("Dragon - QIDI Q2C - My Shop.3mf")[1] == "q2c"
    assert split_name("Dragon - Bambu Lab A1 mini - My Shop.3mf")[1] == "a1mini"
    assert split_name("Dragon - Anycubic Kobra 3 Max.3mf")[1] == "kobra3max"
    assert split_name("Dragon.3mf") == ("Dragon", None)


def test_retargeting_a_suffixed_file_keeps_the_models_name():
    assert _default_name(Path("IronJaw - Bambu Lab A1 - Unicorn Graveyard.3mf")) == "IronJaw"


def test_convert_and_verify_with_a_suffix(project, tmp_path, capsys):
    out = tmp_path / "files"
    assert main(["convert", str(project), "--name", "Cube", "-p", "u1", "-o", str(out), "--suffix", "My Shop"]) == 0
    assert (out / "Cube" / "Cube - Snapmaker U1 - My Shop.3mf").exists()
    assert main(["verify", str(out / "Cube"), "--source", str(project)]) == 0
    assert "1/1 passed" in capsys.readouterr().out
