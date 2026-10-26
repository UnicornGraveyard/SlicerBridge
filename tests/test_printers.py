from pathlib import Path

import pytest

from slicerbridge import printers as P


def test_every_key_has_one_engine_and_a_label():
    assert len(P.ALL) == len(set(P.ALL)) == 30
    for k in P.ALL:
        assert P.engine_of(k) in ("orca", "prusa", "u1")
        assert P.label_of(k) and P.tools_of(k) >= 4


def test_sets_name_real_printers():
    for members in P.SETS.values():
        assert set(members) <= set(P.ALL)


def test_select_all_sets_vendors_and_keys():
    assert P.select(["all"]) == P.ALL
    assert P.select(["popular"]) == P.SETS["popular"]
    assert P.select(["creality"]) == ["k2plus", "k2pro", "k2se", "hi", "k1c"]
    assert P.select(["bambu"]) == [k for k in P.PRINTERS if P.PRINTERS[k].vendor == "BBL"]
    assert P.select(["prusa", "snapmaker"]) == [*P.PRUSA_PRINTERS, "u1"]
    assert P.select(["q2, u1", "q2"]) == ["q2", "u1"]  # comma-separated or repeated, no repeats


def test_select_refuses_unknown_printers():
    with pytest.raises(SystemExit, match="unknown printer 'k9'"):
        P.select(["k9"])


def test_file_names_name_the_printer():
    assert P.out_name("Cube", "q2") == "Cube - QIDI Q2.3mf"
    assert P.out_name("Cube", "mmu3", "Shop - ", " - Plate 2") == "Shop - Cube - Prusa MMU3 - Plate 2.3mf"
    assert P.printer_of(Path("x/Shop - Cube - Anycubic Kobra 3.3mf")) == "kobra3"
    assert P.printer_of(Path("x/Shop - Cube - Anycubic Kobra 3 Max.3mf")) == "kobra3max"


def test_cache_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SLICERBRIDGE_CACHE", str(tmp_path))
    assert P.cache_dir() == tmp_path
