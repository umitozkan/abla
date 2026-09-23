#!/usr/bin/env python3
"""İlk kurulum için rastgele parolalarla özel bir .env dosyası oluşturur."""

import argparse
import os
from pathlib import Path
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production",
        action="store_true",
        help="abla.umitozkan.com.tr için HTTPS ve 18082 portunu kullanır.",
    )
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()

    settings = {
        "COMPOSE_PROJECT_NAME": "abla" if args.production else "emine",
        "APP_ENV": "production" if args.production else "development",
        "APP_PORT": "18082" if args.production else "8080",
        "ALLOWED_HOSTS": (
            "abla.umitozkan.com.tr,localhost,127.0.0.1"
            if args.production
            else "localhost,127.0.0.1,abla.umitozkan.com.tr"
        ),
        "COOKIE_SECURE": "true" if args.production else "false",
        "SECRET_KEY": secrets.token_hex(32),
        "ADMIN_USERNAME": "yonetici",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "EMINE_USERNAME": "emine",
        "EMINE_PASSWORD": secrets.token_urlsafe(24),
    }
    content = (
        "# Gizli kurulum bilgileri. Git'e eklemeyin veya paylaşmayın.\n"
        "# Parolalar sadece boş veritabanında ilk kullanıcıları oluşturur.\n"
        + "".join(f"{key}={value}\n" for key, value in settings.items())
    )
    try:
        # O_EXCL var olan dosyaları ve sembolik bağlantıları değiştirmeyi engeller.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.exit(1, f"Dosya zaten var; değiştirilmedi: {args.output}\n")
    except OSError as exc:
        parser.exit(1, f"Kurulum dosyası oluşturulamadı: {exc}\n")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(content)

    print(f"Kurulum dosyası oluşturuldu: {args.output.absolute()} (izin: 600)")
    print("Kullanıcılar: yonetici ve emine.")
    print("Parolaları bu dosyayı kendi terminalinizde veya editörünüzde açarak okuyun.")
    print("Dosya mevcut kullanıcıların parolalarını değiştirmez.")


if __name__ == "__main__":
    main()
