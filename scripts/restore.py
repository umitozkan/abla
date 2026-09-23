"""Restore a backup from stdin while the application service is stopped."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid

MAX_BYTES = 10 * 1024 * 1024 * 1024
MAX_FILES = 100_000


def validate_and_extract(archive_path: Path, staging: Path) -> None:
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = PurePosixPath(name).parts
            if (not name or name.startswith("/") or "\\" in name or ".." in parts
                    or "." in parts or name in seen or len(seen) >= MAX_FILES
                    or (name != "emine.sqlite3" and not (parts and parts[0] == "receipts"))
                    or not (member.isdir() or member.isfile())):
                raise ValueError("Yedekte güvenli olmayan veya beklenmeyen dosya yolu var.")
            if name == "emine.sqlite3" and not member.isfile():
                raise ValueError("Yedek veritabanı normal bir dosya olmalı.")
            if name == "receipts" and not member.isdir():
                raise ValueError("Fiş dizini geçersiz.")
            seen.add(name)
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError("Yedek açılmış boyut sınırını (10 GB) aşıyor.")
            destination = staging.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Yedekte okunamayan dosya var.")
                with source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o600)
    if "emine.sqlite3" not in seen or "receipts" not in seen:
        raise ValueError("Yedekte veritabanı veya fiş klasörü eksik.")
    with sqlite3.connect(f"file:{staging / 'emine.sqlite3'}?mode=ro", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Yedek veritabanı bütünlük kontrolünden geçmedi.")
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"users", "sessions", "login_attempts", "days", "expenses", "payments",
                    "timers", "receipts", "settings", "fixed_costs", "audit"}
        if not required.issubset(tables):
            raise ValueError("Yedek, Emine’nin Mutfağı veritabanı şemasını içermiyor.")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Yedek veritabanında bozuk kayıt ilişkisi var.")
        for (filename,) in database.execute("SELECT filename FROM receipts"):
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or "\\" in filename or filename in {".", ".."}
                    or not (staging / "receipts" / filename).is_file()):
                raise ValueError("Fiş kaydına ait dosya eksik veya adı geçersiz.")


def restore() -> None:
    parser = argparse.ArgumentParser(description="Durdurulmuş uygulamaya stdin üzerinden yedek yükle.")
    parser.add_argument("--confirm", action="store_true", help="Uygulamanın durdurulduğunu onayla.")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("Önce docker compose stop app çalıştırın; sonra --confirm ekleyin.")
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as temp_dir:
        temp = Path(temp_dir)
        archive_path = temp / "backup.tar.gz"
        total = 0
        with archive_path.open("wb") as output:
            while chunk := sys.stdin.buffer.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("Yedek dosyası 10 GB sınırını aşıyor.")
                output.write(chunk)
        staging = temp / "content"
        staging.mkdir(mode=0o700)
        validate_and_extract(archive_path, staging)
        # Restoring an old snapshot must not resurrect a logged-out session.
        with sqlite3.connect(staging / "emine.sqlite3") as database:
            database.execute("DELETE FROM sessions")
            database.commit()
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        previous = data_dir / ("pre-restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        previous.mkdir(mode=0o700)
        preserved: list[str] = []
        installed: list[str] = []
        try:
            for name in ("emine.sqlite3", "emine.sqlite3-wal", "emine.sqlite3-shm", "receipts"):
                existing = data_dir / name
                if existing.exists():
                    existing.rename(previous / name)
                    preserved.append(name)
            for name in ("emine.sqlite3", "receipts"):
                (staging / name).rename(data_dir / name)
                installed.append(name)
        except BaseException:
            for name in reversed(installed):
                (data_dir / name).rename(staging / name)
            for name in reversed(preserved):
                (previous / name).rename(data_dir / name)
            raise
        print(f"Geri yükleme tamamlandı. Önceki veriler saklandı: {previous}", file=sys.stderr)


if __name__ == "__main__":
    try:
        restore()
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        print(f"Geri yükleme başarısız: {exc}", file=sys.stderr)
        sys.exit(1)
