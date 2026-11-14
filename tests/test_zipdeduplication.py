"""Tests for zipdeduplication. All files are created under pytest's tmp_path."""

import argparse
import os
import struct
import sys
import warnings
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zipdeduplication  # noqa: E402
from zipdeduplication import ZipDeduplicator, main  # noqa: E402


def make_zip(path, entries):
    """Write a stored (uncompressed) ZIP from a list of (name, bytes) pairs."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate entry names are intentional
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return str(path)


def with_crc_residue(data):
    """Append the data's own CRC32 so the result always has the same CRC32."""
    return data + struct.pack("<I", zlib.crc32(data))


# --- extract_date_from_filename -------------------------------------------

@pytest.mark.parametrize(
    "filename, want",
    [
        ("repo_2024-10-09 05-59-01.zip", datetime(2024, 10, 9, 5, 59, 1)),
        ("repo_2023-01-30_14-30-45.zip", datetime(2023, 1, 30, 14, 30, 45)),
        ("repo_2023_01_30_14_30_45.zip", datetime(2023, 1, 30, 14, 30, 45)),
    ],
)
def test_extract_date_keeps_time_of_day(filename, want):
    assert ZipDeduplicator.extract_date_from_filename(filename) == want


def test_extract_date_parses_date_only_names():
    got = ZipDeduplicator.extract_date_from_filename("repo_2024-10-09.zip")
    assert got == datetime(2024, 10, 9)


def test_smart_retention_keeps_newest_same_day_snapshot(tmp_path):
    # Five snapshots on one day; the newest (05:00) is not the largest.
    sizes = {1: 10000, 2: 10300, 3: 10200, 4: 10100, 5: 10050}
    files = [
        make_zip(tmp_path / f"repo_2024-10-09 0{hour}-00-00.zip",
                 [("a.bin", bytes([hour]) * size)])
        for hour, size in sizes.items()
    ]
    dedup = ZipDeduplicator()
    dedup.args = argparse.Namespace(
        strategy="smart", max_versions=3, growth_threshold=5.0
    )

    deleted = {Path(d[0]).name for d in dedup.process_file_group(files)}

    assert "repo_2024-10-09 05-00-00.zip" not in deleted  # newest
    assert "repo_2024-10-09 01-00-00.zip" not in deleted  # oldest


# --- ZIP entry comparison --------------------------------------------------

def test_archives_differing_in_shadowed_duplicate_entry_are_not_identical(tmp_path):
    a = make_zip(tmp_path / "a.zip", [("f.txt", b"AAAA"), ("f.txt", b"same")])
    b = make_zip(tmp_path / "b.zip", [("f.txt", b"BBBB"), ("f.txt", b"same")])

    assert ZipDeduplicator().are_zips_identical(a, b) is False


def test_same_name_and_crc_but_different_entry_size_is_not_a_match(tmp_path):
    short, long = with_crc_residue(b"a"), with_crc_residue(b"bb")
    assert zlib.crc32(short) == zlib.crc32(long) and len(short) != len(long)
    a = make_zip(tmp_path / "a.zip", [("f.bin", short)])
    b = make_zip(tmp_path / "b.zip", [("f.bin", long)])

    assert ZipDeduplicator().are_zips_identical(a, b) is False


def test_same_entries_in_different_order_are_identical(tmp_path):
    a = make_zip(tmp_path / "a.zip", [("x.txt", b"one"), ("y.txt", b"two")])
    b = make_zip(tmp_path / "b.zip", [("y.txt", b"two"), ("x.txt", b"one")])

    assert ZipDeduplicator().are_zips_identical(a, b) is True


# --- Content signature -----------------------------------------------------

