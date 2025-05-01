# ZIP File Deduplication Program 🧹

> ⚠️ **Always test with `--dry-run` first!** Deduplication is irreversible without backups. There is a backup option you can use as a temporary directory/subdirectories to await manual deletion.
> 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**Advanced ZIP File Deduplication Tool** that identifies and removes duplicate ZIP files based on content, not just filenames or sizes. Perfect for cleaning up backup directories, versioned archives, and automated ZIP exports.

**Rationale:** I maintain a GitHub repository mirroring system to create local copies of repos important to my work and interests. While the mirroring utility avoids re-downloading unchanged repositories, certain metadata fields (like timestamps or commit history) can still change in some repositories even when the actual source content remains identical. This creates unnecessary duplicates that bloat storage and complicate management.

This deduplication tool solves this problem by analyzing my local mirror files rather than relying on metadata. It provides a robust set of methods to:


- Identify exact duplicates through content hashing

- Apply smart retention policies for versioned archives

- Clean up redundant repositories while preserving meaningful changes

The result: a streamlined local repository collection that stays focused on actual content changes rather than metadata noise.


---

## 🔍 Key Features

- **Content-Based Comparison**  
  Uses CRC checks, file content hashing, and whole-file verification to detect *true duplicates*.
  
- **Smart Retention Policies**  
  Keep meaningful versions while removing intermediate changes:
  - Size-based thresholds (e.g., keep files that grow >5%)
  - Time intervals (e.g., retain versions at least 7 days apart)
  - Max version limits (e.g., keep only 5 versions)

- **Parallel Processing**  
  Leverages multi-core CPUs for fast scanning (configurable thread count).

- **Safety First**  
  - `--dry-run` mode to preview deletions
  - `--backup-dir` to archive duplicates instead of deleting them

---

## 🌐 Cross-Platform Compatibility

This tool works seamlessly across all major operating systems:

### ✅ Compatibility Summary

| Feature                | Windows | Linux | macOS |
|------------------------|--------|-------|-------|
| Runs natively          | ✅     | ✅    | ✅    |
| File deletion          | ✅     | ✅    | ✅    |
| Recursive scanning     | ✅     | ✅    | ✅    |
| Smart retention        | ✅     | ✅    | ✅    |
| Backup directory       | ✅     | ✅    | ✅    |
| Dry-run mode           | ✅     | ✅    | ✅    |

### 🪟 Windows-Specific Notes

1. **Shebang Line**  
   The `#!/usr/bin/env python3` shebang is ignored on Windows. Run via:  
   ```powershell
   python zipdeduplication.py C:\path\to\zip\files
   ```

2. **File Locking**  
   Windows blocks deletion of in-use files (same behavior as Linux/macOS).

3. **Path Length Limits**  
   Use `\\?\` prefixes for paths >260 characters on Windows.

4. **Case Sensitivity**  
   Windows treats `File.zip` and `file.zip` as the same file.

---

## 🚀 Installation

```bash
# Clone the repository
git clone https://github.com/StephenGenusa/zip-deduplication.git
cd zip-deduplication
```

*No external dependencies required – works with Python 3.6+ standard libraries.*

> **Windows Note**: Run via `python zipdeduplication.py` instead of `python3`.

---

## 🛠️ Usage Examples

### Basic Deduplication

**Linux/macOS**:
```bash
python3 zipdeduplication.py /path/to/zip/files
```

**Windows (PowerShell)**:
```powershell
python zipdeduplication.py C:\path\to\zip\files
```

### Recursive Scan with Smart Retention

**Linux/macOS**:
```bash
python3 zipdeduplication.py /path/to/zip/files \
  --recursive \
  --strategy smart \
  --growth-threshold 10 \
  --keep-interval 14 \
  --max-versions 3
```

**Windows (PowerShell)**:
```powershell
python zipdeduplication.py C:\path\to\zip\files `
  --recursive `
  --strategy smart `
  --growth-threshold 10 `
  --keep-interval 14 `
  --max-versions 3
```

### Safety-First Dry Run

**Linux/macOS**:
```bash
python3 zipdeduplication.py /path/to/zip/files \
  --dry-run \
  --backup-dir /path/to/backup
```

**Windows (PowerShell)**:
```powershell
python zipdeduplication.py C:\path\to\zip\files `
  --dry-run `
  --backup-dir C:\path\to\backup
```

---

## 📋 Command-Line Options

| Flag              | Description                                                                 |
|-------------------|-----------------------------------------------------------------------------|
| `--threads`       | Number of CPU threads to use (default: half your CPU cores)                 |
| `--recursive`     | Scan subdirectories recursively                                             |
| `--strategy`      | Deduplication mode: `exact` (strict) or `smart` (version-aware)             |
| `--growth-threshold` | Size increase % to trigger retention (default: 5%)                        |
| `--keep-interval` | Minimum days between retained versions (default: 7)                         |
| `--max-versions`  | Maximum versions to keep per file group (default: 5)                        |
| `--dry-run`       | Simulate deletions without modifying files                                  |
| `--backup-dir`    | Move duplicates here instead of deleting them                               |

---

## 🧠 How It Works

1. **Group Files**  
   Clusters ZIPs by base filename (ignoring timestamps).
2. **Compare Content**  
   Uses CRC checks, file-by-file hashing, and whole-file SHA256 as fallbacks.
3. **Apply Retention**  
   Keeps oldest file, newest file, and versions with significant changes.

---

## 🛡️ Safety Features

- **Dry Run Mode**  
  Preview all actions with `--dry-run` before execution.
  
- **Backup Option**  
  Use `--backup-dir` to preserve duplicates in a structured archive.

- **Atomic Operations**  
  Files are only deleted/moved after successful verification.

---

## 📦 Project Structure

```
zip-deduplication/
├── zipdeduplication.py    # Main script
├── README.md              # This file
└── LICENSE                # MIT License
```

---

## 🤝 Contributing

1. Fork the repo  
2. Create a feature branch (`git checkout -b feature/amazing-thing`)  
3. Submit a PR with detailed changes  
4. Follow the [Code of Conduct](CODE_OF_CONDUCT.md)

---

## 📄 License

MIT © [Stephen Genusa](https://github.com/StephenGenusa)  
See [LICENSE](LICENSE) for details.

---

**[Open an issue](https://github.com/StephenGenusa/zip-deduplication/issues).  
**Found a bug?** [Report it here](https://github.com/StephenGenusa/zip-deduplication/issues/new).  
