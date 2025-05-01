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
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple, Union

import zipfile


class ZipDeduplicator:
    """Main class for ZIP file deduplication operations.

    This class handles all aspects of finding and removing duplicate ZIP files
    based on their content, with options for smart retention of file versions.

    Attributes:
        args: Command line arguments
        stats: Statistics about deletion operations
    """

    def __init__(self) -> None:
        """Initialize the ZipDeduplicator with default statistics."""
        self.args: argparse.Namespace = None
        self.stats: Dict[str, Any] = {
            "total_deleted": 0,
            "total_bytes_saved": 0,
            "directories_processed": defaultdict(lambda: {"files": 0, "bytes": 0}),
        }

    def parse_arguments(self) -> argparse.Namespace:
        """Parse command line arguments with comprehensive options and detailed help.

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
            help="Deduplication strategy to use: 'exact' only removes completely identical files, "
                 "'smart' applies retention policies to keep representative versions while "
                 "removing intermediate minor changes."
        )

        # Smart retention options
        retention_group = parser.add_argument_group("Smart Retention Options")
        retention_group.add_argument(
            "--growth-threshold", "-g",
            type=float,
            default=5.0,
            help="Size growth percentage threshold. When a file grows by less than this percentage "
                 "compared to a previous version, it's considered a minor change. Higher values will "
                 "result in more files being considered duplicates. Specified as a percentage (e.g., 5.0 = 5%%)."
        )
        retention_group.add_argument(
            "--keep-interval", "-k",
            type=int,
            default=7,
            help="Minimum interval in days between retained versions when using smart retention. "
                 "Files with timestamps closer than this interval may be considered duplicates "
                 "if other criteria are met."
        )
        retention_group.add_argument(
            "--max-versions", "-m",
            type=int,
            default=5,
            help="Maximum number of versions to keep for any file group when using smart retention. "
                 "This ensures that even with many small changes, you'll never keep more than "
                 "this many versions. Always keeps at least the oldest and newest version."
        )

        # Safety options
        safety_group = parser.add_argument_group("Safety Options")
        safety_group.add_argument(
            "--dry-run", "-n",
            action="store_true",
            help="Simulation mode: identify duplicates but don't actually delete or move any files. "
                 "Use this to preview what would happen before making changes."
        )
        safety_group.add_argument(
            "--backup-dir", "-b",
            help="Instead of deleting duplicate files, move them to this directory. "
                 "The original directory structure will be preserved within the backup directory."
        )

        # Output control
        output_group = parser.add_argument_group("Output Options")
        output_group.add_argument(
            "--verbose", "-v",
            action="count",
            default=0,
            help="Increase output verbosity. Use -v for detailed output or -vv for debug level information. "
                 "Shows additional processing details and decision-making criteria."
        )
        output_group.add_argument(
            "--quiet", "-q",
            action="store_true",
            help="Suppress non-essential output. Only warnings, errors and final statistics will be displayed."
        )

        return parser.parse_args()

    def setup_logging(self) -> None:
        """Configure logging based on verbosity level."""
        if self.args.quiet:
            level = logging.WARNING
        elif self.args.verbose == 0:
            level = logging.INFO
        else:
            level = logging.DEBUG

        logging.basicConfig(format="%(levelname)s: %(message)s", level=level)

    def signal_handler(self, sig: int, frame: Any) -> None:
        """Handle interrupt signals to show statistics before exit.

        Args:
            sig: Signal number
            frame: Current stack frame
        """
        logging.warning("\nProcess interrupted by user. Finalizing...")
        self.print_statistics()
        sys.exit(0)

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
    def extract_date_from_filename(filename: str) -> Union[datetime, str]:
        """Extract date from filename using various patterns.

        Args:
            filename: Path to the file

        Returns:
            Union[datetime, str]: Extracted date or date string
        """
        # Remove extension
        basename = os.path.splitext(os.path.basename(filename))[0]

        # Common date formats in filenames
        patterns = [
            # ISO format: 2023-01-30 or 2023_01_30
            r"(\d{4}[-_]\d{2}[-_]\d{2})",
            # ISO with time: 2023-01-30_14-30-45 or 2023_01_30_14_30_45
            r"(\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2})",
            # Date with spaces: 2023-01-30 14-30-45
            r"(\d{4}[-_]\d{2}[-_]\d{2}\s\d{2}[-_]\d{2}[-_]\d{2})",
            # American format: 01-30-2023
            r"(\d{2}[-_]\d{2}[-_]\d{4})",
            # Simple number sequence (fallback)
            r"(\d+)$",
        ]

        for pattern in patterns:
            match = re.search(pattern, basename)
            if match:
                date_str = match.group(1)
                try:
                    # Try parsing with different formats
                    for fmt in [
                        "%Y-%m-%d",
                        "%Y_%m_%d",
                        "%Y-%m-%d_%H-%M-%S",
                        "%Y_%m_%d_%H_%M_%S",
                        "%Y-%m-%d %H-%M-%S",
                        "%Y-%m-%d %H:%M:%S",
                        "%m-%d-%Y",
                        "%m_%d_%Y",
                    ]:
                        try:
                            return datetime.strptime(date_str, fmt)
                        except ValueError:
                            continue

                    # Fallback: just use the string for comparison
                    return date_str
                except Exception:
                    return date_str

        # If no date found, use file modification time
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
    def compare_zips_by_crc(file1: str, file2: str) -> bool:
        """Compare two ZIP files by checking CRC values of internal files.

        Args:
            file1: Path to first ZIP file
            file2: Path to second ZIP file

        Returns:
            bool: True if all CRCs match, False otherwise
        """
        try:
            with zipfile.ZipFile(file1) as zip1, zipfile.ZipFile(file2) as zip2:
                # Get file listings
                files1 = sorted(zip1.infolist(), key=lambda x: x.file_size)
                files2 = sorted(zip2.infolist(), key=lambda x: x.file_size)

                # Quick check for different file counts
                if len(files1) != len(files2):
                    return False

                # Create dictionaries of filename to CRC
                crc_dict1 = {info.filename: info.CRC for info in files1}
                crc_dict2 = {info.filename: info.CRC for info in files2}

                # Check if file lists match
                if set(crc_dict1.keys()) != set(crc_dict2.keys()):
                    return False

                # Compare CRCs of each file
                for filename, crc1 in crc_dict1.items():
                    if crc1 != crc_dict2[filename]:
                        return False

                # All CRCs match
                return True
        except Exception as e:
            logging.error(f"Error comparing ZIP files: {e}")
            return False

    @staticmethod
    def fallback_compare_zips_by_hash(file1: str, file2: str) -> bool:
        """Fallback hash-based comparison if CRC check is inconclusive.

        Args:
            file1: Path to first ZIP file
            file2: Path to second ZIP file

        Returns:
            bool: True if all file contents match, False otherwise
        """
        try:
            with zipfile.ZipFile(file1) as zip1, zipfile.ZipFile(file2) as zip2:
                # Get file listings sorted by size (smallest first)
                files1 = sorted(zip1.infolist(), key=lambda x: x.file_size)
                files2 = sorted(zip2.infolist(), key=lambda x: x.file_size)

                # Check file count
                if len(files1) != len(files2):
                    return False

                # Create file name maps
                file_map1 = {info.filename: info for info in files1}
                file_map2 = {info.filename: info for info in files2}

                # Check if file lists match
                if set(file_map1.keys()) != set(file_map2.keys()):
                    return False

                # Compare content of each file, starting with smallest
                for filename in sorted(
                    file_map1.keys(), key=lambda x: file_map1[x].file_size
                ):
                    data1 = zip1.read(filename)
                    data2 = zip2.read(filename)

                    # Quick size check
                    if len(data1) != len(data2):
                        return False

                    # Compare sha256 hashes
                    hash1 = hashlib.sha256(data1).digest()
                    hash2 = hashlib.sha256(data2).digest()

                    if hash1 != hash2:
                        return False

                # All files identical
                return True
        except Exception as e:
            logging.error(f"Error in fallback comparison: {e}")
            return False

    def are_zips_identical(self, file1: str, file2: str) -> bool:
        """Determine if two ZIP files are identical using progressive checks.

        Args:
            file1: Path to first ZIP file
            file2: Path to second ZIP file

        Returns:
            bool: True if files are identical, False otherwise
        """
        # Quick size check first
        if os.path.getsize(file1) != os.path.getsize(file2):
            return False

        logging.debug(f"Comparing ZIP files: {file1} and {file2}")

        # Method 1: CRC comparison (fastest)
        try:
            result = self.compare_zips_by_crc(file1, file2)
            if result:
                logging.debug("Files match by CRC check")
                return True
            logging.debug("CRC check failed, trying content hash")
        except Exception as e:
            logging.debug(f"CRC comparison error: {e}, trying content hash")

        # Method 2: Content hash comparison
        try:
            result = self.fallback_compare_zips_by_hash(file1, file2)
            if result:
                logging.debug("Files match by content hash")
                return True
            logging.debug("Content hash check failed")
        except Exception as e:
            logging.debug(f"Content hash comparison error: {e}, trying whole-file hash")

        # Method 3: Last resort - whole file hash comparison
        try:
            with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
                # Compare file hashes in chunks to avoid memory issues
                h1 = hashlib.sha256()
                h2 = hashlib.sha256()

                while True:
                    chunk1 = f1.read(8192)
                    chunk2 = f2.read(8192)

                    if not chunk1 and not chunk2:  # Both files ended
                        result = h1.digest() == h2.digest()
                        if result:
                            logging.debug("Files match by whole-file hash")
                        return result

                    if not chunk1 or not chunk2 or len(chunk1) != len(chunk2):
                        return False  # Files have different lengths

                    h1.update(chunk1)
                    h2.update(chunk2)
        except Exception as e:
            logging.error(f"Whole-file hash comparison error: {e}")
            return False

    def apply_smart_retention(
        self, file_group: List[Tuple[str, int, Union[datetime, str]]]
    ) -> List[Tuple[str, int, Union[datetime, str]]]:
        """Apply smart retention policy to a group of files.

        Args:
            file_group: List of tuples (file_path, size, date)

        Returns:
            List[Tuple[str, int, Union[datetime, str]]]: Files to delete
        """
        # Sort files by date
        file_group.sort(key=lambda x: x[2])

        # Always keep the oldest file
        to_keep = [file_group[0]]

        # Analyze size changes
        for i in range(1, len(file_group)):
            prev_file, prev_size, prev_date = to_keep[-1]
            curr_file, curr_size, curr_date = file_group[i]

            # Always keep the file if it's smaller than previous
            if curr_size < prev_size:
                to_keep.append(file_group[i])
                continue

            # Calculate size increase percentage
            size_increase_pct = ((curr_size - prev_size) / prev_size) * 100

            # If significant size increase, keep it
            if size_increase_pct > self.args.growth_threshold:
                to_keep.append(file_group[i])
                continue

            # Check time interval
            if isinstance(prev_date, datetime) and isinstance(curr_date, datetime):
                days_diff = (curr_date - prev_date).days
                if days_diff >= self.args.keep_interval:
                    to_keep.append(file_group[i])
                    continue

        # Always keep the newest file if not already kept
        if file_group[-1] not in to_keep:
            to_keep.append(file_group[-1])

        # If we have too many versions, keep first, last, and evenly spaced ones
        if len(to_keep) > self.args.max_versions:
            # Always keep first and last
            must_keep = [to_keep[0], to_keep[-1]]

            # Add evenly spaced samples from the middle
            middle = to_keep[1:-1]
            samples = self.args.max_versions - 2  # Subtract first and last

            if samples > 0 and middle:
                step = max(1, len(middle) // samples)
                for i in range(0, len(middle), step):
                    if len(must_keep) < self.args.max_versions:
                        must_keep.append(middle[i])

            to_keep = must_keep

        # Mark files not in to_keep for deletion
        files_to_delete = [f for f in file_group if f not in to_keep]
        return files_to_delete

    def delete_or_backup_file(self, file_path: str) -> bool:
        """Delete file or move to backup directory based on options."""
        try:
            dir_path = os.path.dirname(file_path)
            file_size = os.path.getsize(file_path)

            if self.args.dry_run:
                logging.info(f"[DRY RUN] Would remove: {file_path}")
                # Update stats as if deleted
                self.stats["total_deleted"] += 1
                self.stats["total_bytes_saved"] += file_size
                self.stats["directories_processed"][dir_path]["files"] += 1
                self.stats["directories_processed"][dir_path]["bytes"] += file_size
                return True

            if self.args.backup_dir:
                # Calculate path relative to the base scan directory
                rel_path = os.path.relpath(dir_path, self.args.directory)
                # Create matching structure in backup directory
                backup_path = os.path.join(self.args.backup_dir, rel_path)
                os.makedirs(backup_path, exist_ok=True)

                # Move file to backup
                dest_path = os.path.join(backup_path, os.path.basename(file_path))
                shutil.move(file_path, dest_path)
                logging.info(f"Moved to backup: {file_path} -> {dest_path}")
            else:
                # Delete file
                os.remove(file_path)
                logging.info(f"Deleted: {file_path}")

            # Update statistics
            self.stats["total_deleted"] += 1
            self.stats["total_bytes_saved"] += file_size
            self.stats["directories_processed"][dir_path]["files"] += 1
            self.stats["directories_processed"][dir_path]["bytes"] += file_size
            return True
        except Exception as e:
            logging.error(f"Error removing {file_path}: {e}")
            return False

    def process_file_group(
            self, file_group: List[str]
    ) -> List[Tuple[str, int, Union[datetime, str]]]:
        """Process a group of files with the same base name pattern."""
        # Skip singleton groups
        if len(file_group) <= 1:
            return []

        # Sort by file size (ascending) for more efficient comparison
        file_group.sort(key=lambda x: os.path.getsize(x))

        # Group files with same size
        size_groups: DefaultDict[int, List[Tuple[str, int, Union[datetime, str]]]] = defaultdict(
            list
        )
        for file_path in file_group:
            size = os.path.getsize(file_path)
            date = self.extract_date_from_filename(file_path)
            size_groups[size].append((file_path, size, date))

        duplicates = []

        # First pass: find exact duplicates by CRC
        for size, files in size_groups.items():
            if len(files) > 1:
                # Group identical files
                identical_groups = []
                processed: Set[str] = set()

                for i, (file1, _, date1) in enumerate(files):
                    if file1 in processed:
                        continue

                    identical_files = [(file1, size, date1)]
                    processed.add(file1)

                    for j in range(i + 1, len(files)):
                        file2, _, date2 = files[j]
                        if file2 not in processed:
                            # Verify if files are identical and call verification if they are
                            are_identical = self.are_zips_identical(file1, file2)
                            if are_identical:
                                # self.verify_identical_files(file1, file2)
                                identical_files.append((file2, size, date2))
                                processed.add(file2)

                    if len(identical_files) > 1:
                        # Sort by date, keep oldest
                        identical_files.sort(key=lambda x: x[2])
                        identical_groups.append(identical_files)

                # Mark duplicates for deletion, keeping oldest
                for group in identical_groups:
                    # Keep the oldest file (first in the sorted list)
                    to_delete = group[1:]
                    duplicates.extend(to_delete)

        # Smart retention if enabled
        if (
                self.args.strategy == "smart"
                and len(file_group) > self.args.max_versions
        ):
            # Get all unique files (excluding duplicates)
            unique_files = []
            duplicate_paths = {d[0] for d in duplicates}

            for size, files in size_groups.items():
                for file_info in files:
                    if file_info[0] not in duplicate_paths:
                        unique_files.append(file_info)

            # Apply smart retention to unique files
            smart_deletions = self.apply_smart_retention(unique_files)
            duplicates.extend(smart_deletions)

        return duplicates

    def process_directory(self, directory: str) -> None:
        """Scan directory and process ZIP files for deduplication.

        Args:
            directory: Directory path to scan
        """
        logging.info(f"Scanning directory: {directory}")

        # Process files directory by directory
        if self.args.recursive:
            # For recursive mode, process each directory separately
            for root, _, files in os.walk(directory):
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
            else:
                logging.warning(f"No ZIP files found in {directory}")

    def _process_directory_files(self, zip_files: List[str], directory_path: str) -> None:
        """Process a list of ZIP files within a single directory.

        Args:
            zip_files: List of ZIP file paths to process
            directory_path: The directory these files belong to (for logging)
        """
        if not zip_files:
            return

        logging.info(f"Found {len(zip_files)} ZIP files to analyze in {directory_path}")

        # Group files by base name pattern
        file_groups: DefaultDict[str, List[str]] = defaultdict(list)
        for file_path in zip_files:
            base_name = self.get_base_filename(file_path)
            file_groups[base_name].append(file_path)

        # Process each group with multiple files using thread pool
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.threads
        ) as executor:
            future_to_group = {}

            for base_name, group in file_groups.items():
                if len(group) > 1:
                    future = executor.submit(self.process_file_group, group)
                    future_to_group[future] = base_name

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

    def verify_identical_files(self, file1: str, file2: str) -> bool:
        """Debug utility to verify and report when files are identical.

        Checks multiple comparison methods and only reports when files
        are confirmed identical by any method.

        Args:
            file1: First file path
            file2: Second file path

        Returns:
            bool: True if files are identical by any method, False otherwise
        """
        # Track which methods confirm identity
        identical_methods = []

        # Check file size
        same_size = os.path.getsize(file1) == os.path.getsize(file2)
        if not same_size:
            return False  # If sizes differ, they can't be identical
        identical_methods.append("file size")

        # Check CRC
        try:
            crc_match = self.compare_zips_by_crc(file1, file2)
            if crc_match:
                identical_methods.append("CRC values")
        except Exception:
            crc_match = False

        # Check content hash
        try:
            content_match = self.fallback_compare_zips_by_hash(file1, file2)
            if content_match:
                identical_methods.append("content hashes")
        except Exception:
            content_match = False

        # Check whole file hash
        try:
            with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
                h1 = hashlib.sha256(f1.read()).hexdigest()
                h2 = hashlib.sha256(f2.read()).hexdigest()
                whole_file_match = h1 == h2
                if whole_file_match:
                    identical_methods.append("whole file SHA256")
        except Exception:
            whole_file_match = False

        # Only report if at least one verification method confirms identity
        # beyond just file size
        if len(identical_methods) > 1:
            print(f"\n🔍 IDENTICAL FILES DETECTED:")
            print(f"File 1: {file1} ({self.format_size(os.path.getsize(file1))})")
            print(f"File 2: {file2} ({self.format_size(os.path.getsize(file2))})")
            print(f"Verified identical by: {', '.join(identical_methods)}")

            # Additional details if files matched by whole file hash but not by CRC/content
            if whole_file_match and not (crc_match or content_match):
                print("⚠️ Note: Files have identical content but different internal ZIP structure")

                # If we have whole file hashes, show them
                if 'h1' in locals() and 'h2' in locals():
                    print(f"  SHA256: {h1}")
            return True
        return False

    def print_statistics(self) -> None:
        """Print detailed statistics about the deduplication process."""
        if self.stats["total_deleted"] == 0:
            print("\nNo files were deleted during this run.")
            return

        print("\nDeduplication Statistics:")
        print(f"  Total files deleted: {self.stats['total_deleted']}")
        print(
            f"  Total space saved: {self.format_size(self.stats['total_bytes_saved'])}"
        )

        if self.stats["directories_processed"]:
            print("\nBreakdown by directory:")
            for directory, dir_stats in sorted(
                self.stats["directories_processed"].items()
            ):
                if dir_stats["files"] > 0:
                    print(f"  {directory}:")
                    print(f"    Files deleted: {dir_stats['files']}")
                    print(f"    Space saved: {self.format_size(dir_stats['bytes'])}")

    def print_startup_info(self) -> None:
        """Display startup information about the program configuration."""
        total_cores = multiprocessing.cpu_count()
        print(f"ZIP File Deduplication Tool")
        print(f"Using {self.args.threads} threads out of {total_cores} available cores")
        print(f"Scanning directory: {self.args.directory}")
        print(f"Strategy: {self.args.strategy}")
        if self.args.dry_run:
            print("DRY RUN MODE: No files will be deleted")
        if self.args.backup_dir:
            print(f"Backup mode: Files will be moved to {self.args.backup_dir}")

    def run(self) -> None:
        """Run the deduplication process."""
        # Parse arguments
        self.args = self.parse_arguments()

        # Set up logging
        self.setup_logging()

        # Set up signal handler for clean interruption
        signal.signal(signal.SIGINT, self.signal_handler)

        # Display startup information
        self.print_startup_info()

        # Create backup directory if specified
        if self.args.backup_dir:
            os.makedirs(self.args.backup_dir, exist_ok=True)

        # Process the directory
        start_time = time.time()

        try:
            self.process_directory(self.args.directory)
        except Exception as e:
            logging.error(f"Error: {e}")
        finally:
            # Always print statistics
            elapsed_time = time.time() - start_time
            print(f"\nProcessing completed in {elapsed_time:.2f} seconds")
            self.print_statistics()


def main() -> None:
    """Main program entry point."""
    deduplicator = ZipDeduplicator()
    deduplicator.run()


if __name__ == "__main__":
    main()