def exact_args(**overrides):
    values = dict(strategy="exact", max_versions=5, growth_threshold=5.0)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_same_content_with_different_archive_size_is_a_duplicate(tmp_path):
    # Same entries, one stored and one compressed: different bytes, same content.
    older = make_zip(tmp_path / "repo_2024-01-01 00-00-00.zip", [("a.txt", b"x" * 5000)])
    newer = str(tmp_path / "repo_2024-01-02 00-00-00.zip")
    with zipfile.ZipFile(newer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", b"x" * 5000)
    assert os.path.getsize(older) != os.path.getsize(newer)
    dedup = ZipDeduplicator()
    dedup.args = exact_args()

    deleted = [d[0] for d in dedup.process_file_group([older, newer])]

    assert deleted == [newer]


def test_byte_identical_files_that_are_not_zips_are_duplicates(tmp_path):
    a, b, c = (tmp_path / n for n in ("x_2024-01-01.zip", "x_2024-01-02.zip", "x_2024-01-03.zip"))
    a.write_bytes(b"not a zip")
    b.write_bytes(b"not a zip")
    c.write_bytes(b"also junk")
    dedup = ZipDeduplicator()
    dedup.args = exact_args()

    deleted = [d[0] for d in dedup.process_file_group([str(a), str(b), str(c)])]

    assert deleted == [str(b)]


# --- Dates -----------------------------------------------------------------

def test_trailing_version_digits_are_not_taken_as_a_date(tmp_path):
    path = tmp_path / "tool-1.0.0.zip"
    path.write_bytes(b"")
    os.utime(path, (1700000000, 1700000000))

    assert ZipDeduplicator.extract_date_from_filename(str(path)) == datetime.fromtimestamp(1700000000)


# --- Smart retention -------------------------------------------------------

def versions(sizes, days_apart=1):
    """One version per size, named f0, f1, ... and spaced ``days_apart`` days."""
    return [(f"f{i}", size, datetime(2024, 1, 1) + timedelta(days=i * days_apart))
            for i, size in enumerate(sizes)]


def kept_after_retention(group, **overrides):
    dedup = ZipDeduplicator()
    dedup.args = exact_args(strategy="smart", **overrides)
    deleted = dedup.apply_smart_retention(list(group))
    return [f[0] for f in group if f not in deleted]


def test_nothing_is_deleted_while_the_group_is_within_the_limit():
    # Five near-identical versions a day apart: no growth, but within the limit.
    group = versions([1000, 1001, 1002, 1003, 1004])
    assert kept_after_retention(group, max_versions=5) == ["f0", "f1", "f2", "f3", "f4"]


def test_group_over_the_limit_is_cut_to_exactly_the_limit():
    assert len(kept_after_retention(versions([1000 * 2 ** i for i in range(12)]), max_versions=4)) == 4
    # Nine near-identical versions in nine days: one over the limit, so one is removed.
    assert len(kept_after_retention(versions([1000] * 9), max_versions=8)) == 8


def test_version_that_grew_is_kept_ahead_of_evenly_spaced_versions():
    # Ten versions 10 days apart; the only growth is the jump at f6.
    group = versions([1000] * 6 + [1500] * 4, days_apart=10)
    assert kept_after_retention(group, max_versions=4) == ["f0", "f3", "f6", "f9"]


def test_largest_growths_win_when_there_are_more_than_fit():
    # Growth: f1 +10%, f2 +100%, f3 +10%; only one slot between oldest and newest.
    assert kept_after_retention(versions([1000, 1100, 2200, 2420, 2500]), max_versions=3) == ["f0", "f2", "f4"]


def test_shrunken_version_gets_no_priority():
    # f1 is a 60% shrink. It is not kept for that; the spare slot goes to the middle date.
    assert kept_after_retention(versions([1000, 400, 401, 402, 403]), max_versions=3) == ["f0", "f2", "f4"]


def test_growth_after_a_lasting_shrink_still_counts():
    # After dropping to 400, f2 grows 50% -- measured from the low point, not from f0.
    group = versions([1000, 400, 600, 601, 602, 603, 604])
    assert kept_after_retention(group, max_versions=3) == ["f0", "f2", "f6"]


def test_spare_slots_are_filled_evenly_across_the_biggest_time_gaps():
    group = versions([1000] * 30)
    assert kept_after_retention(group, max_versions=5) == ["f0", "f7", "f14", "f21", "f29"]


def test_zero_byte_file_does_not_abort_smart_retention():
    group = [("empty", 0, datetime(2024, 1, 1)), ("b", 500, datetime(2024, 1, 2)),
             ("c", 505, datetime(2024, 1, 3)), ("d", 506, datetime(2024, 1, 4))]
    assert kept_after_retention(group, max_versions=3) == ["empty", "b", "d"]


def test_identical_copies_do_not_count_toward_the_limit(tmp_path):
    # Four files, two of them identical: three distinct versions fit a limit of three.
    paths = [make_zip(tmp_path / f"repo_2024-01-0{day} 00-00-00.zip", [("a.txt", content)])
             for day, content in [(1, b"one"), (2, b"one"), (3, b"two"), (4, b"three")]]
    dedup = ZipDeduplicator()
    dedup.args = exact_args(strategy="smart", max_versions=3)

    deleted = [d[0] for d in dedup.process_file_group(paths)]

    assert deleted == [paths[1]]


# --- Command line ----------------------------------------------------------

def make_series(directory, name="repo"):
    """Two identical snapshots and one different one. Returns their paths, oldest first."""
    directory.mkdir(parents=True, exist_ok=True)
    return [
        make_zip(directory / f"{name}_2024-01-01 00-00-00.zip", [("a.txt", b"one")]),
        make_zip(directory / f"{name}_2024-01-02 00-00-00.zip", [("a.txt", b"one")]),
        make_zip(directory / f"{name}_2024-01-03 00-00-00.zip", [("a.txt", b"two")]),
    ]


def zips_under(directory):
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*.zip"))


@pytest.mark.parametrize("extra", [["-s", "smart", "-m", "1"], ["-t", "0"]])
def test_invalid_option_values_are_rejected(tmp_path, extra):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), *extra])
    assert exc.value.code == 2


