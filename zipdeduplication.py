#!/usr/bin/env python3
"""
Advanced ZIP File Deduplication Tool.

This tool identifies and removes duplicate ZIP files based on content,
with options for smart retention of file versions.
"""

import argparse
import concurrent.futures
import hashlib
import logging
import multiprocessing
import os
import re
import shutil
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Any, DefaultDict, Dict, List, Optional, Sequence, Set, Tuple

# (file path, size in bytes, date)
FileInfo = Tuple[str, int, datetime]

# Identifies a file's content; equal signatures mean identical content.
Signature = Tuple[str, Any]

DEFAULT_REPORT_NAME = "zip_content_matches.tsv"
HASH_CHUNK = 1024 * 1024


class ZipDeduplicator:
    """Main class for ZIP file deduplication operations.

    This class handles all aspects of finding and removing duplicate ZIP files
    based on their content, with options for smart retention of file versions.

    Attributes:
        args: Command line arguments
        stats: Statistics about removal operations
        errors: Number of groups or files that could not be processed
    """

    def __init__(self) -> None:
        """Initialize the ZipDeduplicator with default statistics."""
        self.args: Optional[argparse.Namespace] = None
        self.stats: Dict[str, Any] = {
            "total_deleted": 0,
            "total_bytes_saved": 0,
            "directories_processed": defaultdict(lambda: {"files": 0, "bytes": 0}),
        }
        self.errors = 0
        self.zip_files_seen = 0
        # Content signature of every file examined, by path.
        self._signatures: Dict[str, Optional[Signature]] = {}
        # Files removed (or, in a dry run, that would be removed) by this run.
        self._removed: Set[str] = set()

    def parse_arguments(self, argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
        """Parse command line arguments with comprehensive options and detailed help.

        Args:
            argv: Arguments to parse; defaults to the process arguments

        Returns:
            argparse.Namespace: Parsed command line arguments
        """
        cpu_count = multiprocessing.cpu_count()
        default_threads = max(1, cpu_count // 2)

        parser = argparse.ArgumentParser(
            description="Advanced ZIP file deduplication tool that identifies and removes duplicate ZIP files "
                        "based on their contents, not just filenames or sizes.",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )

        # Basic arguments
        parser.add_argument(
            "directory",
            help="Directory to scan for duplicate ZIP files. All .zip files in this directory will be analyzed."
        )
        parser.add_argument(
            "--threads", "-t",
            type=int,
            default=default_threads,
            help=f"Number of processing threads to use for parallel operations. Higher values can improve "
                 f"performance on multi-core systems but may use more system resources. "
                 f"(System has {cpu_count} cores available)"
        )
        parser.add_argument(
            "--recursive", "-r",
            action="store_true",
            help="Scan subdirectories recursively. When enabled, all subdirectories under the specified "
                 "directory will also be searched for ZIP files."
        )

        # Deduplication strategy
        strategy_group = parser.add_argument_group("Deduplication Strategy")
        strategy_group.add_argument(
            "--strategy", "-s",
            choices=["exact", "smart"],
            default="exact",
            help="Deduplication strategy to use: 'exact' only removes completely identical files. "
                 "'smart' also limits each file group to --max-versions different versions, "
                 "deleting the least significant ones when a group has more than that."
        )

        # Smart retention options
        retention_group = parser.add_argument_group("Smart Retention Options")
        retention_group.add_argument(
            "--growth-threshold", "-g",
            type=float,
            default=5.0,
            help="Size growth percentage that makes a version worth keeping. When a group must be cut "
                 "down, versions that grew by more than this percentage are kept ahead of the others, "
                 "largest growth first. Shrinking does not count. Specified as a percentage (e.g., 5.0 = 5%%)."
        )
        retention_group.add_argument(
            "--max-versions", "-m",
            type=int,
            default=5,
            help="Maximum number of different versions to keep for any file group when using smart "
                 "retention (at least 2). Groups with this many versions or fewer are left alone. Larger "
                 "groups are cut to exactly this many: the oldest, the newest, the versions that grew the "
                 "most, then versions spread evenly by date."
        )

        # Safety options
        safety_group = parser.add_argument_group("Safety Options")
        safety_group.add_argument(
            "--dry-run", "-n", "-d",
            action="store_true",
            help="Simulation mode: identify duplicates but don't actually delete or move any files. "
                 "Use this to preview what would happen before making changes."
        )
        safety_group.add_argument(
            "--backup-dir", "-b",
            help="Instead of deleting duplicate files, move them to this directory. "
                 "The original directory structure will be preserved within the backup directory. "
                 "The backup directory itself is never scanned, and files already in it are never overwritten."
        )

        # Output control
        output_group = parser.add_argument_group("Output Options")
        output_group.add_argument(
            "--report",
            nargs="?",
            const=DEFAULT_REPORT_NAME,
            metavar="PATH",
            help="Also write a report of ZIP files with identical content that this run does not remove, "
                 "because they have different names or are in different directories. Nothing listed in it "
                 f"is deleted. The file is overwritten on every run. (path when none is given: {DEFAULT_REPORT_NAME})"
        )
        output_group.add_argument(
            "--verbose", "-v",
            action="count",
            default=0,
            help="Show debug output: processing details and decision-making criteria."
        )
        output_group.add_argument(
            "--quiet", "-q",
            action="store_true",
            help="Suppress non-essential output. Only warnings, errors and final statistics will be displayed."
        )

        args = parser.parse_args(argv)

        if args.threads < 1:
            parser.error("--threads must be at least 1")
        if args.max_versions < 2:
            parser.error("--max-versions must be at least 2 (the oldest and newest are always kept)")
        if not os.path.isdir(args.directory):
            parser.error(f"not a directory: {args.directory}")

        return args

    def setup_logging(self) -> None:
        """Configure logging based on verbosity level."""
        if self.args.quiet:
            level = logging.WARNING
        elif self.args.verbose == 0:
            level = logging.INFO
        else:
            level = logging.DEBUG

        logging.basicConfig(format="%(levelname)s: %(message)s", level=level, force=True)

    @staticmethod
    def format_size(size_bytes: int) -> str:
        """Format file size in human-readable format.

        Args:
            size_bytes: Size in bytes

        Returns:
            str: Human-readable size string
        """
        if size_bytes < 1024:
            return f"{size_bytes} bytes"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes/(1024*1024):.2f} MB"
        else:
            return f"{size_bytes/(1024*1024*1024):.2f} GB"

    @staticmethod
    def extract_date_from_filename(filename: str) -> datetime:
        """Extract date from filename using various patterns.

        Args:
            filename: Path to the file

        Returns:
            datetime: Date from the filename, or the file's modification time
                when the name holds no recognizable date
        """
        # Remove extension
        basename = os.path.splitext(os.path.basename(filename))[0]

        # Common date formats in filenames. Date-and-time patterns must come
        # before the date-only pattern, which would otherwise match first and
        # discard the time of day.
        patterns = [
            # ISO with time: 2023-01-30_14-30-45 or 2023_01_30_14_30_45
            r"(\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2})",
            # Date with spaces: 2023-01-30 14-30-45
            r"(\d{4}[-_]\d{2}[-_]\d{2}\s\d{2}[-_]\d{2}[-_]\d{2})",
            # ISO format: 2023-01-30 or 2023_01_30
            r"(\d{4}[-_]\d{2}[-_]\d{2})",
            # American format: 01-30-2023
            r"(\d{2}[-_]\d{2}[-_]\d{4})",
        ]
        formats = [
            "%Y-%m-%d",
            "%Y_%m_%d",
            "%Y-%m-%d_%H-%M-%S",
            "%Y_%m_%d_%H_%M_%S",
            "%Y-%m-%d %H-%M-%S",
            "%Y-%m-%d %H:%M:%S",
            "%m-%d-%Y",
            "%m_%d_%Y",
        ]

        for pattern in patterns:
            match = re.search(pattern, basename)
            if match:
                for fmt in formats:
                    try:
                        return datetime.strptime(match.group(1), fmt)
                    except ValueError:
                        continue

        # No usable date in the name (a trailing version number such as
        # "tool-1.0.0" is not a date): use the file modification time.
        return datetime.fromtimestamp(os.path.getmtime(filename))

    @staticmethod
    def get_base_filename(filename: str) -> str:
        """Extract the base name excluding date/time components."""
        basename = os.path.basename(filename)

        # Common date patterns to remove
        date_patterns = [
            # Pattern for format like _2024-10-09 05-59-01 (with hyphens in date and time)
            r"_\d{4}-\d{2}-\d{2}\s\d{2}-\d{2}-\d{2}",
            # Previous patterns
            r"_\d{4}[-_]\d{2}[-_]\d{2}",
            r"_\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}",
            r"_\d{4}[-_]\d{2}[-_]\d{2}\s\d{2}[-_]\d{2}[-_]\d{2}",
            r"_\d{2}[-_]\d{2}[-_]\d{4}",
        ]

        # Start with the file name without extension
        base = os.path.splitext(basename)[0]

        # Remove date patterns
        for pattern in date_patterns:
            base = re.sub(pattern, "", base)

        return base

    @staticmethod
    def zip_signature(path: str) -> Optional[Signature]:
        """Describe a file's content so equal content gives an equal signature.

        For a readable ZIP the signature is the name, CRC and uncompressed size
        of every entry (including entries that share a name), so two archives
        holding the same files match even when their bytes differ through
        compression or timestamps. A file that cannot be read as a ZIP is
        identified by the SHA-256 of its bytes, so only exact copies match.

        Args:
            path: Path to the file

        Returns:
            Optional[Signature]: The signature, or None if the file cannot be read
        """
        try:
            with zipfile.ZipFile(path) as archive:
                entries = sorted(
                    (info.filename, info.CRC, info.file_size) for info in archive.infolist()
                )
            return ("zip", tuple(entries))
        except Exception as e:
            logging.debug(f"Not readable as a ZIP ({e}); comparing by whole-file hash: {path}")

        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                    digest.update(chunk)
            return ("raw", digest.hexdigest())
        except OSError as e:
            logging.error(f"Cannot read {path}: {e}")
            return None

    def signature_for(self, path: str) -> Optional[Signature]:
        """Return the content signature of a file, computing it at most once."""
        if path not in self._signatures:
            self._signatures[path] = self.zip_signature(path)
        return self._signatures[path]

    def are_zips_identical(self, file1: str, file2: str) -> bool:
        """Determine if two ZIP files hold identical content.

        Args:
            file1: Path to first ZIP file
            file2: Path to second ZIP file

        Returns:
            bool: True if files are identical, False otherwise
        """
        signature = self.signature_for(file1)
        return signature is not None and signature == self.signature_for(file2)

    def apply_smart_retention(self, file_group: List[FileInfo]) -> List[FileInfo]:
        """Choose which distinct versions of a group to delete.

        A group within ``--max-versions`` is left alone. A larger group is cut
        to exactly that many versions, kept in this order of priority:

        1. The oldest and the newest.
        2. Versions that grew by more than ``--growth-threshold`` percent,
           largest growth first. Growth is measured from the smallest size
           seen since the last such version, so growth after a lasting shrink
           still counts. A version that shrank gets no priority.
        3. Versions nearest the middle of the widest remaining date gaps.

        Args:
            file_group: List of tuples (file_path, size, date), all with
                different content

        Returns:
            List[FileInfo]: Files to delete
        """
        limit = self.args.max_versions
        if len(file_group) <= limit:
            return []

        # Sort files by date; the path breaks ties
        file_group.sort(key=lambda x: (x[2], x[0]))
        last = len(file_group) - 1

        # Always keep the oldest and the newest
        keep: Set[int] = {0, last}

        # Find versions that grew significantly
        growths: List[Tuple[float, int]] = []
        baseline = file_group[0][1]
        for i in range(1, last):
            size = file_group[i][1]
            if baseline > 0:
                growth_pct = (size - baseline) / baseline * 100
            else:
                # Any growth from an empty file counts as significant
                growth_pct = float("inf") if size > 0 else 0.0

            if growth_pct > self.args.growth_threshold:
                growths.append((growth_pct, i))
                baseline = size
            elif size < baseline:
                baseline = size

        # Keep the largest growths that fit (the newer of two equal ones first)
        growths.sort(key=lambda g: (-g[0], -g[1]))
        keep.update(i for _, i in growths[:limit - 2])

        # Fill what room is left from the middle of the widest date gaps
        while len(keep) < limit:
            kept = sorted(keep)
            widest = None
            for start, end in zip(kept, kept[1:]):
                if end - start < 2:
                    continue  # nothing between these two
                gap = file_group[end][2] - file_group[start][2]
                if widest is None or gap > widest[0]:
                    widest = (gap, start, end)

            _, start, end = widest
            middle = file_group[start][2] + (file_group[end][2] - file_group[start][2]) / 2
            keep.add(min(
                range(start + 1, end),
                key=lambda i: abs(file_group[i][2] - middle),
            ))

        # Mark files not kept for deletion
        return [f for i, f in enumerate(file_group) if i not in keep]

    @staticmethod
    def _unused_path(path: str) -> str:
        """Return ``path``, or a numbered variant of it if something is already there."""
        if not os.path.lexists(path):
            return path
        stem, ext = os.path.splitext(path)
        number = 1
        while os.path.lexists(f"{stem} ({number}){ext}"):
            number += 1
        return f"{stem} ({number}){ext}"

    def delete_or_backup_file(self, file_path: str) -> bool:
        """Delete file or move to backup directory based on options."""
        try:
            dir_path = os.path.dirname(file_path)
            file_size = os.path.getsize(file_path)

            if self.args.dry_run:
                logging.info(f"[DRY RUN] Would remove: {file_path}")
            elif self.args.backup_dir:
                # Calculate path relative to the base scan directory
                rel_path = os.path.relpath(dir_path, self.args.directory)
                # Create matching structure in backup directory
                backup_path = os.path.join(self.args.backup_dir, rel_path)
                os.makedirs(backup_path, exist_ok=True)

                # Move file to backup, never replacing an earlier backup
                dest_path = self._unused_path(
                    os.path.join(backup_path, os.path.basename(file_path))
                )
                shutil.move(file_path, dest_path)
                logging.info(f"Moved to backup: {file_path} -> {dest_path}")
            else:
                # Delete file
                os.remove(file_path)
                logging.info(f"Deleted: {file_path}")

            # Update statistics
            self._removed.add(file_path)
            self.stats["total_deleted"] += 1
            self.stats["total_bytes_saved"] += file_size
            self.stats["directories_processed"][dir_path]["files"] += 1
            self.stats["directories_processed"][dir_path]["bytes"] += file_size
            return True
        except Exception as e:
            logging.error(f"Error removing {file_path}: {e}")
            self.errors += 1
            return False

    def process_file_group(self, file_group: List[str]) -> List[FileInfo]:
        """Process a group of files with the same base name pattern.

        Args:
            file_group: Paths of the files in the group

        Returns:
            List[FileInfo]: Files to delete
        """
        # Skip singleton groups
        if len(file_group) <= 1:
            return []

        # Oldest first; the path breaks ties so the result is deterministic
        files: List[FileInfo] = sorted(
            (
                (path, os.path.getsize(path), self.extract_date_from_filename(path))
                for path in file_group
            ),
            key=lambda info: (info[2], info[0]),
        )

        # Group files with identical content
        by_signature: DefaultDict[Signature, List[FileInfo]] = defaultdict(list)
        for info in files:
            signature = self.signature_for(info[0])
            if signature is not None:  # unreadable files are never duplicates
                by_signature[signature].append(info)

        # Mark duplicates for deletion, keeping the oldest of each identical set
        duplicates: List[FileInfo] = []
        for identical in by_signature.values():
            duplicates.extend(identical[1:])

        # Smart retention if enabled
        if self.args.strategy == "smart":
            # Apply smart retention to unique files (excluding duplicates)
            duplicate_paths = {d[0] for d in duplicates}
            unique_files = [info for info in files if info[0] not in duplicate_paths]
            duplicates.extend(self.apply_smart_retention(unique_files))

        return duplicates

    def process_directory(self, directory: str) -> None:
        """Scan directory and process ZIP files for deduplication.

        Args:
            directory: Directory path to scan
        """
        logging.info(f"Scanning directory: {directory}")

        backup_dir = os.path.abspath(self.args.backup_dir) if self.args.backup_dir else None

        # Process files directory by directory
        if self.args.recursive:
            # For recursive mode, process each directory separately
            for root, dirs, files in os.walk(directory):
                # Never scan the backup directory: files moved there would be
                # treated as new duplicates on this or a later run
                dirs[:] = [
                    d for d in dirs if os.path.abspath(os.path.join(root, d)) != backup_dir
                ]
                zip_files = [os.path.join(root, f) for f in files if f.lower().endswith(".zip")]
                if zip_files:
                    logging.info(f"Processing subdirectory: {root}")
                    self._process_directory_files(zip_files, root)
        else:
            # For non-recursive mode, just process the main directory
            zip_files = [
                os.path.join(directory, f)
                for f in os.listdir(directory)
                if f.lower().endswith(".zip")
            ]
            if zip_files:
                self._process_directory_files(zip_files, directory)

        if self.zip_files_seen == 0:
            logging.warning(f"No ZIP files found in {directory}")

    def _process_directory_files(self, zip_files: List[str], directory_path: str) -> None:
        """Process a list of ZIP files within a single directory.

        Args:
            zip_files: List of ZIP file paths to process
            directory_path: The directory these files belong to (for logging)
        """
        if not zip_files:
            return

        self.zip_files_seen += len(zip_files)
        logging.info(f"Found {len(zip_files)} ZIP files to analyze in {directory_path}")

        # Group files by base name pattern
        file_groups: DefaultDict[str, List[str]] = defaultdict(list)
        for file_path in zip_files:
            base_name = self.get_base_filename(file_path)
            file_groups[base_name].append(file_path)

        # Process each group with multiple files using thread pool
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.args.threads)
        finished = False
        try:
            future_to_group = {}

            for base_name, group in file_groups.items():
                if len(group) > 1:
                    future = executor.submit(self.process_file_group, group)
                    future_to_group[future] = base_name

            # The report compares every file, including ones with no sibling
            if self.args.report:
                list(executor.map(self.signature_for, zip_files))

            # Process completed groups and handle duplicates
            for future in concurrent.futures.as_completed(future_to_group):
                base_name = future_to_group[future]
                try:
                    duplicates = future.result()

                    if duplicates:
                        logging.info(
                            f"Found {len(duplicates)} duplicates in group '{base_name}' in {directory_path}"
                        )

                        # Delete or backup duplicates
                        for dup_file, _, _ in duplicates:
                            self.delete_or_backup_file(dup_file)

                except Exception as e:
                    logging.error(f"Error processing group '{base_name}': {e}")
                    self.errors += 1
            finished = True
        finally:
            # On an interrupt or error, drop queued work instead of waiting for it
            executor.shutdown(wait=finished, cancel_futures=not finished)

    def write_report(self, report_path: str) -> int:
        """Write the report of identical content this run did not remove.

        Lists every set of two or more remaining files with identical content.
        After the removal pass, such files differ in name or directory. The
        file is overwritten. Nothing listed is deleted.

        Args:
            report_path: Where to write the tab-separated report

        Returns:
            int: Number of sets written
        """
        by_signature: DefaultDict[Signature, List[str]] = defaultdict(list)
        for path, signature in self._signatures.items():
            if signature is not None and path not in self._removed:
                by_signature[signature].append(path)

        sets = []
        for paths in by_signature.values():
            if len(paths) > 1:
                sizes = {path: os.path.getsize(path) for path in paths}
                # Space that would be freed by keeping only the largest copy
                reclaimable = sum(sizes.values()) - max(sizes.values())
                sets.append((reclaimable, sorted(paths), sizes))
        sets.sort(key=lambda item: (-item[0], item[1]))

        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("set\tfiles_in_set\tsize_bytes\tpath\n")
            for number, (_, paths, sizes) in enumerate(sets, 1):
                for path in paths:
                    handle.write(f"{number}\t{len(paths)}\t{sizes[path]}\t{path}\n")
        return len(sets)

    def _action_label(self) -> str:
        """Describe what this run does with a duplicate, for the statistics."""
        if self.args.dry_run:
            return "would be removed"
        if self.args.backup_dir:
            return "moved to backup"
        return "deleted"

    def print_statistics(self) -> None:
        """Print detailed statistics about the deduplication process."""
        action = self._action_label()
        saved = "Space that would be saved" if self.args.dry_run else "Space saved"

        if self.stats["total_deleted"] == 0:
            print(f"\nNo files {action} during this run.")
            return

        print("\nBreakdown by directory:")
        for directory, dir_stats in sorted(self.stats["directories_processed"].items()):
            if dir_stats["files"] > 0:
                print(f"  {directory}:")
                print(f"    Files {action}: {dir_stats['files']}")
                print(f"    {saved}: {self.format_size(dir_stats['bytes'])}")

        print("\nDeduplication Statistics:")
        print(f"  Total files {action}: {self.stats['total_deleted']}")
        print(f"  {saved}: {self.format_size(self.stats['total_bytes_saved'])}")

    def print_startup_info(self) -> None:
        """Display startup information about the program configuration."""
        total_cores = multiprocessing.cpu_count()
        print("ZIP File Deduplication Tool")
        print(f"Using {self.args.threads} threads out of {total_cores} available cores")
        print(f"Scanning directory: {self.args.directory}")
        print(f"Strategy: {self.args.strategy}")
        if self.args.dry_run:
            print("DRY RUN MODE: No files will be deleted")
        if self.args.backup_dir:
            print(f"Backup mode: Files will be moved to {self.args.backup_dir}")

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        """Run the deduplication process.

        Args:
            argv: Command line arguments; defaults to the process arguments

        Returns:
            int: Exit code: 0 on success, 1 if anything could not be processed,
                130 if interrupted
        """
        # Parse arguments
        self.args = self.parse_arguments(argv)

        # Set up logging
        self.setup_logging()

        # Display startup information
        self.print_startup_info()

        # Create backup directory if specified
        if self.args.backup_dir and not self.args.dry_run:
            os.makedirs(self.args.backup_dir, exist_ok=True)

        # Process the directory
        start_time = time.time()
        outcome = "completed"

        try:
            self.process_directory(self.args.directory)
        except KeyboardInterrupt:
            logging.warning("Interrupted by user. No further files will be removed.")
            outcome = "interrupted"
        except Exception as e:
            logging.error(f"Error: {e}")
            self.errors += 1

        elapsed_time = time.time() - start_time
        print(f"\nProcessing {outcome} in {elapsed_time:.2f} seconds")
        self.print_statistics()

        if outcome == "interrupted":
            return 130

        if self.args.report:
            try:
                count = self.write_report(self.args.report)
                print(f"\nReport: {count} sets of identical content not removed by this run "
                      f"written to {self.args.report}")
            except OSError as e:
                logging.error(f"Could not write report {self.args.report}: {e}")
                self.errors += 1

        if self.errors:
            print(f"\n{self.errors} error(s) occurred; see the log above.")
            return 1
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main program entry point."""
    deduplicator = ZipDeduplicator()
    return deduplicator.run(argv)


if __name__ == "__main__":
    sys.exit(main())
