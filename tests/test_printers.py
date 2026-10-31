from pathlib import Path

import pytest

from slicerbridge import printers as P


def test_every_key_has_one_engine_and_a_label():
    assert len(P.ALL) == len(set(P.ALL)) == 34
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


def _missing_orca(monkeypatch, tmp_path, env=None):
    calls = []
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P.shutil, "which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(P.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or P.subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(P, "_TRIED_CASKS", set())
    if env:
        monkeypatch.setenv("SLICERBRIDGE_ORCASLICER", env)
    else:
        monkeypatch.delenv("SLICERBRIDGE_ORCASLICER", raising=False)
    return P.Slicer("OrcaSlicer", tmp_path / "OrcaSlicer.app", cask="orcaslicer"), calls


def test_a_missing_orcaslicer_is_installed_with_homebrew_once(monkeypatch, tmp_path):
    orca, calls = _missing_orca(monkeypatch, tmp_path)
    P.ensure_installed(orca)
    P.ensure_installed(orca)  # the next printer in the same run: not again
    assert calls == [["/opt/homebrew/bin/brew", "install", "--cask", "orcaslicer"]]
    assert "brew install --cask orcaslicer" in P.not_found(orca)


def test_homebrew_never_installs_over_a_path_someone_set_or_a_slicer_that_is_there(monkeypatch, tmp_path):
    orca, calls = _missing_orca(monkeypatch, tmp_path, env=str(tmp_path / "OrcaSlicer-2.4.2.app"))
    P.ensure_installed(orca)
    assert calls == [] and "unset SLICERBRIDGE_ORCASLICER" in P.not_found(orca)
    monkeypatch.delenv("SLICERBRIDGE_ORCASLICER")
    orca.binary.parent.mkdir(parents=True)
    orca.binary.touch()
    P.ensure_installed(orca)
    assert calls == []


def test_what_a_slicer_wont_load_is_read_in_each_slicers_words():
    from slicerbridge.engines import prusa
    log = ("[info] loading\nParam values in 3mf/config error: \ntree_support_wall_count: -1 not in range [0,2]\n"
           "Value out of range: wipe_tower_brim_width\n"
           "[error] Invalid value provided for parameter brim_width: abc\n"
           "tree_support_wall_count: 5 not in range [0,2]\nrun found error, exit\n")
    assert P.refused_settings(log) == {
        "tree_support_wall_count": "-1 not in range [0,2]",  # OrcaSlicer and its forks; the first word kept
        "wipe_tower_brim_width": "Value out of range: wipe_tower_brim_width",  # PrusaSlicer
        "brim_width": "Invalid value provided for parameter brim_width: abc"}
    check_9 = prusa.Check("9", "PrusaSlicer --info loads it", False, "exit 1", log)
    assert prusa.refused_key([check_9], {"brim_width", "wipe_tower_brim_width"}) == "wipe_tower_brim_width"
    assert prusa.refused_key([check_9], {"wall_loops"}) is None