def test_missing_directory_is_an_error_even_when_recursive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "nope"), "-r"])
    assert exc.value.code == 2


def test_run_deletes_the_newer_identical_snapshot(tmp_path):
    first, second, third = make_series(tmp_path / "d")

    assert main([str(tmp_path), "-r", "-q"]) == 0

    assert [os.path.exists(p) for p in (first, second, third)] == [True, False, True]


def test_dry_run_deletes_nothing_and_does_not_report_files_as_deleted(tmp_path, capsys):
    files = make_series(tmp_path / "d")

    assert main([str(tmp_path), "-r", "--dry-run", "-q"]) == 0

    assert all(os.path.exists(p) for p in files)
    out = capsys.readouterr().out.lower()
    assert "would be removed: 1" in out and "files deleted" not in out


@pytest.mark.parametrize("flag", ["-n", "-d"])
def test_short_dry_run_flags_delete_nothing(tmp_path, flag):
    files = make_series(tmp_path / "d")

    assert main([str(tmp_path), "-r", flag, "-q"]) == 0

    assert all(os.path.exists(p) for p in files)


def test_backup_directory_inside_the_scanned_tree_is_not_rescanned(tmp_path):
    make_series(tmp_path / "d", "one")
    make_series(tmp_path / "d", "two")
    backup = tmp_path / "backup"

    main([str(tmp_path), "-r", "-q", "-b", str(backup)])
    after_first = zips_under(tmp_path)
    main([str(tmp_path), "-r", "-q", "-b", str(backup)])

    assert zips_under(tmp_path) == after_first
    assert zips_under(backup) == [os.path.join("d", "one_2024-01-02 00-00-00.zip"),
                                  os.path.join("d", "two_2024-01-02 00-00-00.zip")]


def test_backup_never_overwrites_a_file_already_in_the_backup(tmp_path):
    make_series(tmp_path / "scan" / "d")
    backup = tmp_path / "backup"
    (backup / "d").mkdir(parents=True)
    earlier = backup / "d" / "repo_2024-01-02 00-00-00.zip"
    earlier.write_bytes(b"earlier backup")

    main([str(tmp_path / "scan"), "-r", "-q", "-b", str(backup)])

    assert earlier.read_bytes() == b"earlier backup"
    assert len(zips_under(backup)) == 2


def test_interrupt_exits_130_and_prints_statistics_once(tmp_path, monkeypatch, capsys):
    def interrupted(self, directory):
        raise KeyboardInterrupt

    monkeypatch.setattr(ZipDeduplicator, "process_directory", interrupted)

    assert main([str(tmp_path), "-q"]) == 130
    assert capsys.readouterr().out.count("Processing ") == 1


# --- Cross-directory report ------------------------------------------------

def report_groups(path):
    groups = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines()[1:]:
        group, _, _, file_path = line.split("\t")
        groups.setdefault(group, []).append(file_path)
    return sorted(sorted(g) for g in groups.values())


def test_report_lists_same_content_under_different_names_and_directories(tmp_path):
    a = make_zip((tmp_path / "x").mkdir() or tmp_path / "x" / "alpha.zip", [("a.txt", b"same")])
    b = make_zip((tmp_path / "y").mkdir() or tmp_path / "y" / "beta.zip", [("a.txt", b"same")])
    make_zip(tmp_path / "y" / "other.zip", [("a.txt", b"different")])
    report = tmp_path / "report.tsv"

    assert main([str(tmp_path), "-r", "-q", "--report", str(report)]) == 0

    assert report_groups(report) == [sorted([a, b])]
    assert os.path.exists(a) and os.path.exists(b)  # reported, never deleted


def test_report_leaves_out_files_removed_as_duplicates_and_is_overwritten(tmp_path):
    first, second, third = make_series(tmp_path / "d")
    elsewhere = make_zip((tmp_path / "e").mkdir() or tmp_path / "e" / "copy.zip", [("a.txt", b"one")])
    report = tmp_path / "report.tsv"
    report.write_text("stale contents from an earlier run\n", encoding="utf-8")

    main([str(tmp_path), "-r", "-q", "--dry-run", "--report", str(report)])

    assert report_groups(report) == [sorted([first, elsewhere])]
    assert "stale" not in report.read_text(encoding="utf-8")
