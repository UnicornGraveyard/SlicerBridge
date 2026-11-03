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


# Every printer's label, colors, vendor, engine and slicer, as they were before they moved into one
# record (PrinterInfo): the record must answer exactly what the per-engine tables did.
BEFORE = {
    "a1mini": ('Bambu Lab A1 mini', 4, 'BBL', 'orca', 'Bambu Studio'),
    "a1": ('Bambu Lab A1', 4, 'BBL', 'orca', 'Bambu Studio'),
    "p1s": ('Bambu Lab P1S', 16, 'BBL', 'orca', 'Bambu Studio'),
    "p2s": ('Bambu Lab P2S', 16, 'BBL', 'orca', 'Bambu Studio'),
    "x1c": ('Bambu Lab X1 Carbon', 16, 'BBL', 'orca', 'Bambu Studio'),
    "h2s": ('Bambu Lab H2S', 16, 'BBL', 'orca', 'Bambu Studio'),
    "h2d": ('Bambu Lab H2D', 16, 'BBL', 'orca', 'Bambu Studio'),
    "h2dpro": ('Bambu Lab H2D Pro', 16, 'BBL', 'orca', 'Bambu Studio'),
    "h2c": ('Bambu Lab H2C', 16, 'BBL', 'orca', 'Bambu Studio'),
    "x2d": ('Bambu Lab X2D', 16, 'BBL', 'orca', 'Bambu Studio'),
    "k2plus": ('Creality K2 Plus', 4, 'Creality', 'orca', 'OrcaSlicer'),
    "k2pro": ('Creality K2 Pro', 4, 'Creality', 'orca', 'OrcaSlicer'),
    "k2se": ('Creality K2 SE', 4, 'Creality', 'orca', 'OrcaSlicer'),
    "hi": ('Creality Hi', 4, 'Creality', 'orca', 'OrcaSlicer'),
    "k1c": ('Creality K1C CFS', 4, 'Creality', 'orca', 'OrcaSlicer'),
    "cc": ('Elegoo Centauri Carbon', 4, 'Elegoo', 'orca', 'OrcaSlicer'),
    "kobras1": ('Anycubic Kobra S1', 4, 'Anycubic', 'orca', 'OrcaSlicer'),
    "kobras1max": ('Anycubic Kobra S1 Max', 4, 'Anycubic', 'orca', 'OrcaSlicer'),
    "kobra3": ('Anycubic Kobra 3', 4, 'Anycubic', 'orca', 'OrcaSlicer'),
    "kobra3max": ('Anycubic Kobra 3 Max', 4, 'Anycubic', 'orca', 'OrcaSlicer'),
    "kobrax": ('Anycubic Kobra X', 4, 'Anycubic', 'orca', 'OrcaSlicer'),
    "ad5x": ('Flashforge AD5X', 4, 'Flashforge', 'orca', 'OrcaSlicer'),
    "creator5": ('Flashforge Creator 5', 4, 'Flashforge', 'orca', 'OrcaSlicer'),
    "creator5pro": ('Flashforge Creator 5 Pro', 4, 'Flashforge', 'orca', 'OrcaSlicer'),
    "q2": ('QIDI Q2', 4, 'Qidi', 'orca', 'OrcaSlicer'),
    "q2c": ('QIDI Q2C', 4, 'Qidi', 'orca', 'OrcaSlicer'),
    "plus4": ('QIDI Plus4', 4, 'Qidi', 'orca', 'OrcaSlicer'),
    "max4": ('QIDI Max4', 4, 'Qidi', 'orca', 'OrcaSlicer'),
    "zrultras": ('WonderMaker ZR Ultra S', 4, 'WonderMaker', 'orca', 'OrcaSlicer'),
    "mmu3": ('Prusa MMU3', 5, 'Prusa', 'prusa', 'PrusaSlicer'),
    "xl": ('Prusa XL', 5, 'Prusa', 'prusa', 'PrusaSlicer'),
    "indx4": ('Prusa Core One INDX 4T', 4, 'Prusa', 'prusa', 'PrusaSlicer'),
    "indx8": ('Prusa Core One INDX 8T', 8, 'Prusa', 'prusa', 'PrusaSlicer'),
    "u1": ('Snapmaker U1', 4, 'Snapmaker', 'u1', 'Snapmaker Orca'),
}


def test_each_printer_answers_as_it_did_before_one_record():
    assert list(BEFORE) == P.ALL
    for k, (label, tools, vendor, engine, slicer) in BEFORE.items():
        assert (P.label_of(k), P.tools_of(k), P.vendor_of(k), P.engine_of(k)) == (label, tools, vendor, engine), k
        info = P.info(k)
        assert (info.key, info.label, info.tools, info.vendor, info.engine) == (k, label, tools, vendor, engine), k
        assert P.SLICERS[info.slicer].name == slicer, k


def test_an_unknown_printer_is_a_key_error():
    with pytest.raises(KeyError):
        P.engine_of("ender3")
    with pytest.raises(KeyError):
        P.info("ender3")
