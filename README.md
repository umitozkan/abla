# Emine’nin Mutfağı

FastAPI, sunucuda oluşturulan Türkçe HTML ve SQLite ile çalışan, telefon odaklı küçük işletme günlüğü. Ayrı frontend derlemesi yoktur. Saatler `Europe/Istanbul`, tutarlar TL olarak gösterilir; hesaplarda kuruş kullanılır. Container düzeni için [FastAPI Docker belgesi](https://fastapi.tiangolo.com/deployment/docker/), form ve yükleme sınırları için [Starlette istek belgesi](https://www.starlette.io/requests/) temel alınmıştır.

Kaynak depo: [umitozkan/abla](https://github.com/umitozkan/abla). Hedef yayın: [abla.umitozkan.com.tr](https://abla.umitozkan.com.tr), sunucu: `207.180.223.77`.

## Yerelde çalıştırma

Docker Engine/Desktop ve Docker Compose eklentisi gereklidir.

```sh
test -f .env || cp .env.example .env
chmod 600 .env
openssl rand -hex 32
openssl rand -base64 24
openssl rand -base64 24
```

Üç çıktıyı sırasıyla `.env` içindeki `SECRET_KEY`, `ADMIN_PASSWORD`, `EMINE_PASSWORD` alanlarına yazın. Birbirinden farklı ve en az 12 karakterlik şifreler kullanın. `.env` dosyasını kaynak kontrolüne eklemeyin. `ADMIN_USERNAME` ve `EMINE_USERNAME` farklı olmalı; örnekteki kullanıcı adlarını değiştirebilirsiniz.

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 app
```

[http://localhost:8080](http://localhost:8080) adresini açın. Yerelde `APP_ENV=development`, `COOKIE_SECURE=false` kullanılır. Servis yalnızca sunucunun `127.0.0.1` arayüzüne açılır. İlk açılışta boş veritabanına `.env` bilgileriyle bir yönetici ve bir Emine hesabı eklenir; sonraki açılışlarda hesaplar korunur. `.env` şifrelerini değiştirmek mevcut hesapların şifrelerini değiştirmez. Bu MVP’de hesap yönetimi ekranı yoktur.

Yönetici ilk girişte ayarlardan saatlik emek bedelini, ocak ve fırının **tahmini saatlik enerji giderini** girer. Bilinmeyen bedelleri boş bırakın; gerçekten gider yoksa `0` girin. Aylık sabit giderleri ilgili ay için ekleyin. Emine aynı giriş sayfasından kendi hesabıyla günlük kayıt ekranına ulaşır. İlk kurulumda örnek mali veri otomatik eklenmez.

SQLite (`/data/emine.sqlite3`) ve fişler (`/data/receipts`) `emine_data` adlı Docker volume içinde saklanır. `docker compose down` veriyi silmez; **`docker compose down -v` kalıcı veriyi siler.** Uygulama container içinde root olmayan kullanıcıyla çalışır.

## Günlük kullanım ve hesaplar

Emine gün seçer, menüyü ve istenen/yapılan/teslim edilen porsiyonları girer. İş, ocak ve fırın başlat/durdur düğmeleri saatleri kaydeder; süreler sonradan düzeltilebilir. Emek hesabında iş başlangıç/bitiş süreleri veya alışveriş, hazırlık, teslimat, temizlik sürelerinin toplamı seçilir; ikisi birlikte sayılmaz. Her güne birden fazla alışveriş, diğer gider, tahsilat ve kamera/galeriden fiş eklenebilir. Ekipman ayrı gider türüdür.

Yönetici tarih aralığı raporunda günlük ve haftalık değerleri, eksikleri, fişleri ve değişiklik geçmişini görür; CSV indirir. Gün kaydına bağlı tahsilatın ayrıca gerçek ödeme tarihi vardır: eski satışın bugünkü ödemesi bugünkü kasaya girer, bugünkü satış gelirine eklenmez. Senaryo aracı porsiyon/fiyat değiştiğinde satış ve tahmini sonucu karşılaştırır; kaydı değiştirmez ve mevcut toplam giderlerin sabit kaldığını varsayar.

- **Satış geliri:** o gün yapılan satış için girilen tutar. **Tahsilat:** ödeme tarihi rapor aralığında olan gerçek girişler. **Alacak:** seçili günlerin satış tutarından, rapor bitiş tarihine kadar bu satışlara bağlanan tahsilatlar çıkarılır. Fazla tahsilat ayrı toplanır; bir günün fazla ödemesi başka günün alacağını kapatmaz. Bu alan tüm geçmiş işletme alacaklarının toplamı değildir.
- **Tahmini tüketilen malzeme:** alışveriş + önceki günlerden kullanılan malzeme tutarı − sonraya kalan malzeme tutarı. Stok alanları miktar bazında otomatik envanter oluşturmaz.
- **Emek ve diğer giderler öncesi alışveriş farkı:** satış − malzeme alışverişi. Örnek: 15 porsiyon, 4.000 TL satış, 2.000 TL alışveriş için 2.000 TL. Bu tutar **net kâr değildir**; stok aktarımı da uygulanmamıştır. Tüketilen malzeme düşüldükten sonraki ara tutar ayrıca hesaplanır.
- **Tahmini enerji:** ocak saati × ocak saatlik gideri + fırın saati × fırın saatlik gideri. Sayaç verisi bulunmadığından tüketim ve enerji gideri tahmindir.
- **Sabit giderin günlük payı:** her aylık sabit gider / o ayın takvim günü sayısı. Bölümden kalan kuruşlar ayın ilk günlerine dağıtılır; ay toplamı tam korunur. Çalışılmayan günler de pay alır; tarih aralığı raporu aralıktaki tüm günlerin payını kapsar.
- **Emek bedeli hariç kalan:** satış − tüketilen malzeme − diğer işletme giderleri − tahmini enerji − sabit gider payı.
- **Emek bedeli dâhil tahmini sonuç:** emek hariç kalan − emek saati × saatlik emek bedeli. Ekipman alımı bu sonuca otomatik eklenmez; ayrıca gösterilir.
- **Tahmini kâr marjı:** emek dâhil sonuç / satış × 100. Porsiyon başı satış, maliyet ve sonuçta teslim edilen porsiyon sayısı kullanılır; sıfır veya eksik paydada sonuç hesaplanmaz.

Eksik stok tutarları, süreler, oranlar veya tamamlanmamış kontroller ekranda belirtilir. Bilinmeyen giderler geçici olarak sıfır kabul edilir; eksik satışlı günün kendi sonucu gösterilmez. Aralık/hafta toplamlarında bilinen satışlar ve tüm bilinen giderler kullanılır; eksik satış geçici sıfırdır. Malzeme bakiyesi tutarsızsa günün ve ilgili toplamın maliyet/kâr sonucu gösterilmez. Eksik veriyle görünen ara değerler kesin net kâr değildir. Yönetici oranları değiştirince geçmiş raporlar da güncel oranlarla yeniden hesaplanır; oran değişiklikleri ve kayıt düzeltmeleri eski/yeni değer ve değiştiren kullanıcıyla denetim kaydına yazılır. Bu MVP dönem kapanışı, bordro, vergi hesabı veya resmi muhasebe sistemi içermez.

## Testler

```sh
docker compose exec -T app pytest -q -p no:cacheprovider
```

Hesaplama testleri satış/tahsilat ayrımı, malzeme aktarımı, emek, enerji, sabit gider dağıtımı ve porsiyon hesaplarını denetler. Uygulama testleri giriş/yetki, CSRF, kayıt akışı ve güvenli fiş erişimini kapsar. Testler geçici veri dizininde çalışır.

## Yedekleme ve geri yükleme

Canlı yedek sırasında uygulamadaki yazmalar kısa süre bekletilir; SQLite anlık görüntüsü ile fişler aynı arşive alınır. Yedeği volume dışında tutun. Arşiv parola hash’lerini, işletme bilgilerini ve fişleri içerir; yalnızca yetkili kişilerin eriştiği bir konuma kopyalayın.

```sh
mkdir -p backups
chmod 700 backups
umask 077
docker compose exec -T app python scripts/backup.py > backups/emine-backup.tar.gz
```

Komutun başarılı çıkış kodunu kontrol edin; başarısız veya yarıda kalan dosyayı kullanılabilir yedek saymayın. Farklı yedekler için dosya adına tarih ekleyin. Geri yükleme mevcut veriyi değiştirir; önce güncel yedek alın, sonra uygulamayı durdurun:

```sh
docker compose stop app
docker compose run --rm -T --no-deps --entrypoint python app scripts/restore.py --confirm < backups/emine-backup.tar.gz
docker compose up -d app
docker compose ps
```

Geri yükleme tar yollarını, dosya türlerini, SQLite bütünlüğünü, uygulama tablolarını ve fiş referanslarını doğrular. Eski dosyaları `/data/pre-restore-*` altında saklar; doğrulama sonrası gereksiz kopyaları yönetici temizleyebilir. Kabul edilen arşiv ve açılmış içerik boyutu en fazla 10 GB’dır. Geri yüklenen hesap şifreleri yedeğe aittir; `.env` değerleriyle üzerlerine yazılmaz. Sunucu dışındaki bir kopya ve düzenli geri yükleme denemesi önerilir.

## 207.180.223.77 sunucusunda yayın

Aşağıdaki komutlar sunucuda `root` olarak, mevcut Nginx'in host üzerinde çalıştığı Ubuntu/Debian düzeni için hazırlanmıştır. Docker/Compose zaten mevcut; komutlarda servis sağlığını beklemek için güncel Compose v2/v5 `--wait` desteği kullanılır. Yeni uygulama `127.0.0.1:18082` kullanır; paylaşılan servislerin `18000`, `18001`, `18080`, `3000`, `3002` portlarıyla çakışmaz. Compose proje adı `abla`, kalıcı volume `abla_emine_data` olur. Yerel geliştirme `8080` portunda kalır. Proje adı/aşağıdaki `.env` dosyası sonradan değiştirilmemelidir; farklı proje adı farklı volume açar. [Compose proje adı belgesi](https://docs.docker.com/compose/how-tos/project-name/).

### 1. DNS

Alan adının DNS panelinde **A / `abla` / `207.180.223.77`** kaydını ekleyin. IPv6 kullanılmayacaksa `abla` için AAAA eklemeyin. Cloudflare varsa ilk sertifika kurulumu için DNS only kullanın. Sunucu güvenlik duvarında 80/443 erişilebilir olmalı; 18082 internete açılmaz.

### 2. Projeyi kurun

Sunucudaki mevcut proje dizini `/root/abla` olarak kabul edilir. Repo zaten klonlandıysa yeniden klonlamayın; aşağıdaki komutlar mevcut dizinden devam eder. Tamamen yeni bir sunucuda önce `git clone https://github.com/umitozkan/abla.git /root/abla` çalıştırılabilir.

```sh
cd /root/abla
git pull --ff-only origin main
test -e .env || python3 scripts/setup_env.py --production
docker compose config -q
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
curl --fail http://127.0.0.1:18082/health
```

Mevcut `.env` korunur. `setup_env.py` yeni `.env` dosyasını 600 izniyle ve üç ayrı rastgele sırla oluşturur. Var olan dosyanın üzerine yazmaz. Üretim ayarları otomatik gelir: `COMPOSE_PROJECT_NAME=abla`, `APP_PORT=18082`, `APP_ENV=production`, `COOKIE_SECURE=true`, `ALLOWED_HOSTS=abla.umitozkan.com.tr,localhost,127.0.0.1`. Alanların açıklaması `.env.production.example` içindedir. Sunucuda Python3/Git yoksa dağıtımın paket yöneticisiyle kurun.

İlk giriş kullanıcı adları `yonetici` ve `emine`; şifreleri yalnız kendi sunucu terminalinizde `/root/abla/.env` dosyasından okuyun ve parola yöneticinize kaydedin. Yerel `.env`, fişler, veritabanı ve TEST kayıtları GitHub'a gönderilmez. Yeni sunucu boş bir veritabanı ile başlar. `.env` şifresini sonradan değiştirmek mevcut hesaba uygulanmaz.

### 3. HTTP doğrulama adresini açın

`nginx -t` başarılı olmalı ve 18082 başka bir servis tarafından kullanılmamalı. Herhangi bir komut hata verirse sonraki aşamaya geçmeyin. Aşağıdaki alt kabuk yalnız yeni `abla` site dosyasını oluşturur; mevcut aynı adlı dosya varsa durur.

```sh
cd /root/abla
(
  set -eu
  command -v nginx
  nginx -t
  if nginx -T 2>&1 | grep -Fq 'abla.umitozkan.com.tr'; then
    echo 'Bu alan adı Nginx yapılandırmasında zaten var; mevcut bloğu kontrol edin.' >&2
    exit 1
  fi
  test -d /etc/nginx/sites-available
  test -d /etc/nginx/sites-enabled
  test ! -e /etc/nginx/sites-available/abla
  test ! -L /etc/nginx/sites-available/abla
  test ! -e /etc/nginx/sites-enabled/abla
  test ! -L /etc/nginx/sites-enabled/abla
  install -d -m 755 /var/www/letsencrypt
  install -m 644 deploy/nginx-http.conf /etc/nginx/sites-available/abla
  ln -s /etc/nginx/sites-available/abla /etc/nginx/sites-enabled/abla
  if nginx -t; then
    systemctl reload nginx
  else
    rm /etc/nginx/sites-enabled/abla /etc/nginx/sites-available/abla
    exit 1
  fi
)
```

Sertifika öncesi bu site yalnız ACME doğrulama dosyalarını sunar, diğer istekler 503 alır. Uygulamaya HTTPS hazır olduğunda giriş yapılır. Aynı alan adına ait başka bir Nginx bloğu zaten varsa yeni blok eklemek yerine mevcut bloğu uyarlayın.

### 4. Sertifikayı alın ve HTTPS'i etkinleştirin

DNS'in hedef sunucuya yönlendiğinden emin olun. Certbot zaten kuruluysa paket kurulumunu atlayın; kurulu değilse Ubuntu/Debian için `apt-get update && apt-get install -y certbot` çalıştırın. Certbot ilk çalıştırmada e-posta ve hizmet koşulları hakkında sorular sorabilir.

```sh
cd /root/abla
certbot certonly --webroot -w /var/www/letsencrypt \
  --cert-name abla.umitozkan.com.tr -d abla.umitozkan.com.tr \
  --deploy-hook 'nginx -t && systemctl reload nginx'
```

Sertifika başarıyla oluşturulduktan sonra:

```sh
cd /root/abla
(
  set -eu
  test -s /etc/letsencrypt/live/abla.umitozkan.com.tr/fullchain.pem
  test -s /etc/letsencrypt/live/abla.umitozkan.com.tr/privkey.pem
  cp /etc/nginx/sites-available/abla /etc/nginx/sites-available/abla.http-backup
  install -m 644 deploy/nginx.conf /etc/nginx/sites-available/abla
  if nginx -t; then
    systemctl reload nginx
  else
    cp /etc/nginx/sites-available/abla.http-backup /etc/nginx/sites-available/abla
    exit 1
  fi
)
curl --fail https://abla.umitozkan.com.tr/health
certbot renew --cert-name abla.umitozkan.com.tr --dry-run
```

[Certbot webroot yöntemi](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot) mevcut Nginx çalışırken sertifika alır. Güncel sertifika yenilendiğinde kayıtlı deploy hook Nginx'i test edip yeniden yükler. Paket kurulumunda `certbot.timer`, Snap kurulumunda ilgili Snap zamanlayıcısının etkin olduğunu kontrol edin. Uygulamayı [HTTPS adresinden](https://abla.umitozkan.com.tr) açıp giriş, günlük kayıt ve fiş yüklemeyi doğrulayın; yönetici maliyet ayarlarını doldurun.

Nginx örneği gerçek istemci IP'sini iletir, dışarıdan gelen `X-Forwarded-For` başlığının üzerine yazar. Uvicorn yalnız localhost'a yayınlanan portun arkasındaki bu proxy düzeni için yönlendirme başlıklarına güvenir. Nginx de container içindeyse bu localhost upstream çalışmaz; özel Docker ağı ve proxy erişimi ayrıca uyarlanmalıdır. Fiş klasörü için açık Nginx `alias` oluşturmayın. [Nginx proxy belgesi](https://nginx.org/en/docs/http/ngx_http_proxy_module.html), [Nginx HTTPS belgesi](https://nginx.org/en/docs/http/configuring_https_servers.html).

### Güncelleme

Önce yukarıdaki yedekleme komutuyla yedek alın. Aynı dizinde ve aynı `.env` ile:

```sh
cd /root/abla
git pull --ff-only origin main
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
curl --fail https://abla.umitozkan.com.tr/health
```

Bu komutlar `abla` Compose projesini günceller. Volume silme (`down -v`) veya genel Docker temizleme komutu kullanmayın. HTTPS Nginx dosyası değişirse ayrıca kontrollü olarak kopyalayıp `nginx -t && systemctl reload nginx` uygulayın.

Geri yükleme tüm oturumları iptal eder; kullanıcılar yeniden giriş yapar. Docker imajı test edilmiş sürümleri `requirements.lock` dosyasından kurar; doğrudan bağımlılık aralıkları `requirements.txt` içindedir.
