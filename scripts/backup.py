"""Stream a consistent SQLite + receipt tar.gz to stdout; diagnostics use stderr."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile


def backup() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    database = data_dir / "emine.sqlite3"
    receipts = data_dir / "receipts"
    if database.is_symlink() or receipts.is_symlink() or not database.is_file() or not receipts.is_dir():
        raise ValueError("Veritabanı veya fiş klasörü bulunamadı.")
    # Application receipt mutations must use the same SQLite write transaction.
    lock = sqlite3.connect(database, timeout=60)
    try:
        lock.execute("BEGIN IMMEDIATE")
        receipt_paths = sorted(receipts.rglob("*"))
        for path in receipt_paths:
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Fiş klasöründe desteklenmeyen dosya türü var.")
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=data_dir) as temp_dir:
            snapshot = Path(temp_dir) / "emine.sqlite3"
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
                with sqlite3.connect(snapshot) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Veritabanı bütünlük kontrolü başarısız.")
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
                archive.add(snapshot, arcname="emine.sqlite3", recursive=False)
                archive.add(receipts, arcname="receipts", recursive=False)
                for path in receipt_paths:
                    archive.add(path, arcname=path.relative_to(data_dir).as_posix(), recursive=False)
        print("Veritabanı ve fiş yedeği tamamlandı.", file=sys.stderr)
    finally:
        lock.rollback()
        lock.close()


if __name__ == "__main__":
    try:
        backup()
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        print(f"Yedekleme başarısız: {exc}", file=sys.stderr)
        sys.exit(1)
