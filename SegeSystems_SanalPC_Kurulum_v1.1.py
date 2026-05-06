"""
SegeSystems Sanal PC Kurulum Aracı v1
=====================================
www.segemacro.com

VMware Workstation üzerinde toplu Windows VM kurulum aracı.

Akış (özet)
-----------
1. Kullanıcı GUI'den ISO + RAM/CPU/disk + adet/isim girer.
2. Her VM için:
   - ~/Documents/Virtual Machines/<ad>/ klasörü açılır
   - <ad>.vmx config dosyası yazılır (donanım: RAM, CPU, disk, NAT, ISO)
   - <ad>.vmdk sanal disk vmware-vdiskmanager ile yaratılır
   - (varsa) iso.xml şablonundan <ad>.flp autounattend floppy üretilir
   - VMware penceresi açılır + vmrun start ile VM güç verilir
3. Windows ISO'dan boot eder; floppy'deki Autounattend.xml'i bulup
   soru sormadan kurulumu tamamlar.
4. "BYPASS ET" butonu kurulum sonrası anti-detection anahtarlarını
   .vmx dosyalarına enjekte eder, ISO'yu söker, boot'u HDD'ye çevirir.

Mimari
------
- PyQt5 main window (`SegeSystemsGUI`)
- Worker thread'ler kurulum/bypass işlerini yapar
- `WorkerSignals` Qt sinyalleriyle thread → UI güncellemesi
- Modül seviyesindeki `build_autounattend_floppy` saf stdlib ile
  1.44 MB FAT12 floppy imajı üretir (harici bağımlılık yok)

Bağımlılık: PyQt5
"""
# ── Standart kütüphane ─────────────────────────────────────────────
import sys                       # frozen tespiti, exit code, argv
import os                        # dosya/klasör yolları, makedirs
import re                        # autounattend.xml ComputerName regex
import subprocess                # vmrun.exe, vmware.exe, vmware-vdiskmanager
import threading                 # kurulum/bypass UI'yi bloklamasın
import time                      # adımlar arası kısa beklemeler
import webbrowser                # segemacro.com linkini aç
from datetime import datetime    # log timestamp'leri
from pathlib import Path         # script dizini için

# ── PyQt5 (tek harici bağımlılık) ──────────────────────────────────
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QProgressBar, QSlider,
    QFileDialog, QMessageBox, QButtonGroup, QFrame, QScrollArea, QSizePolicy,
    QStackedWidget, QComboBox, QSpinBox,
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QFontDatabase, QIcon


# ════════════════════════════════════════════════════════════════════
# UNATTENDED FLOPPY ÜRETİCİSİ (saf stdlib, 0 harici bağımlılık)
# ════════════════════════════════════════════════════════════════════
# Windows Setup boot ettiğinde takılı tüm removable sürücülerin kökünde
# "autounattend.xml" arar; bulursa kurulum sorularını otomatik yanıtlar.
# Bu blok 1.44 MB FAT12 floppy imajı (.flp) üretir; içinde tek dosya:
# Autounattend.xml. VMware'in floppy0 olarak iliştirdiği bu imajı Windows
# A: sürücüsü olarak görür ve otomatik tarar.
#
# FAT12 Floppy Düzeni (1.44 MB = 2880 sektör × 512 byte):
#   Sektör  0       : Boot sector + BPB (BIOS Parameter Block)
#   Sektör  1- 9    : FAT #1 (9 sektör)
#   Sektör 10-18    : FAT #2 (FAT #1'in birebir kopyası, yedek)
#   Sektör 19-32    : Root directory (14 sektör, 224 entry × 32 byte)
#   Sektör 33-2879  : Veri bölgesi (cluster 2'den başlar)
#
# Cluster numaralandırması cluster 2'den başlar (cluster 0 ve 1 rezerve).
# 1 cluster = 1 sektör = 512 byte (floppy'de standart).

_FLP_SECTOR = 512                  # Standart sektör boyutu
_FLP_TOTAL_SECTORS = 2880          # 2880 × 512 = 1.474.560 byte = 1.44 MB
_FLP_RESERVED = 1                  # Boot sector için ayrılan sektör sayısı
_FLP_NUM_FATS = 2                  # FAT tablosu sayısı (2'si standart)
_FLP_SECTORS_PER_FAT = 9           # Her FAT'in kapladığı sektör
_FLP_ROOT_ENTRIES = 224            # Root dir'de tutulabilen max dizin girişi
# Root dir'in kapladığı sektör (224 × 32 byte = 7168 byte = 14 sektör):
_FLP_ROOT_SECTORS = (_FLP_ROOT_ENTRIES * 32 + _FLP_SECTOR - 1) // _FLP_SECTOR
_FLP_FAT_BYTES = _FLP_SECTORS_PER_FAT * _FLP_SECTOR  # = 4608 byte/FAT

# Veri bölgesinin (cluster 2'nin) imaj içindeki başlangıç offset'i:
# (1 reserved + 18 FAT + 14 root) × 512 = 16896
_FLP_DATA_OFFSET = (
    _FLP_RESERVED + _FLP_NUM_FATS * _FLP_SECTORS_PER_FAT + _FLP_ROOT_SECTORS
) * _FLP_SECTOR


def _fat12_set(arr: bytearray, cluster: int, value: int) -> None:
    """Bir FAT12 girişine 12-bit değer yaz.

    FAT12'de her giriş 1.5 bayt (12 bit) yer kaplar; iki giriş birlikte
    3 baytı paylaşır. Bu yüzden çift indekslerde alt 12 bit, tek
    indekslerde üst 12 bit yazılır.

    Args:
        arr: FAT bayt dizisi (bytearray, in-place değişir)
        cluster: Yazılacak cluster numarası
        value: 12 bitlik değer (sonraki cluster numarası ya da 0xFFF=EOF)
    """
    # Her cluster ortalama 1.5 bayt → byte offset = cluster + cluster//2
    off = cluster + (cluster // 2)
    if cluster % 2 == 0:
        # Çift indeks: bu girişin tüm 8 alt biti `off`'a, üst 4 biti
        # `off+1`'in alt yarısına gider (sonraki tek girişin alt 4'ü
        # `off+1`'in üst yarısında olduğu için bunu korumak gerek)
        arr[off] = value & 0xFF
        arr[off + 1] = (arr[off + 1] & 0xF0) | ((value >> 8) & 0x0F)
    else:
        # Tek indeks: alt 4 bit `off`'un üst yarısına, üst 8 bit `off+1`'e
        arr[off] = (arr[off] & 0x0F) | ((value & 0x0F) << 4)
        arr[off + 1] = (value >> 4) & 0xFF


def _lfn_checksum(name11: bytes) -> int:
    """8.3 kısa adın LFN sağlamasını hesapla.

    LFN (Long File Name) girişlerinin her biri, bağlı oldukları kısa
    8.3 (SFN) girişin sağlamasını içermek zorundadır; eşleşmezse FAT
    sürücüleri uzun adı reddeder. Algoritma Microsoft FAT spec §7.2.

    Args:
        name11: 8.3 kısa adın 11 baytlık ham hali (8 base + 3 ext, dot çıkık)

    Returns:
        0..255 arası tek bayt sağlama değeri
    """
    s = 0
    for b in name11:
        # Düşük bitten döndürme + ekleme; FAT spec'inden birebir
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _make_lfn_entry(seq: int, is_last: bool, chunk: str, chk: int) -> bytes:
    """Tek bir 32 baytlık LFN (Long File Name) dizin girişi üret.

    LFN girişi 13 UCS-2 (UTF-16LE) karakter taşır; üç parçaya bölünür:
    name1 (5 char), name2 (6 char), name3 (2 char). Doldurulmamış slotlar
    0xFFFF padding ile doldurulur. LFN girişleri diskte uzun ad parçaları
    sondan başa sıralı yerleşir; ilk yazılan giriş 0x40 bit'i (LAST_LONG)
    ile işaretlenir.

    Args:
        seq: Sıra numarası (1'den başlar, ne kadar parça varsa o kadar)
        is_last: Diskte ilk yazılan parça mı? (en yüksek seq'li olan)
        chunk: 13 karakterlik metin parçası (eksikse padding)
        chk: Kısa adın checksum'ı (tüm LFN girişlerinde aynı olmalı)

    Returns:
        32 baytlık ham dizin girişi
    """
    e = bytearray(32)
    # Byte 0: sıra numarası + 0x40 (sadece son girişte)
    e[0] = seq | (0x40 if is_last else 0)
    # Byte 1-10: name1 (5 karakter × 2 byte UTF-16LE)
    for i in range(5):
        ch = chunk[i] if i < len(chunk) else '￿'  # U+FFFF = padding
        e[1 + i * 2:3 + i * 2] = ord(ch).to_bytes(2, 'little')
    e[11] = 0x0F  # Attribute byte: 0x0F = LFN sabiti (FAT bunu LFN olarak tanır)
    e[12] = 0     # Type byte: 0
    e[13] = chk   # Bağlı kısa adın checksum'ı
    # Byte 14-25: name2 (6 karakter)
    for i in range(6):
        j = 5 + i
        ch = chunk[j] if j < len(chunk) else '￿'
        e[14 + i * 2:16 + i * 2] = ord(ch).to_bytes(2, 'little')
    # Byte 26-27: 0 (cluster alanı, LFN'de kullanılmıyor)
    # Byte 28-31: name3 (2 karakter)
    for i in range(2):
        j = 11 + i
        ch = chunk[j] if j < len(chunk) else '￿'
        e[28 + i * 2:30 + i * 2] = ord(ch).to_bytes(2, 'little')
    return bytes(e)


def build_autounattend_floppy(xml_text: str) -> bytes:
    """1.44 MB FAT12 floppy imajı üret; içinde tek dosya: Autounattend.xml.

    Bu fonksiyon Windows Setup'ın boot esnasında bulup okuyabileceği,
    geçerli bir FAT12 dosya sistemi yazar. VMware'in floppy0 olarak
    iliştirdiği imaj guest tarafından A: sürücüsü olarak görülür.

    Args:
        xml_text: Autounattend.xml dosyasının içeriği (Microsoft unattend
                  şeması). Per-VM özelleştirme için ComputerName vs. çağıran
                  taraf zaten değiştirmiş olmalı.

    Returns:
        Tam 1.474.560 baytlık ham FAT12 imaj (1.44 MB). VMware bunu
        floppy0.fileName olarak doğrudan tüketir.
    """
    # Boş 1.44 MB imaj (sıfırla doldurulmuş)
    img = bytearray(_FLP_SECTOR * _FLP_TOTAL_SECTORS)

    # ── Sektör 0: Boot Sector + BPB ─────────────────────────────────
    # BPB (BIOS Parameter Block) FAT'in yapısını tanımlar; Windows ve diğer
    # FAT sürücüleri imajı tanımak için bu alanı okur.
    bs = bytearray(_FLP_SECTOR)
    bs[0:3]   = b'\xEB\x3C\x90'                              # JMP + NOP (boot kodu placeholder)
    bs[3:11]  = b'MSWIN4.1'                                  # OEM adı (8 karakter)
    bs[11:13] = _FLP_SECTOR.to_bytes(2, 'little')            # Bayt/sektör (512)
    bs[13]    = 1                                            # Sektör/cluster (1)
    bs[14:16] = _FLP_RESERVED.to_bytes(2, 'little')          # Rezerve sektör sayısı (1)
    bs[16]    = _FLP_NUM_FATS                                # FAT tablosu sayısı (2)
    bs[17:19] = _FLP_ROOT_ENTRIES.to_bytes(2, 'little')      # Root dir entry kapasitesi (224)
    bs[19:21] = _FLP_TOTAL_SECTORS.to_bytes(2, 'little')     # Toplam sektör (2880)
    bs[21]    = 0xF0                                         # Media tipi: 0xF0 = 1.44MB floppy
    bs[22:24] = _FLP_SECTORS_PER_FAT.to_bytes(2, 'little')   # Sektör/FAT (9)
    bs[24:26] = (18).to_bytes(2, 'little')                   # Sektör/track (geometri)
    bs[26:28] = (2).to_bytes(2, 'little')                    # Kafa sayısı (geometri)
    bs[36]    = 0x00                                         # BIOS sürücü no (floppy: 0x00)
    bs[38]    = 0x29                                         # Genişletilmiş boot signature
    bs[39:43] = b'\x12\x34\x56\x78'                          # Volume seri no (rastgele/sabit)
    bs[43:54] = b'AUTOUNAT   '                               # Volume label (11 byte, padded)
    bs[54:62] = b'FAT12   '                                  # FS tipi etiketi
    bs[510:512] = b'\x55\xAA'                                # Boot sector imzası (zorunlu)
    img[0:_FLP_SECTOR] = bs

    # ── FAT tablosu (cluster zinciri) ───────────────────────────────
    # Dosya verisi cluster 2'den başlayan bağlı liste şeklinde tutulur.
    # Her cluster sonraki cluster'ı işaret eder; son cluster 0xFFF (EOF).
    xml_bytes = xml_text.encode('utf-8')
    # Dosya kaç cluster yer kaplar? (1 cluster = 1 sektör = 512 byte)
    file_clusters = (len(xml_bytes) + _FLP_SECTOR - 1) // _FLP_SECTOR
    fat = bytearray(_FLP_FAT_BYTES)
    fat[0] = 0xF0  # Cluster 0 rezerve, media descriptor'u taşır
    fat[1] = 0xFF  # Cluster 1 rezerve (0xFFF = EOF işaretleyici)
    fat[2] = 0xFF
    # Cluster 2'den başlayarak zincirin halkalarını ata
    for i in range(file_clusters):
        cluster = 2 + i
        # Son cluster ise EOF (0xFFF), aksi halde sonraki cluster
        nxt = cluster + 1 if i < file_clusters - 1 else 0xFFF
        _fat12_set(fat, cluster, nxt)
    # Her iki FAT kopyasını da yaz (FAT #1 ve aynı içerikli FAT #2 yedek)
    fat_off = _FLP_RESERVED * _FLP_SECTOR
    img[fat_off:fat_off + _FLP_FAT_BYTES] = fat
    img[fat_off + _FLP_FAT_BYTES:fat_off + 2 * _FLP_FAT_BYTES] = fat

    # ── Root directory entry'leri ──────────────────────────────────
    # FAT 8.3 dosya adıyla sınırlı; "Autounattend.xml" 16 karakter, sığmaz.
    # Bu yüzden iki LFN girişi + bir kısa (SFN) giriş yazıyoruz:
    #   LFN seq=2 (LAST_LONG bayrağı): "xml" + null + padding
    #   LFN seq=1                    : "Autounattend."
    #   SFN "AUTOUN~1.XML"           : asıl dosya bilgisi
    short_name11 = b'AUTOUN~1XML'  # SFN: 8 base + 3 ext, dot çıkık
    chk = _lfn_checksum(short_name11)
    # LFN payload'ı: tam ad + null terminator + 0xFFFF padding'lerle
    # 13 karakterin katlarına yuvarlanır
    lfn = 'Autounattend.xml' + '\x00'
    while len(lfn) % 13 != 0:
        lfn += '￿'  # U+FFFF padding
    n_lfn = len(lfn) // 13  # "Autounattend.xml" için → 2 LFN entry

    # Root dir'in imaj içindeki başlangıç offset'i
    root_off = (_FLP_RESERVED + _FLP_NUM_FATS * _FLP_SECTORS_PER_FAT) * _FLP_SECTOR
    pos = 0
    # LFN'ler ters sırayla yazılır: önce en yüksek seq (LAST bayraklı), sonra azalarak
    for i in range(n_lfn):
        seq = n_lfn - i
        # Seq numarasına karşılık gelen 13 karakterlik parçayı al
        chunk = lfn[(seq - 1) * 13:seq * 13]
        is_last = (i == 0)  # Diskte ilk yazılan LFN, LAST_LONG (0x40) işaretli
        img[root_off + pos:root_off + pos + 32] = _make_lfn_entry(
            seq, is_last, chunk, chk,
        )
        pos += 32

    # Kısa 8.3 dizin girişi (asıl dosya kaydı)
    de = bytearray(32)
    de[0:11] = short_name11                              # Byte 0-10: 8.3 ad
    de[11] = 0x20                                        # Attribute: 0x20 = ARCHIVE
    # Byte 12-25: tarih/saat alanları (sıfır bırakıldı, FAT bunu tolere eder)
    de[26:28] = (2).to_bytes(2, 'little')                # İlk cluster numarası (2)
    de[28:32] = len(xml_bytes).to_bytes(4, 'little')     # Dosya boyutu (byte)
    img[root_off + pos:root_off + pos + 32] = de

    # ── Veri bölgesi: cluster 2'ye XML içeriğini yaz ────────────────
    # Cluster 2'nin imaj offset'i = _FLP_DATA_OFFSET (16896).
    # Dosya boyutu 1 sektörden büyükse FAT zincirini takip ederek
    # ardışık cluster'lara yayılır; ardışık atadığımız için
    # tek bir kopyalama yeterli.
    img[_FLP_DATA_OFFSET:_FLP_DATA_OFFSET + len(xml_bytes)] = xml_bytes

    return bytes(img)


def script_dir() -> Path:
    """Çalışan scriptin/EXE'nin bulunduğu dizini döndür.

    PyInstaller ile derlenince ``sys.frozen`` True olur ve `__file__`
    geçici extract klasörünü gösterir; bu durumda EXE'nin gerçek konumu
    `sys.executable`'dadır. Aksi halde normal `__file__` tabanlı çözüm.

    `iso.xml` arandığı zaman programın yanındaki dosyaya bakılması için.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# ── Renk paleti ────────────────────────────────────────────────────
C = {
    'bg':      '#070A0E',
    'panel':   '#0C1117',
    'panel2':  '#040608',
    'line':    '#1A2230',
    'text':    '#C7D5E0',
    'dim':     '#5A6B7A',
    'accent':  '#00FFA3',  # neon yeşil
    'magenta': '#FF2E88',
    'cyan':    '#00D4FF',
    'amber':   '#FFB800',
}


# ════════════════════════════════════════════════════════════════════
# i18n SÖZLÜĞÜ (TR varsayılan + EN)
# ════════════════════════════════════════════════════════════════════
# Tüm kullanıcıya görünen metinler (başlıklar, butonlar, log mesajları,
# hata diyalogları, onay metinleri) anahtarla buradan çekilir. Sağ üstteki
# TR/EN butonlarıyla anlık dil değiştirme `_set_lang()` ile sağlanır.
# Yeni metin eklerken her iki dile de aynı anahtarı eklemek şart.
# Format string'lerde {name}, {n}, {ram} vs. .format() ile doldurulur.
I18N = {
    'tr': {
        'window_title': 'SEGESYS // vm.forge — SegeSystems Sanal PC Kurulum Aracı v1',
        'iso_section': 'ISO İMAJI',
        'iso_placeholder': 'ISO dosyası seçin...',
        'select': 'SEÇ',
        'mode_section': 'MOD',
        'mode_batch': 'TOPLU',
        'mode_custom': 'CUSTOM',
        'count_section': 'ADET',
        'name_section': 'İSİM',
        'memory_section': 'BELLEK',
        'cpu_section': 'İŞLEMCİ',
        'disk_section': 'DİSK (GB)',
        'custom_hint': 'Her VM için ayrı isim, bellek, işlemci ve disk gir.',
        'add_vm': '+ VM EKLE',
        'remove': '×',
        'cores': 'çekirdek',
        'execute': '▶ ÇALIŞTIR',
        'forging': '▮▮ ÇALIŞIYOR',
        'bypass': '⚡ BYPASS ET',
        'log_count': '[ STDOUT · {n} SATIR ]',
        'instances': '[ AKTİF VM · {n} ]',
        'no_instances': 'henüz VM yok',
        'ready': 'Kuruluma hazır!',
        'state_idle': '● BEKLİYOR',
        'state_busy': '● ÇALIŞIYOR',
        'state_ready': '● HAZIR',
        'footer_left': 'segemacro.com :: forge daemon',
        'iso_required': 'ISO dosyası seçin!',
        'iso_missing': 'ISO dosyası bulunamadı!',
        'count_invalid': 'Geçersiz VM sayısı!',
        'count_min': "VM sayısı 1'den büyük olmalı!",
        'name_required': 'VM ismi boş olamaz!',
        'disk_min': 'Disk boyutu en az 10 GB olmalı!',
        'custom_empty': 'Custom modda en az bir VM eklemelisiniz!',
        'custom_dup': 'Tekrar eden VM ismi: ',
        'confirm_title': 'Onay',
        'confirm_batch': "{n} adet '{name}' VM'i oluşturulsun mu?\nDisk: {hdd}GB · RAM: {ram} · CPU: {cpu}\nISO ile boot edecek",
        'confirm_custom': "{n} özel VM oluşturulsun mu?\nISO ile boot edecekler",
        'tools_title': 'VMware Tools Kontrolü',
        'tools_question': "Tüm VMware'lere VMtools kurdunuz mu?\n\n❌ HAYIR: Lütfen önce VMtools kurun\n✅ EVET: Bypass işlemine devam et",
        'tools_first': "Önce tüm VM'lere VMware Tools kurun!",
        'vmware_missing': 'VMware Workstation kurulu değil!',
        'no_vm_folder': 'VM klasörü bulunamadı!',
        'no_match_vm': "'{base}' ile başlayan VM bulunamadı!",
        'success_title': 'Başarılı! 🎉',
        'install_done': "✅ {n} VM oluşturuldu!\n\n✅ ISO'dan boot edecek\n✅ 3D Graphics aktif\n\nSonraki adım: Tüm VM'lere VMware Tools kurun, ardından BYPASS ET.",
        'bypass_done': "{n} VM başarıyla güncellendi!\n\n✅ VMware detection bypass edildi\n✅ ISO'lar ayırıldı\n✅ Boot sırası HDD öncelikli\n🚀 VM'ler hazır!",
        'no_vm_processed': 'İşlenecek VM bulunamadı!',
        'error_title': 'Hata',
        'install_error': 'Kurulum hatası:\n{msg}',
        'bypass_error': 'Bypass + ISO ayırma başarısız:\n{msg}',
        'web_open_fail': 'Web sitesi açılamadı!\nManuel olarak: https://www.segemacro.com',
        'info_title': 'Bilgi',
        'memory_log': 'bellek profili → {ram}',
        'cpu_log': 'işlemci profili → {cpu} çekirdek',
        'mode_log': 'mod → {mode}',
        'iso_log': 'iso seçildi · {name}',
        'init_log': 'init · {n} sanal makine oluşturuluyor',
        'alloc_log': 'alloc · {name}',
        'vmdk_log': 'vmdk → {hdd}GB ide',
        'mount_log': 'iso bağla → {name}',
        'boot_log': 'boot · ide1:0 connected · {name}',
        'done_log': 'tamam · {n} VM çevrimiçi · /// hazır',
        'bypass_init_log': 'bypass · {n} hedef bulundu',
        'stop_log': 'durdur · {name}',
        'inject_log': 'anahtar enjekte → {name}',
        'eject_log': 'iso çıkar · {name}',
        'bypass_done_log': 'bypass tamam · {n} hedef',
        'unattend_found': 'iso.xml bulundu · otomatik kurulum aktif',
        'unattend_missing': 'iso.xml yok · manuel kurulum (Windows tüm soruları soracak)',
        'unattend_prepared': 'unattend hazır · ComputerName={name}',
        'floppy_written': 'floppy yazıldı · {name}.flp (1.44 MB)',
    },
    'en': {
        'window_title': 'SEGESYS // vm.forge — SegeSystems Virtual PC Setup Tool v1',
        'iso_section': 'ISO IMAGE',
        'iso_placeholder': 'select iso file...',
        'select': 'SELECT',
        'mode_section': 'MODE',
        'mode_batch': 'BATCH',
        'mode_custom': 'CUSTOM',
        'count_section': 'COUNT',
        'name_section': 'NAME',
        'memory_section': 'MEMORY',
        'cpu_section': 'CPU',
        'disk_section': 'DISK (GB)',
        'custom_hint': 'Per-VM name, memory, cpu and disk.',
        'add_vm': '+ ADD VM',
        'remove': '×',
        'cores': 'cores',
        'execute': '▶ EXECUTE FORGE',
        'forging': '▮▮ FORGING',
        'bypass': '⚡ BYPASS DETECTION',
        'log_count': '[ STDOUT · {n} LINES ]',
        'instances': '[ ACTIVE INSTANCES · {n} ]',
        'no_instances': 'no instances yet',
        'ready': 'Ready to forge!',
        'state_idle': '● IDLE',
        'state_busy': '● BUSY',
        'state_ready': '● READY',
        'footer_left': 'segemacro.com :: forge daemon',
        'iso_required': 'Select an ISO file!',
        'iso_missing': 'ISO file not found!',
        'count_invalid': 'Invalid VM count!',
        'count_min': 'VM count must be at least 1!',
        'name_required': 'VM name cannot be empty!',
        'disk_min': 'Disk size must be at least 10 GB!',
        'custom_empty': 'Custom mode requires at least one VM!',
        'custom_dup': 'Duplicate VM name: ',
        'confirm_title': 'Confirm',
        'confirm_batch': "Create {n} '{name}' VMs?\nDisk: {hdd}GB · RAM: {ram} · CPU: {cpu}\nWill boot from ISO",
        'confirm_custom': "Create {n} custom VMs?\nThey will boot from ISO",
        'tools_title': 'VMware Tools Check',
        'tools_question': "Did you install VMtools on every VM?\n\n❌ NO: Install VMtools first\n✅ YES: Continue with bypass",
        'tools_first': 'Install VMware Tools on all VMs first!',
        'vmware_missing': 'VMware Workstation is not installed!',
        'no_vm_folder': 'VM folder not found!',
        'no_match_vm': "No VM starting with '{base}' found!",
        'success_title': 'Success! 🎉',
        'install_done': "✅ {n} VMs created!\n\n✅ Will boot from ISO\n✅ 3D Graphics enabled\n\nNext: Install VMware Tools on every VM, then BYPASS DETECTION.",
        'bypass_done': "{n} VMs updated successfully!\n\n✅ VMware detection bypassed\n✅ ISOs disconnected\n✅ Boot order set to HDD first\n🚀 VMs are ready!",
        'no_vm_processed': 'No VM to process!',
        'error_title': 'Error',
        'install_error': 'Install error:\n{msg}',
        'bypass_error': 'Bypass + ISO disconnect failed:\n{msg}',
        'web_open_fail': 'Could not open website!\nManually: https://www.segemacro.com',
        'info_title': 'Info',
        'memory_log': 'memory profile → {ram}',
        'cpu_log': 'cpu profile → {cpu} cores',
        'mode_log': 'mode → {mode}',
        'iso_log': 'iso selected · {name}',
        'init_log': 'init · creating {n} virtual machine(s)',
        'alloc_log': 'alloc · {name}',
        'vmdk_log': 'vmdk → {hdd}GB ide',
        'mount_log': 'mount iso → {name}',
        'boot_log': 'boot · ide1:0 connected · {name}',
        'done_log': 'done · {n} vm online · /// ready',
        'bypass_init_log': 'bypass · {n} target(s) found',
        'stop_log': 'stop · {name}',
        'inject_log': 'inject keys → {name}',
        'eject_log': 'iso eject · {name}',
        'bypass_done_log': 'bypass complete · {n} target(s)',
        'unattend_found': 'iso.xml found · auto-install enabled',
        'unattend_missing': 'iso.xml missing · manual install (Windows will ask everything)',
        'unattend_prepared': 'unattend ready · ComputerName={name}',
        'floppy_written': 'floppy written · {name}.flp (1.44 MB)',
    },
}


# ── Stylesheet ─────────────────────────────────────────────────────
def cyber_qss(mono: str) -> str:
    return f"""
    QMainWindow, QWidget {{
        background-color: {C['bg']};
        color: {C['text']};
        font-family: {mono};
        font-size: 12px;
    }}
    QFrame#titlebar {{
        background-color: {C['panel']};
        border-bottom: 1px solid {C['line']};
    }}
    QFrame#footer {{
        background-color: {C['panel2']};
        border-top: 1px solid {C['line']};
    }}
    QFrame.panel {{
        background-color: {C['panel']};
        border: 1px solid {C['line']};
    }}
    QFrame.rightpane {{
        background-color: {C['panel2']};
    }}
    QFrame.vmrow {{
        background-color: #050709;
        border: 1px solid {C['line']};
        border-radius: 6px;
    }}
    QLabel.dim {{
        color: {C['dim']};
        font-size: 11px;
        letter-spacing: 1px;
    }}
    QLineEdit, QSpinBox, QComboBox {{
        background-color: #050709;
        border: 1px solid {C['line']};
        border-radius: 6px;
        padding: 6px 10px;
        color: {C['text']};
        font-family: {mono};
        font-size: 11px;
        selection-background-color: {C['accent']};
        selection-color: #021015;
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border: 1px solid {C['accent']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {C['cyan']};
        margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {C['panel']};
        color: {C['text']};
        border: 1px solid {C['line']};
        selection-background-color: rgba(0, 255, 163, 0.15);
        selection-color: {C['accent']};
        outline: 0;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background-color: transparent;
        border: none;
        width: 14px;
    }}
    QSpinBox::up-arrow {{
        image: none;
        border-left: 3px solid transparent;
        border-right: 3px solid transparent;
        border-bottom: 4px solid {C['cyan']};
    }}
    QSpinBox::down-arrow {{
        image: none;
        border-left: 3px solid transparent;
        border-right: 3px solid transparent;
        border-top: 4px solid {C['cyan']};
    }}

    QPushButton.select {{
        background-color: rgba(0, 212, 255, 0.08);
        color: {C['cyan']};
        border: 1px solid rgba(0, 212, 255, 0.40);
        border-radius: 6px;
        padding: 0 12px;
        font-weight: 700;
        font-size: 10px;
        letter-spacing: 1.5px;
    }}
    QPushButton.select:hover {{ background-color: rgba(0, 212, 255, 0.18); }}

    QPushButton.tab {{
        background-color: #050709;
        color: {C['dim']};
        border: 1px solid {C['line']};
        border-radius: 6px;
        padding: 8px 0;
        font-weight: 700;
        font-size: 10px;
        letter-spacing: 2px;
    }}
    QPushButton.tab:hover  {{ border: 1px solid {C['cyan']}; color: {C['text']}; }}
    QPushButton.tab:checked {{
        background-color: rgba(0, 212, 255, 0.10);
        color: {C['cyan']};
        border: 1px solid {C['cyan']};
    }}

    QPushButton.ram, QPushButton.cpu {{
        background-color: #050709;
        color: {C['dim']};
        border: 1px solid {C['line']};
        border-radius: 6px;
        padding: 8px 0;
        font-weight: 600;
        font-size: 11px;
        letter-spacing: 0.5px;
    }}
    QPushButton.ram:hover, QPushButton.cpu:hover {{ border: 1px solid {C['accent']}; color: {C['text']}; }}
    QPushButton.ram:checked {{
        background-color: rgba(0, 255, 163, 0.10);
        color: {C['accent']};
        border: 1px solid {C['accent']};
    }}
    QPushButton.cpu:checked {{
        background-color: rgba(255, 46, 136, 0.10);
        color: {C['magenta']};
        border: 1px solid {C['magenta']};
    }}

    QPushButton.add {{
        background-color: rgba(0, 255, 163, 0.06);
        color: {C['accent']};
        border: 1px dashed rgba(0, 255, 163, 0.45);
        border-radius: 6px;
        padding: 8px;
        font-weight: 700;
        font-size: 11px;
        letter-spacing: 1.5px;
    }}
    QPushButton.add:hover {{ background-color: rgba(0, 255, 163, 0.12); }}

    QPushButton.remove {{
        background-color: transparent;
        color: {C['magenta']};
        border: 1px solid rgba(255, 46, 136, 0.40);
        border-radius: 4px;
        font-weight: 700;
        font-size: 12px;
    }}
    QPushButton.remove:hover {{ background-color: rgba(255, 46, 136, 0.15); }}

    QPushButton.lang {{
        background-color: transparent;
        color: {C['dim']};
        border: 1px solid {C['line']};
        border-radius: 4px;
        padding: 2px 8px;
        font-weight: 700;
        font-size: 10px;
        letter-spacing: 1px;
    }}
    QPushButton.lang:checked {{
        background-color: rgba(0, 212, 255, 0.10);
        color: {C['cyan']};
        border: 1px solid {C['cyan']};
    }}

    QPushButton#executeBtn {{
        background-color: {C['accent']};
        color: #021015;
        border: none;
        border-radius: 8px;
        padding: 14px 16px;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 2px;
    }}
    QPushButton#executeBtn:hover    {{ background-color: #1AFFB0; }}
    QPushButton#executeBtn:disabled {{ background-color: {C['line']}; color: {C['dim']}; }}

    QPushButton#bypassBtn {{
        background-color: transparent;
        color: {C['magenta']};
        border: 1px solid rgba(255, 46, 136, 0.55);
        border-radius: 8px;
        padding: 12px 16px;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 2px;
    }}
    QPushButton#bypassBtn:hover    {{ background-color: rgba(255, 46, 136, 0.12); }}
    QPushButton#bypassBtn:disabled {{ color: {C['dim']}; border: 1px solid {C['line']}; }}

    QSlider::groove:horizontal {{
        height: 4px;
        background: {C['line']};
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {C['amber']};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {C['amber']};
        width: 14px;
        height: 14px;
        margin: -6px 0;
        border-radius: 7px;
    }}

    QProgressBar {{
        background-color: {C['line']};
        border: none;
        max-height: 3px;
        min-height: 3px;
    }}
    QProgressBar::chunk {{
        background-color: {C['accent']};
    }}

    QTextEdit#logbox {{
        background-color: {C['panel2']};
        border: none;
        color: {C['text']};
        font-family: {mono};
        font-size: 11px;
        padding: 0 18px 12px 18px;
    }}
    QScrollArea, QScrollArea > QWidget > QWidget {{
        background-color: transparent;
        border: none;
    }}
    QScrollBar:vertical, QScrollBar:horizontal {{
        background: {C['panel2']};
        width: 8px; height: 8px;
        border: none;
    }}
    QScrollBar::handle {{ background: {C['line']}; border-radius: 4px; }}
    QScrollBar::handle:hover {{ background: {C['dim']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ background: none; border: none; }}

    QFrame.vmcard {{
        background-color: rgba(0, 255, 163, 0.06);
        border: 1px solid rgba(0, 255, 163, 0.30);
        border-radius: 6px;
    }}
    """


# ════════════════════════════════════════════════════════════════════
# WORKER → UI SİNYALLERİ
# ════════════════════════════════════════════════════════════════════
# Kurulum/bypass işleri arka plan thread'inde çalışır. Qt'de UI'ye
# yalnızca ana thread dokunabilir; bu yüzden worker thread, UI'yi
# pyqtSignal yayını üzerinden tetikler. Sinyaller ana thread'in event
# loop'unda işlenir, race condition olmaz.
class WorkerSignals(QObject):
    progress  = pyqtSignal(float)              # 0..1 progress yüzdesi
    status    = pyqtSignal(str)                # Kısa durum metni (sağ panel)
    log       = pyqtSignal(str, str)           # (mesaj, kind: ok/info/cmd/sys/err)
    vm_added  = pyqtSignal(str, str, int, int) # (ad, ram, hdd, cpu) — VM kart ekle
    state     = pyqtSignal(str)                # 'idle' / 'running' / 'done'
    finished  = pyqtSignal(str, str)           # (başlık, mesaj) → bilgi diyaloğu
    error     = pyqtSignal(str, str)           # (başlık, mesaj) → hata diyaloğu
    reset_btn = pyqtSignal()                   # İşlem bitince butonları aç


# ════════════════════════════════════════════════════════════════════
# CUSTOM MOD — TEK VM SATIRI WIDGET'I
# ════════════════════════════════════════════════════════════════════
# CUSTOM modda her VM için ayrı yapılandırma satırı. Kullanıcı isim,
# RAM, CPU ve disk değerlerini bağımsız girer. "+ VM EKLE" yeni satır
# oluşturur, sağdaki "×" butonu satırı kaldırır.
class VMRow(QFrame):
    # Sinyal: silme talebi geldiğinde parent'ı bilgilendir
    removed = pyqtSignal(object)

    # Combobox seçenekleri (RAM_MB tablosuyla senkron olmalı)
    RAM_OPTIONS = ('1GB', '2GB', '4GB', '8GB')
    CPU_OPTIONS = ('1', '2', '4', '8')

    def __init__(self, name: str, lang_label: dict):
        """Yeni VM satırı oluştur.

        Args:
            name: Varsayılan VM adı (LineEdit'e prefill)
            lang_label: Aktif dilin I18N sözlüğü ('remove' anahtarı için)
        """
        super().__init__()
        self.setProperty('class', 'vmrow')
        self.setFixedHeight(46)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        self.name_in = QLineEdit(name)
        self.name_in.setMinimumWidth(140)

        self.ram_in = QComboBox()
        self.ram_in.addItems(self.RAM_OPTIONS)
        self.ram_in.setCurrentText('2GB')
        self.ram_in.setFixedWidth(70)

        self.cpu_in = QComboBox()
        self.cpu_in.addItems(self.CPU_OPTIONS)
        self.cpu_in.setCurrentText('2')
        self.cpu_in.setFixedWidth(54)

        self.hdd_in = QSpinBox()
        self.hdd_in.setRange(10, 500)
        self.hdd_in.setValue(50)
        self.hdd_in.setSuffix(' GB')
        self.hdd_in.setFixedWidth(86)

        rm = QPushButton(lang_label.get('remove', '×'))
        rm.setProperty('class', 'remove')
        rm.setFixedSize(28, 28)
        rm.setCursor(Qt.PointingHandCursor)
        rm.clicked.connect(lambda: self.removed.emit(self))

        h.addWidget(self.name_in, 1)
        h.addWidget(self.ram_in, 0)
        h.addWidget(self.cpu_in, 0)
        h.addWidget(self.hdd_in, 0)
        h.addWidget(rm, 0)

    def values(self) -> tuple:
        """Bu satırın anlık değerlerini (ad, RAM, CPU, HDD) tuple olarak döndür."""
        return (
            self.name_in.text().strip(),
            self.ram_in.currentText(),
            int(self.cpu_in.currentText()),
            self.hdd_in.value(),
        )


# ════════════════════════════════════════════════════════════════════
# ANA UYGULAMA PENCERESİ
# ════════════════════════════════════════════════════════════════════
class SegeSystemsGUI(QMainWindow):
    """SegeSystems Sanal PC Kurulum Aracı — ana pencere.

    Sorumluluklar:
      - GUI'yi inşa et (titlebar, sol config paneli, sağ log paneli, footer)
      - Kullanıcı girdilerini doğrula
      - Kurulum/bypass thread'lerini başlat ve sinyalleri UI'ya yansıt
      - i18n metin değişimini yönet
      - autounattend floppy'sini per-VM üret ve .vmx'e iliştir
    """

    # ── VMware Workstation araçlarının varsayılan yolları ───────────
    # Farklı yola kuruluysa bu üç sabiti güncellemen gerekir.
    VMRUN_PATH = r'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
    VMWARE_PATH = r'C:\Program Files (x86)\VMware\VMware Workstation\vmware.exe'
    VDISK_PATH = r'C:\Program Files (x86)\VMware\VMware Workstation\vmware-vdiskmanager.exe'

    # GUI'deki "1GB", "2GB" gibi seçimleri MB'a çeviren tablo (.vmx'te
    # `memsize = "1024"` şeklinde MB cinsinden yazılır)
    RAM_MB = {'1GB': 1024, '2GB': 2048, '4GB': 4096, '8GB': 8192}

    def __init__(self):
        super().__init__()
        # ── Uygulama state ──
        self.lang = 'tr'           # Aktif dil ('tr' veya 'en')
        self.ram_choice = '2GB'    # Toplu modda seçili RAM
        self.cpu_choice = '2'      # Toplu modda seçili CPU çekirdek sayısı
        self.mode = 'batch'        # 'batch' (toplu) veya 'custom'
        self._busy = False         # Kurulum/bypass çalışıyor mu? (çift tık koruması)

        # Pencere geometrisi — sağ panelin log + VM raf alanına yetecek genişlik
        self.setMinimumSize(1180, 740)
        self.resize(1180, 740)
        self.setWindowTitle(self.t('window_title'))

        # Worker thread → UI köprüsü: sinyalleri slot metodlara bağla
        self.signals = WorkerSignals()
        self.signals.progress.connect(self._set_progress)
        self.signals.status.connect(self._set_status)
        self.signals.log.connect(self._append_log)
        self.signals.vm_added.connect(self._add_vm_card)
        self.signals.state.connect(self._set_state)
        self.signals.finished.connect(self._show_info)
        self.signals.error.connect(self._show_error)
        self.signals.reset_btn.connect(self._reset_btns)

        self._setup_ui()
        self._set_state('idle')
        self._push_log('segesys.daemon ready · awaiting input', 'sys')

    # ═══ i18n yardımcıları ═════════════════════════════════════════════
    def t(self, key: str, **kwargs) -> str:
        """Aktif dilde anahtara karşılık gelen metni döndür.

        Anahtar EN sözlüğünde yoksa TR'ye düşer; orada da yoksa anahtar
        adı kendisi döndürülür (kayıp metinleri fark etmek kolay olsun).
        kwargs varsa Python str.format() ile placeholder'lar doldurulur.
        """
        s = I18N[self.lang].get(key, I18N['tr'].get(key, key))
        return s.format(**kwargs) if kwargs else s

    def _set_lang(self, lang: str):
        """Dili değiştir ve tüm görünür metinleri tazele."""
        if lang == self.lang:
            return
        self.lang = lang
        self.tr_btn.setChecked(lang == 'tr')
        self.en_btn.setChecked(lang == 'en')
        self._refresh_texts()
        self._push_log(f'language → {lang}', 'sys')

    def _refresh_texts(self):
        """Aktif dile göre tüm UI etiketlerini güncelle.
        Yeni metin eklediğinde buraya da satır eklemeyi unutma."""
        self.setWindowTitle(self.t('window_title'))
        self.iso_section.setText(self.t('iso_section'))
        self.iso_entry.setPlaceholderText(self.t('iso_placeholder'))
        self.select_btn.setText(self.t('select'))
        self.mode_section.setText(self.t('mode_section'))
        self.batch_btn.setText(self.t('mode_batch'))
        self.custom_btn.setText(self.t('mode_custom'))
        self.count_section.setText(self.t('count_section'))
        self.name_section.setText(self.t('name_section'))
        self.memory_section.setText(self.t('memory_section'))
        self.cpu_section.setText(self.t('cpu_section'))
        self.disk_section.setText(self.t('disk_section'))
        self.custom_hint.setText(self.t('custom_hint'))
        self.add_vm_btn.setText(self.t('add_vm'))
        self.execute_btn.setText(self.t('execute'))
        self.bypass_btn.setText(self.t('bypass'))
        self.status_label.setText(self.t('ready'))
        self._refresh_log_count()
        self._refresh_shelf_header()
        self._set_state(self._current_state)

    # ═══ UI inşa ═══════════════════════════════════════════════════════
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_titlebar())

        body = QWidget()
        body_l = QHBoxLayout(body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(0)
        body_l.addWidget(self._build_left_panel(), 0)
        body_l.addWidget(self._build_right_panel(), 1)
        root.addWidget(body, 1)

        root.addWidget(self._build_footer())

    def _build_titlebar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName('titlebar')
        bar.setFixedHeight(46)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(14)

        # Trafik ışığı noktaları
        dots = QHBoxLayout()
        dots.setSpacing(6)
        for color in (C['magenta'], C['amber'], C['accent']):
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(f'background-color: {color}; border-radius: 5px;')
            dots.addWidget(dot)
        dots_w = QWidget()
        dots_w.setLayout(dots)
        h.addWidget(dots_w)

        title = QLabel(
            f"┌─ <span style='color:{C['accent']};font-weight:700'>SEGE</span>"
            f"<span style='color:{C['magenta']};font-weight:700'>SYS</span> "
            f"─ vm.forge ─ <span style='color:{C['cyan']}'>www.segemacro.com</span> ─┐"
        )
        title.setStyleSheet(f"color: {C['dim']}; font-size: 11px; letter-spacing: 1.5px;")
        title.setCursor(Qt.PointingHandCursor)
        title.mousePressEvent = lambda e: self.open_website()
        h.addWidget(title)

        h.addStretch()

        # Dil seçici
        self.tr_btn = QPushButton('TR')
        self.tr_btn.setProperty('class', 'lang')
        self.tr_btn.setCheckable(True)
        self.tr_btn.setChecked(True)
        self.tr_btn.setCursor(Qt.PointingHandCursor)
        self.tr_btn.setFixedHeight(24)
        self.tr_btn.clicked.connect(lambda: self._set_lang('tr'))
        self.en_btn = QPushButton('EN')
        self.en_btn.setProperty('class', 'lang')
        self.en_btn.setCheckable(True)
        self.en_btn.setCursor(Qt.PointingHandCursor)
        self.en_btn.setFixedHeight(24)
        self.en_btn.clicked.connect(lambda: self._set_lang('en'))
        h.addWidget(self.tr_btn)
        h.addWidget(self.en_btn)

        sep0 = QLabel('│')
        sep0.setStyleSheet(f"color: {C['line']}; font-size: 11px; padding: 0 4px;")
        h.addWidget(sep0)

        # Status göstergesi
        self.state_label = QLabel('● BEKLİYOR')
        self.state_label.setStyleSheet(f"color: {C['cyan']}; font-size: 11px; font-weight: 700;")
        ver = QLabel('v1')
        ver.setStyleSheet(f"color: {C['dim']}; font-size: 11px; letter-spacing: 1px;")
        h.addWidget(ver)
        sep = QLabel('//')
        sep.setStyleSheet(f"color: {C['dim']}; font-size: 11px;")
        h.addWidget(sep)
        h.addWidget(self.state_label)
        return bar

    def _build_left_panel(self) -> QFrame:
        pane = QFrame()
        pane.setProperty('class', 'panel')
        pane.setFixedWidth(380)
        v = QVBoxLayout(pane)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)

        # ISO satırı
        self.iso_section = self._section_label(self.t('iso_section'), C['cyan'])
        v.addWidget(self.iso_section)
        iso_row = QHBoxLayout()
        self.iso_entry = QLineEdit()
        self.iso_entry.setPlaceholderText(self.t('iso_placeholder'))
        iso_row.addWidget(self.iso_entry)
        self.select_btn = QPushButton(self.t('select'))
        self.select_btn.setProperty('class', 'select')
        self.select_btn.setFixedWidth(70)
        self.select_btn.setFixedHeight(32)
        self.select_btn.setCursor(Qt.PointingHandCursor)
        self.select_btn.clicked.connect(self.select_iso)
        iso_row.addWidget(self.select_btn)
        v.addLayout(iso_row)

        # Mod sekmesi (TOPLU / CUSTOM)
        self.mode_section = self._section_label(self.t('mode_section'), C['cyan'])
        v.addWidget(self.mode_section)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.batch_btn = QPushButton(self.t('mode_batch'))
        self.batch_btn.setProperty('class', 'tab')
        self.batch_btn.setCheckable(True)
        self.batch_btn.setChecked(True)
        self.batch_btn.setFixedHeight(32)
        self.batch_btn.setCursor(Qt.PointingHandCursor)
        self.custom_btn = QPushButton(self.t('mode_custom'))
        self.custom_btn.setProperty('class', 'tab')
        self.custom_btn.setCheckable(True)
        self.custom_btn.setFixedHeight(32)
        self.custom_btn.setCursor(Qt.PointingHandCursor)
        self.mode_group.addButton(self.batch_btn, 0)
        self.mode_group.addButton(self.custom_btn, 1)
        self.mode_group.buttonClicked[int].connect(self._on_mode_changed)
        mode_row.addWidget(self.batch_btn)
        mode_row.addWidget(self.custom_btn)
        v.addLayout(mode_row)

        # Mod içerikleri (stacked)
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_batch_widget())
        self.mode_stack.addWidget(self._build_custom_widget())
        v.addWidget(self.mode_stack, 1)

        # Aksiyonlar
        self.execute_btn = QPushButton(self.t('execute'))
        self.execute_btn.setObjectName('executeBtn')
        self.execute_btn.setFixedHeight(46)
        self.execute_btn.setCursor(Qt.PointingHandCursor)
        self.execute_btn.clicked.connect(self.start_installation)
        v.addWidget(self.execute_btn)

        self.bypass_btn = QPushButton(self.t('bypass'))
        self.bypass_btn.setObjectName('bypassBtn')
        self.bypass_btn.setFixedHeight(40)
        self.bypass_btn.setCursor(Qt.PointingHandCursor)
        self.bypass_btn.clicked.connect(self.start_bypass)
        v.addWidget(self.bypass_btn)

        return pane

    def _build_batch_widget(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # Adet + İsim
        cn = QGridLayout()
        cn.setHorizontalSpacing(12)
        cn.setVerticalSpacing(6)
        self.count_section = self._section_label(self.t('count_section'), C['magenta'])
        self.name_section = self._section_label(self.t('name_section'), C['magenta'])
        cn.addWidget(self.count_section, 0, 0)
        cn.addWidget(self.name_section, 0, 1)
        self.count_entry = QLineEdit('3')
        self.count_entry.setAlignment(Qt.AlignCenter)
        self.name_entry = QLineEdit('PC')
        self.name_entry.setAlignment(Qt.AlignCenter)
        cn.addWidget(self.count_entry, 1, 0)
        cn.addWidget(self.name_entry, 1, 1)
        v.addLayout(cn)

        # Bellek
        self.memory_section = self._section_label(self.t('memory_section'), C['accent'])
        v.addWidget(self.memory_section)
        ram_row = QHBoxLayout()
        ram_row.setSpacing(6)
        self.ram_buttons = QButtonGroup(self)
        self.ram_buttons.setExclusive(True)
        for label in ('1GB', '2GB', '4GB', '8GB'):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty('class', 'ram')
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(32)
            self.ram_buttons.addButton(b)
            ram_row.addWidget(b)
            if label == self.ram_choice:
                b.setChecked(True)
        self.ram_buttons.buttonClicked.connect(self._on_ram_changed)
        v.addLayout(ram_row)

        # CPU
        self.cpu_section = self._section_label(self.t('cpu_section'), C['magenta'])
        v.addWidget(self.cpu_section)
        cpu_row = QHBoxLayout()
        cpu_row.setSpacing(6)
        self.cpu_buttons = QButtonGroup(self)
        self.cpu_buttons.setExclusive(True)
        for label in ('1', '2', '4', '8'):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty('class', 'cpu')
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(32)
            self.cpu_buttons.addButton(b)
            cpu_row.addWidget(b)
            if label == self.cpu_choice:
                b.setChecked(True)
        self.cpu_buttons.buttonClicked.connect(self._on_cpu_changed)
        v.addLayout(cpu_row)

        # Disk slider
        self.disk_section = self._section_label(self.t('disk_section'), C['amber'])
        v.addWidget(self.disk_section)
        disk_row = QHBoxLayout()
        disk_row.setSpacing(10)
        self.hdd_slider = QSlider(Qt.Horizontal)
        self.hdd_slider.setMinimum(10)
        self.hdd_slider.setMaximum(500)
        self.hdd_slider.setValue(50)
        self.hdd_slider.valueChanged.connect(self._on_hdd_changed)
        disk_row.addWidget(self.hdd_slider, 1)
        self.hdd_badge = QLabel('50<span style="font-size:10px;color:#5A6B7A;"> GB</span>')
        self.hdd_badge.setAlignment(Qt.AlignCenter)
        self.hdd_badge.setFixedSize(64, 28)
        self.hdd_badge.setStyleSheet(
            f"color: {C['amber']}; font-weight: 700; font-size: 13px; "
            f"border: 1px solid rgba(255,184,0,0.4); border-radius: 4px; "
            f"background-color: rgba(255,184,0,0.08);"
        )
        disk_row.addWidget(self.hdd_badge)
        v.addLayout(disk_row)

        v.addStretch(1)
        return w

    def _build_custom_widget(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self.custom_hint = QLabel(self.t('custom_hint'))
        self.custom_hint.setStyleSheet(f"color: {C['dim']}; font-size: 10px; letter-spacing: 1px;")
        self.custom_hint.setWordWrap(True)
        v.addWidget(self.custom_hint)

        # Kolon başlıkları
        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 8, 0)
        head.setSpacing(6)
        for txt, w_px, color in (
            ('NAME', 0,  C['magenta']),
            ('RAM',  70, C['accent']),
            ('CPU',  54, C['magenta']),
            ('DISK', 86, C['amber']),
            ('',     28, C['dim']),
        ):
            lbl = QLabel(txt)
            lbl.setStyleSheet(
                f"color: {color}; font-size: 9px; font-weight: 700; letter-spacing: 2px;"
            )
            if w_px:
                lbl.setFixedWidth(w_px)
                lbl.setAlignment(Qt.AlignCenter)
            else:
                lbl.setMinimumWidth(140)
            head.addWidget(lbl, 0 if w_px else 1)
        v.addLayout(head)

        # Liste alanı
        self.custom_scroll = QScrollArea()
        self.custom_scroll.setWidgetResizable(True)
        inner = QWidget()
        self.custom_layout = QVBoxLayout(inner)
        self.custom_layout.setContentsMargins(0, 0, 0, 0)
        self.custom_layout.setSpacing(6)
        self.custom_layout.addStretch(1)
        self.custom_scroll.setWidget(inner)
        v.addWidget(self.custom_scroll, 1)

        # + EKLE
        self.add_vm_btn = QPushButton(self.t('add_vm'))
        self.add_vm_btn.setProperty('class', 'add')
        self.add_vm_btn.setCursor(Qt.PointingHandCursor)
        self.add_vm_btn.setFixedHeight(34)
        self.add_vm_btn.clicked.connect(lambda: self._add_custom_row())
        v.addWidget(self.add_vm_btn)

        # Başlangıçta 1 satır ekle
        self._add_custom_row(name='PC-1')
        return w

    def _add_custom_row(self, name: str = ''):
        if not name:
            n = self.custom_layout.count()  # son eleman stretch
            name = f'PC-{n}'
        row = VMRow(name, I18N[self.lang])
        row.removed.connect(self._remove_custom_row)
        # Stretch'ten önce ekle
        self.custom_layout.insertWidget(self.custom_layout.count() - 1, row)

    def _remove_custom_row(self, row: VMRow):
        self.custom_layout.removeWidget(row)
        row.deleteLater()

    def _custom_rows(self) -> list:
        rows = []
        for i in range(self.custom_layout.count()):
            w = self.custom_layout.itemAt(i).widget()
            if isinstance(w, VMRow):
                rows.append(w)
        return rows

    def _build_right_panel(self) -> QFrame:
        pane = QFrame()
        pane.setProperty('class', 'rightpane')
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(3)
        v.addWidget(self.progress_bar)

        head = QWidget()
        h = QHBoxLayout(head)
        h.setContentsMargins(18, 14, 18, 6)
        self.log_count_label = QLabel(self.t('log_count', n=0))
        self.log_count_label.setStyleSheet(f"color: {C['dim']}; font-size: 10px; letter-spacing: 2px;")
        h.addWidget(self.log_count_label)
        h.addStretch()
        legend = QLabel(
            f"<span style='color:{C['accent']}'>●</span> <span style='color:{C['dim']}'>ok</span>  "
            f"<span style='color:{C['cyan']}'>●</span> <span style='color:{C['dim']}'>info</span>  "
            f"<span style='color:{C['magenta']}'>●</span> <span style='color:{C['dim']}'>cmd</span>  "
            f"<span style='color:{C['amber']}'>●</span> <span style='color:{C['dim']}'>sys</span>"
        )
        legend.setStyleSheet('font-size: 10px;')
        h.addWidget(legend)
        v.addWidget(head)

        self.status_label = QLabel(self.t('ready'))
        self.status_label.setStyleSheet(
            f"color: {C['dim']}; font-size: 11px; padding: 0 18px 4px 18px;"
        )
        v.addWidget(self.status_label)

        self.log_box = QTextEdit()
        self.log_box.setObjectName('logbox')
        self.log_box.setReadOnly(True)
        v.addWidget(self.log_box, 1)

        # VM raf
        shelf = QFrame()
        shelf.setStyleSheet(
            f"background-color: {C['panel']}; border-top: 1px solid {C['line']};"
        )
        sv = QVBoxLayout(shelf)
        sv.setContentsMargins(18, 12, 18, 12)
        sv.setSpacing(8)

        self.shelf_header = QLabel(self.t('instances', n=0))
        self.shelf_header.setStyleSheet(f"color: {C['dim']}; font-size: 10px; letter-spacing: 2px;")
        sv.addWidget(self.shelf_header)

        self.shelf_scroll = QScrollArea()
        self.shelf_scroll.setWidgetResizable(True)
        self.shelf_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.shelf_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shelf_scroll.setFixedHeight(72)
        shelf_inner = QWidget()
        self.shelf_layout = QHBoxLayout(shelf_inner)
        self.shelf_layout.setContentsMargins(0, 0, 0, 0)
        self.shelf_layout.setSpacing(8)
        self._empty_card = QLabel(self.t('no_instances'))
        self._empty_card.setStyleSheet(f"color: {C['dim']}; font-size: 11px;")
        self.shelf_layout.addWidget(self._empty_card)
        self.shelf_layout.addStretch(1)
        self.shelf_scroll.setWidget(shelf_inner)
        sv.addWidget(self.shelf_scroll)

        v.addWidget(shelf)
        return pane

    def _build_footer(self) -> QFrame:
        f = QFrame()
        f.setObjectName('footer')
        f.setFixedHeight(28)
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 4, 16, 4)
        left = QLabel(self.t('footer_left'))
        left.setStyleSheet(f"color: {C['dim']}; font-size: 10px; letter-spacing: 1.5px;")
        left.setCursor(Qt.PointingHandCursor)
        left.mousePressEvent = lambda e: self.open_website()
        h.addWidget(left)
        h.addStretch()
        self.footer_state = QLabel('cpu — · ram — · idle')
        self.footer_state.setStyleSheet(f"color: {C['dim']}; font-size: 10px; letter-spacing: 1.5px;")
        h.addWidget(self.footer_state)
        return f

    @staticmethod
    def _section_label(text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 700; letter-spacing: 2px;"
        )
        return lbl

    # ═══ Slot helpers ═════════════════════════════════════════════════
    def _set_progress(self, val: float):
        self.progress_bar.setValue(int(max(0.0, min(1.0, val)) * 1000))

    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _append_log(self, msg: str, kind: str):
        self._push_log(msg, kind)

    def _refresh_log_count(self):
        n = self.log_box.document().blockCount() if self.log_box.toPlainText() else 0
        self.log_count_label.setText(self.t('log_count', n=n))

    def _refresh_shelf_header(self):
        n = sum(
            1 for i in range(self.shelf_layout.count())
            if isinstance(self.shelf_layout.itemAt(i).widget(), QFrame)
        )
        self.shelf_header.setText(self.t('instances', n=n))

    def _push_log(self, msg: str, kind: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        color = {
            'ok':   C['accent'],
            'info': C['cyan'],
            'cmd':  C['magenta'],
            'sys':  C['amber'],
            'err':  C['magenta'],
        }.get(kind, C['cyan'])
        prefix = '$ ' if kind == 'cmd' else '› '
        line = (
            f"<span style='color:{C['dim']}'>{ts}</span> "
            f"<span style='color:{color};font-weight:700;font-size:10px'>{kind.upper():<4}</span> "
            f"<span style='color:{C['text']}'>{prefix}{self._html_escape(msg)}</span>"
        )
        self.log_box.append(line)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())
        self._refresh_log_count()

    @staticmethod
    def _html_escape(s: str) -> str:
        return (
            s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        )

    def _add_vm_card(self, name: str, ram: str, hdd_gb: int, cpu: int):
        if self._empty_card is not None:
            self.shelf_layout.removeWidget(self._empty_card)
            self._empty_card.deleteLater()
            self._empty_card = None
        card = QFrame()
        card.setProperty('class', 'vmcard')
        card.setFixedSize(160, 50)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(10, 6, 10, 6)
        cv.setSpacing(2)
        top = QLabel(f"● {name}")
        top.setStyleSheet(f"color: {C['accent']}; font-size: 11px; font-weight: 700;")
        cv.addWidget(top)
        bot = QLabel(f"{ram} · {cpu} {self.t('cores')} · {hdd_gb}GB")
        bot.setStyleSheet(f"color: {C['dim']}; font-size: 10px;")
        cv.addWidget(bot)
        self.shelf_layout.insertWidget(self.shelf_layout.count() - 1, card)
        self._refresh_shelf_header()

    def _set_state(self, state: str):
        self._current_state = state
        keys = {
            'idle':    ('state_idle',  C['cyan']),
            'running': ('state_busy',  C['amber']),
            'done':    ('state_ready', C['accent']),
        }
        key, color = keys.get(state, ('state_idle', C['cyan']))
        self.state_label.setText(self.t(key))
        self.state_label.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 700;")
        self.footer_state.setText(f'cpu — · ram — · {state}')

    def _show_info(self, title: str, msg: str):
        QMessageBox.information(self, title, msg)

    def _show_error(self, title: str, msg: str):
        QMessageBox.critical(self, title, msg)

    def _reset_btns(self):
        self._busy = False
        self.execute_btn.setEnabled(True)
        self.execute_btn.setText(self.t('execute'))
        self.bypass_btn.setEnabled(True)

    # ═══ Bağlamalar ═══════════════════════════════════════════════════
    def _on_hdd_changed(self, v: int):
        self.hdd_badge.setText(
            f'{v}<span style="font-size:10px;color:#5A6B7A;"> GB</span>'
        )

    def _on_ram_changed(self, btn):
        self.ram_choice = btn.text()
        self._push_log(self.t('memory_log', ram=self.ram_choice), 'info')

    def _on_cpu_changed(self, btn):
        self.cpu_choice = btn.text()
        self._push_log(self.t('cpu_log', cpu=self.cpu_choice), 'info')

    def _on_mode_changed(self, idx: int):
        self.mode = 'batch' if idx == 0 else 'custom'
        self.mode_stack.setCurrentIndex(idx)
        self._push_log(self.t('mode_log', mode=self.mode), 'sys')

    # ═══ Subprocess yardımcısı (stdout/stderr loglanır) ═══════════════
    def _run_logged(self, cmd, label: str, kind: str = 'info',
                    timeout=None, allow_nonzero: bool = False,
                    silent_empty: bool = False):
        """Bir alt-süreci çalıştır ve çıktısının her satırını log'a yansıt.

        Mevcut `subprocess.run` çağrılarını sessizce yutmamak için bu
        yardımcı kullanılır. stdout satırları normal renkte, stderr
        satırları hata renginde, sıfır olmayan exit kodu ekstra `err`
        satırı olarak görünür.

        Args:
            cmd: Komut listesi (subprocess.run'a aynen geçirilir)
            label: Log satırlarının önüne eklenen kısa etiket (örn. "vmrun-stop[PC-1]")
            kind: stdout için kullanılacak log kategorisi
            timeout: Saniye cinsinden zaman aşımı (None=sınırsız)
            allow_nonzero: True ise sıfır olmayan exit kodu hata olarak işaretlenmez
                           (örn. "vm zaten kapalı" durumu için)
            silent_empty: True ise hiç çıktı yoksa "ok (exit 0)" satırı yazılmaz
                          (her tıklamada gürültü yapmaması için `vmrun list` gibi)

        Returns:
            subprocess.CompletedProcess veya None (timeout/OSError'da)
        """
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding='utf-8', errors='replace',
            )
        except subprocess.TimeoutExpired:
            self.signals.log.emit(f'{label} · zaman aşımı', 'err')
            return None
        except OSError as e:
            self.signals.log.emit(f'{label} · {e}', 'err')
            return None

        # stdout satırları
        for line in (result.stdout or '').splitlines():
            line = line.strip()
            if line:
                self.signals.log.emit(f'{label} › {line}', kind)
        # stderr satırları (uyarı/hata)
        for line in (result.stderr or '').splitlines():
            line = line.strip()
            if line:
                self.signals.log.emit(f'{label} ‼ {line}', 'err')

        if result.returncode != 0 and not allow_nonzero:
            self.signals.log.emit(
                f'{label} · exit={result.returncode}', 'err',
            )
        elif (
            not silent_empty
            and not (result.stdout or result.stderr)
            and result.returncode == 0
        ):
            self.signals.log.emit(f'{label} · ok (exit 0)', 'ok')
        return result

    # ═══ Aksiyonlar ═══════════════════════════════════════════════════
    def open_website(self):
        """Tarayıcıda segemacro.com aç. Başarısız olursa kullanıcıya manuel link göster."""
        try:
            webbrowser.open('https://www.segemacro.com')
        except Exception:
            QMessageBox.information(self, self.t('info_title'), self.t('web_open_fail'))

    def select_iso(self):
        """Dosya seçici aç → seçilen ISO yolunu LineEdit'e yaz."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.t('iso_section').replace('// ', ''), '',
            'ISO files (*.iso);;All files (*.*)',
        )
        if path:
            self.iso_entry.setText(path)
            self._push_log(self.t('iso_log', name=os.path.basename(path)), 'info')

    # ── Plan üretimi (batch ya da custom) ─────────────────────────
    def _build_plan(self) -> list:
        """Kuruluma girecek VM listesini hazırla.

        İki mod aynı şekle indirgenir: ``[(name, ram_str, cpu_int, hdd_int), ...]``.
        Toplu modda count + base name'den otomatik üretilir; custom modda
        her VMRow widget'ından okunur.

        Returns:
            VM tanımları listesi. Geçersiz girdide boş liste döner;
            ``validate_inputs()`` zaten önceden çağrılmış olmalı.
        """
        if self.mode == 'batch':
            try:
                count = int(self.count_entry.text())
            except ValueError:
                return []
            base = self.name_entry.text().strip()
            return [
                (f'{base}-{i}', self.ram_choice, int(self.cpu_choice),
                 self.hdd_slider.value())
                for i in range(1, count + 1)
            ]
        plan = []
        for row in self._custom_rows():
            name, ram, cpu, hdd = row.values()
            if not name:
                continue
            plan.append((name, ram, cpu, hdd))
        return plan

    def validate_inputs(self) -> bool:
        """Kullanıcı girdilerini kontrol et; hatada kullanıcıya bildir.

        ISO yolunun varlığı, sayı/isim alanları, disk minimumu, custom
        modda mükerrer isim kontrolü. Hata bulunursa diyalog açar,
        log'a hata satırı yazar ve False döner.
        """
        if not self.iso_entry.text():
            return self._error(self.t('iso_required'))
        if not os.path.exists(self.iso_entry.text()):
            return self._error(self.t('iso_missing'))

        if self.mode == 'batch':
            try:
                if int(self.count_entry.text()) < 1:
                    return self._error(self.t('count_min'))
            except ValueError:
                return self._error(self.t('count_invalid'))
            if not self.name_entry.text().strip():
                return self._error(self.t('name_required'))
            if self.hdd_slider.value() < 10:
                return self._error(self.t('disk_min'))
            return True

        # custom
        plan = self._build_plan()
        if not plan:
            return self._error(self.t('custom_empty'))
        seen = set()
        for name, _, _, hdd in plan:
            if not name:
                return self._error(self.t('name_required'))
            if hdd < 10:
                return self._error(self.t('disk_min'))
            if name in seen:
                return self._error(self.t('custom_dup') + name)
            seen.add(name)
        return True

    def _error(self, msg: str) -> bool:
        QMessageBox.critical(self, self.t('error_title'), msg)
        self._push_log(msg, 'err')
        return False

    # ═══ Kurulum ══════════════════════════════════════════════════════
    def start_installation(self):
        """ÇALIŞTIR butonuna tıklanınca tetiklenen ana giriş noktası.

        Doğrulama yapar, kullanıcıdan onay alır, butonları kilitler ve
        ağır işi `_installation_thread` içinde arka plan thread'ine atar
        (UI bloklanmasın diye).
        """
        if self._busy or not self.validate_inputs():
            return
        plan = self._build_plan()
        if self.mode == 'batch':
            confirm = self.t(
                'confirm_batch',
                n=len(plan),
                name=self.name_entry.text(),
                hdd=self.hdd_slider.value(),
                ram=self.ram_choice,
                cpu=self.cpu_choice,
            )
        else:
            confirm = self.t('confirm_custom', n=len(plan))
        if QMessageBox.question(
            self, self.t('confirm_title'), confirm,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._busy = True
        self.execute_btn.setEnabled(False)
        self.execute_btn.setText(self.t('forging'))
        self.bypass_btn.setEnabled(False)
        self.signals.state.emit('running')
        self._set_progress(0)
        threading.Thread(
            target=self._installation_thread, args=(plan,), daemon=True,
        ).start()

    def _installation_thread(self, plan: list):
        """Asıl kurulum işi — arka plan thread'inde çalışır.

        Her VM için sırayla:
          1. ~/Documents/Virtual Machines/<ad>/ klasörü oluştur
          2. (varsa) iso.xml şablonundan ComputerName=<ad> ile özelleştirilmiş
             autounattend.xml içeren bir <ad>.flp floppy yaz
          3. <ad>.vmx dosyasını yaz (donanım + ISO + floppy referansı)
          4. <ad>.vmdk sanal diski vmware-vdiskmanager ile yarat
          5. VMware penceresini aç (vmware.exe Popen)
          6. vmrun start ile VM'e güç ver
          7. UI rafına kart ekle

        Hata olursa exception yakalanıp `error` sinyali ile diyaloğa
        çıkar; her durumda butonlar `reset_btn` ile yeniden açılır.
        UI'ya doğrudan dokunmaz; her güncelleme `self.signals.*.emit()`
        üzerinden ana thread'e gönderilir.
        """
        try:
            if not (os.path.exists(self.VMRUN_PATH) or os.path.exists(self.VMWARE_PATH)):
                self.signals.log.emit('vmware not found on host', 'err')
                self.signals.error.emit(self.t('error_title'), self.t('vmware_missing'))
                return
            iso_path = self.iso_entry.text()
            vm_root = os.path.join(os.path.expanduser('~'), 'Documents', 'Virtual Machines')
            os.makedirs(vm_root, exist_ok=True)

            # autounattend.xml şablonunu bir kez yükle
            unattend_template = self._load_unattend_template()
            if unattend_template:
                self.signals.log.emit(self.t('unattend_found'), 'sys')
            else:
                self.signals.log.emit(self.t('unattend_missing'), 'sys')

            self.signals.log.emit(self.t('init_log', n=len(plan)), 'cmd')

            for i, (vm_name, ram_str, cpu_n, hdd_gb) in enumerate(plan, start=1):
                ram_mb = self.RAM_MB.get(ram_str, 2048)
                vm_path = os.path.join(vm_root, vm_name)
                self.signals.progress.emit(i / len(plan))
                self.signals.status.emit(f'🔨 {vm_name} ({i}/{len(plan)})')

                self.signals.log.emit(self.t('alloc_log', name=vm_name), 'info')
                os.makedirs(vm_path, exist_ok=True)

                # Otomatik kurulum için floppy yaz
                floppy_name = ''
                if unattend_template:
                    try:
                        floppy_name = self._write_unattend_floppy(
                            vm_path, vm_name, unattend_template,
                        )
                        self.signals.log.emit(
                            self.t('unattend_prepared', name=vm_name), 'info',
                        )
                        self.signals.log.emit(
                            self.t('floppy_written', name=vm_name), 'ok',
                        )
                    except OSError as e:
                        self.signals.log.emit(f'floppy fail · {e}', 'err')
                        floppy_name = ''

                vmx_path = os.path.join(vm_path, f'{vm_name}.vmx')
                with open(vmx_path, 'w', encoding='utf-8') as f:
                    f.write(self._vmx_content(
                        vm_name, iso_path, ram_mb, hdd_gb, cpu_n, floppy_name,
                    ))

                vmdk_path = os.path.join(vm_path, f'{vm_name}.vmdk')
                if os.path.exists(self.VDISK_PATH):
                    self.signals.log.emit(self.t('vmdk_log', hdd=hdd_gb), 'info')
                    # -a lsilogic: SCSI LSI Logic adapter (.vmx ile uyumlu)
                    # -t 0: tek dosyalı, growable virtual disk
                    self._run_logged(
                        [self.VDISK_PATH, '-c', '-s', f'{hdd_gb}GB',
                         '-a', 'lsilogic', '-t', '0', vmdk_path],
                        label=f'vmdk[{vm_name}]',
                    )

                self.signals.log.emit(
                    self.t('mount_log', name=os.path.basename(iso_path)), 'info',
                )
                if os.path.exists(self.VMWARE_PATH):
                    try:
                        proc = subprocess.Popen(
                            [self.VMWARE_PATH, vmx_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        self.signals.log.emit(
                            f'vmware.exe başlatıldı · pid {proc.pid} · '
                            f'Windows kurulumu sanal pencerede sürüyor', 'sys',
                        )
                        time.sleep(1)
                    except OSError as e:
                        self.signals.log.emit(f'vmware launch warn · {e}', 'err')
                if os.path.exists(self.VMRUN_PATH):
                    self.signals.log.emit(f'vmrun start · {vm_name}', 'cmd')
                    self._run_logged(
                        [self.VMRUN_PATH, 'start', vmx_path],
                        label=f'vmrun[{vm_name}]',
                    )
                    # Çalışan VM listesini doğrula
                    res = self._run_logged(
                        [self.VMRUN_PATH, 'list'],
                        label='vmrun-list', kind='info', silent_empty=True,
                    )
                    if res and res.stdout and vm_name in res.stdout:
                        self.signals.log.emit(
                            f'vmrun · {vm_name} listede görünüyor', 'ok',
                        )

                self.signals.log.emit(self.t('boot_log', name=vm_name), 'ok')
                self.signals.vm_added.emit(vm_name, ram_str, hdd_gb, cpu_n)
                time.sleep(0.4)

            self.signals.progress.emit(1.0)
            self.signals.log.emit(self.t('done_log', n=len(plan)), 'ok')
            self.signals.status.emit(f'✅ {len(plan)} VM')
            self.signals.state.emit('done')
            self.signals.finished.emit(
                self.t('success_title'),
                self.t('install_done', n=len(plan)),
            )
        except Exception as e:
            self.signals.log.emit(f'exception · {e}', 'err')
            self.signals.error.emit(
                self.t('error_title'), self.t('install_error', msg=str(e)),
            )
        finally:
            self.signals.reset_btn.emit()

    @staticmethod
    def _vmx_content(vm_name: str, iso_path: str, ram_mb: int,
                     hdd_gb: int, cpu_n: int,
                     floppy_name: str = '') -> str:
        """Tek bir VM için tam .vmx config dosyasının içeriğini üret.

        Üretilen config:
          - guestOS = "windows9-64" (Win10/11; eski Win7-Win8.1 ISO'ları
            da bu altında sorunsuz boot eder)
          - SCSI controller (LSI Logic Parallel) + sanal disk (scsi0:0)
            → BYPASS_KEYS'teki scsi0:0.productID/vendorID anahtarları
              etkili olur (IDE'de yok sayılıyordu)
          - IDE üzerinde sadece kurulum ISO'su (ide1:0 cdrom-image)
          - NAT ağ (e1000 sürücüsü)
          - 3D grafik aktif, 256 MB SVGA VRAM
          - cdrom,hdd boot order (kurulumun ISO'dan başlaması için)
          - floppy_name verilmişse floppy0 da iliştirilir (autounattend için)

        Bypass adımı `bios.bootOrder`'ı sonradan "hdd,cdrom"'a çevirir
        ve `ide1:0.startConnected = FALSE` yapar.

        Args:
            vm_name: VM ve dosya adı (klasör adıyla aynı tutulur)
            iso_path: Bağlanacak Windows ISO'sunun tam yolu
            ram_mb: Bellek (MB)
            hdd_gb: Disk (GB) — yalnızca log için, asıl disk vmdk yaratılırken
            cpu_n: vCPU çekirdek sayısı (numvcpus + coresPerSocket aynı yapılır)
            floppy_name: Aynı klasördeki .flp dosyasının adı; boş ise
                         floppy bağlanmaz (manuel kurulum modu)

        Returns:
            UTF-8 metin olarak hazır .vmx içeriği
        """
        iso_safe = iso_path.replace('\\', '/')
        floppy_block = ''
        if floppy_name:
            floppy_block = (
                'floppy0.present = "TRUE"\n'
                'floppy0.fileType = "file"\n'
                f'floppy0.fileName = "{floppy_name}"\n'
                'floppy0.startConnected = "TRUE"\n'
                'floppy0.clientDevice = "FALSE"\n'
            )
        return (
            '.encoding = "UTF-8"\n'
            'config.version = "8"\n'
            'virtualHW.version = "16"\n'
            f'memsize = "{ram_mb}"\n'
            f'numvcpus = "{cpu_n}"\n'
            f'cpuid.coresPerSocket = "{cpu_n}"\n'
            f'displayName = "{vm_name}"\n'
            'guestOS = "windows9-64"\n'
            # ── SCSI controller + sistem diski (scsi0:0) ──────────────
            # LSI Logic Parallel: Win 7/8.1/10/11 default'ta destekler,
            # ekstra driver disk gerekmez.
            'scsi0.present = "TRUE"\n'
            'scsi0.virtualDev = "lsilogic"\n'
            'scsi0:0.present = "TRUE"\n'
            f'scsi0:0.fileName = "{vm_name}.vmdk"\n'
            'scsi0:0.deviceType = "scsi-hardDisk"\n'
            # ── IDE: sadece kurulum ISO'su (ide1:0 cdrom) ─────────────
            'ide1:0.present = "TRUE"\n'
            'ide1:0.deviceType = "cdrom-image"\n'
            f'ide1:0.fileName = "{iso_safe}"\n'
            'ide1:0.startConnected = "TRUE"\n'
            'ide1:0.autodetect = "TRUE"\n'
            + floppy_block +
            'ethernet0.present = "TRUE"\n'
            'ethernet0.virtualDev = "e1000"\n'
            'ethernet0.networkName = "NAT"\n'
            'ethernet0.startConnected = "TRUE"\n'
            'bios.bootOrder = "cdrom,hdd"\n'
            'bios.bootDelay = "3000"\n'
            'sound.present = "TRUE"\n'
            'usb.present = "TRUE"\n'
            'mks.enable3d = "TRUE"\n'
            'mks.use3dRenderer = "automatic"\n'
            'svga.autodetect = "TRUE"\n'
            'svga.vramSize = "268435456"\n'
            'accelerate3d.enable = "TRUE"\n'
        )

    # ═══ Unattended Windows kurulumu (autounattend.xml + floppy) ═════
    # Windows Setup ISO'dan boot ettiğinde tüm removable sürücülerin
    # kökünde 'autounattend.xml' arar. Programın yanında `iso.xml`
    # şablonu varsa, her VM için içeriği özelleştirip 1.44 MB FAT12
    # floppy imajına paketliyor; .vmx'e floppy0 olarak iliştiriyoruz.
    # Bu sayede HERHANGİ bir Microsoft ISO'su (Win7-Win10/11 Server)
    # soru sormadan otomatik kurulur. iso.xml yoksa manuel kuruluma
    # düşer.

    def _unattend_template_path(self) -> Path:
        """iso.xml'in beklenen tam yolunu döndür (script ile aynı klasör)."""
        return script_dir() / 'iso.xml'

    def _load_unattend_template(self):
        """iso.xml dosyasını oku; yoksa veya okunamazsa None döndür.

        None dönmesi 'manuel kurulum modu' demektir — kurulum thread'i
        floppy üretmez, .vmx'e floppy0 yazmaz.
        """
        path = self._unattend_template_path()
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding='utf-8')
        except OSError as e:
            self.signals.log.emit(f'iso.xml read fail · {e}', 'err')
            return None

    @staticmethod
    def _customize_unattend(template: str, vm_name: str) -> str:
        """Şablonun <ComputerName> alanını VM adıyla değiştir.

        Aynı şablonla onlarca VM oluşturulduğunda her birinin ağda
        benzersiz hostname'le açılması için. <ComputerName> tag'i
        şablonda yoksa olduğu gibi döndürülür (kullanıcı isterse
        elle ekler).
        """
        return re.sub(
            r'<ComputerName>[^<]*</ComputerName>',
            f'<ComputerName>{vm_name}</ComputerName>',
            template,
        )

    def _write_unattend_floppy(
        self, vm_path: str, vm_name: str, template: str,
    ) -> str:
        """ComputerName=vm_name özelleştirmeli .flp dosyasını disk'e yaz.

        Şablonu özelleştir, FAT12 imajı üret, vm_path/<vm_name>.flp olarak
        diske yaz. .vmx içinde floppy0.fileName olarak kullanılacak adı
        (sadece dosya adı, klasörsüz) döndürür — VMware .vmx'in olduğu
        klasöre relative çözer.

        Args:
            vm_path: VM'in klasör yolu (.vmx ile aynı yer)
            vm_name: ComputerName olarak yazılacak ad ve dosya prefix'i
            template: iso.xml içeriği (zaten yüklenmiş)

        Returns:
            Yazılan floppy'nin dosya adı (örn. "PC-1.flp")
        """
        xml = self._customize_unattend(template, vm_name)
        floppy_name = f'{vm_name}.flp'
        floppy_path = os.path.join(vm_path, floppy_name)
        img = build_autounattend_floppy(xml)
        with open(floppy_path, 'wb') as f:
            f.write(img)
        return floppy_name

    # ═══ Bypass ═══════════════════════════════════════════════════════
    def start_bypass(self):
        """⚡ BYPASS ET butonuna tıklanınca tetiklenen anti-detection akışı.

        Akış: VMware Tools onayı → hedef VM listesini belirle (toplu modda
        base name, custom modda her satırın adı) → arka plan thread'inde
        her VM'i kapat, .vmx'e bypass anahtarlarını ekle, ISO'yu çıkar.
        """
        if self._busy:
            return
        # Hedef belirleme:
        #  - Toplu mod: kullanıcı "PC" girmişse, "PC-1, PC-2..." prefix'iyle eşleş
        #  - Custom mod: VMRow isimlerinin ortak prefix'ini hesapla; thread daha sonra
        #    isimleri set olarak kullanıp birebir eşleştirme yapar
        if self.mode == 'batch':
            target = self.name_entry.text().strip()
        else:
            rows = self._custom_rows()
            target = self._common_prefix([r.values()[0] for r in rows if r.values()[0]])
        if not target:
            return self._error(self.t('name_required'))

        if QMessageBox.question(
            self, self.t('tools_title'), self.t('tools_question'),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            QMessageBox.information(self, self.t('info_title'), self.t('tools_first'))
            return
        self._busy = True
        self.execute_btn.setEnabled(False)
        self.bypass_btn.setEnabled(False)
        self.signals.state.emit('running')
        self._set_progress(0)
        threading.Thread(
            target=self._bypass_thread, args=(target,), daemon=True,
        ).start()

    @staticmethod
    def _common_prefix(names: list) -> str:
        """İsim listesinin ortak prefix'ini bul ('-' eki kırpılır).

        Custom modda kullanıcı ['Web-1', 'Web-2', 'Web-3'] yazdıysa
        'Web' döner. Tek isim varsa ilk '-' segmentini al. Boş listede
        boş string. Bu prefix bypass thread'inde "X- ile başlayan VM
        klasörlerini hedefle" şeklinde kullanılır.
        """
        if not names:
            return ''
        if len(names) == 1:
            return names[0].split('-')[0]
        # En küçük ve en büyük string'i karşılaştırarak ortak başlangıç bul
        # (sözlük sırasında ortada kalanlar bu ikisinin aralarında olur)
        s1, s2 = min(names), max(names)
        i = 0
        while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
            i += 1
        prefix = s1[:i].rstrip('-')
        return prefix or s1.split('-')[0]

    # ── VMware Anti-Detection Anahtarları ─────────────────────────
    # Bypass tıklanınca her .vmx dosyasının sonuna bu blok eklenir.
    # Detaylı açıklamalar için repo'daki docs/bypass.md dosyasına bakın.
    # Özet:
    #   - hypervisor.cpuid.v0     → CPUID hypervisor bit'i gizle
    #   - *.reflectHost           → SMBIOS host bilgisini yansıt
    #   - SMBIOS.noOEMStrings     → "VMware" OEM string'i temizle
    #   - isolation.tools.*       → Tools introspection API'lerini kapat
    #   - monitor_control.disable_*  → BT diagnostics kapat
    #   - monitor_control.restrict_backdoor + disable_btinout → 0x5658 backdoor kapat
    #   - scsi0:0.{productID,vendorID} → SCSI disk model spoof
    #     (Not: program IDE disk kullanıyor; bu iki satır mevcut VM'lere etki etmez,
    #      ileride SCSI'ye dönüldüğünde lazım olur)
    BYPASS_KEYS = (
        '\nhypervisor.cpuid.v0 = "FALSE"'
        '\nboard-id.reflectHost = "TRUE"'
        '\nhw.model.reflectHost = "TRUE"'
        '\nserialNumber.reflectHost = "TRUE"'
        '\nsmbios.reflectHost = "TRUE"'
        '\nSMBIOS.noOEMStrings = "TRUE"'
        '\nisolation.tools.getPtrLocation.disable = "TRUE"'
        '\nisolation.tools.setPtrLocation.disable = "TRUE"'
        '\nisolation.tools.setVersion.disable = "TRUE"'
        '\nisolation.tools.getVersion.disable = "TRUE"'
        '\nmonitor_control.disable_directexec = "TRUE"'
        '\nmonitor_control.disable_chksimd = "TRUE"'
        '\nmonitor_control.disable_ntreloc = "TRUE"'
        '\nmonitor_control.disable_selfmod = "TRUE"'
        '\nmonitor_control.disable_reloc = "TRUE"'
        '\nmonitor_control.disable_btinout = "TRUE"'
        '\nmonitor_control.disable_btmemspace = "TRUE"'
        '\nmonitor_control.disable_btpriv = "TRUE"'
        '\nmonitor_control.disable_btseg = "TRUE"'
        '\nmonitor_control.restrict_backdoor = "TRUE"'
        # Disk kimliği: VM artık SCSI olarak üretildiği için bu satırlar
        # Device Manager'da gerçekten "Samsung 970 EVO Plus" olarak görünür.
        '\nscsi0:0.productID = "970 EVO Plus"'
        '\nscsi0:0.vendorID = "Samsung"'
    )

    def _bypass_thread(self, target_prefix: str):
        """Asıl bypass işi — arka plan thread'inde çalışır.

        Akış:
          1. ~/Documents/Virtual Machines/ altında hedef VM klasörlerini bul
             (toplu modda prefix-based, custom modda isim setine göre)
          2. Her VM için:
             a. vmrun stop ile çalışıyor olabilen VM'i kapat
             b. .vmx içeriğini oku
             c. BYPASS_KEYS bloğunu ekle (zaten varsa atla)
             d. Boot ayarlarını güncelle: ISO'yu söker, HDD-öncelikli boot
             e. .vmx'i geri yaz
          3. Sonuç diyalogu göster

        UI'ya yine sinyal-aracılığıyla dokunur.
        """
        try:
            vm_root = os.path.join(os.path.expanduser('~'), 'Documents', 'Virtual Machines')
            if not os.path.isdir(vm_root):
                self.signals.error.emit(self.t('error_title'), self.t('no_vm_folder'))
                return
            # Custom modda her satır ismi tek başına da var olabilir
            if self.mode == 'custom':
                expected = {r.values()[0] for r in self._custom_rows() if r.values()[0]}
                vm_dirs = [d for d in os.listdir(vm_root) if d in expected]
            else:
                vm_dirs = [
                    d for d in os.listdir(vm_root)
                    if d.startswith(target_prefix + '-') or d == target_prefix
                ]
            if not vm_dirs:
                self.signals.error.emit(
                    self.t('error_title'), self.t('no_match_vm', base=target_prefix),
                )
                return

            self.signals.log.emit(self.t('bypass_init_log', n=len(vm_dirs)), 'cmd')
            processed = 0
            for i, vm_name in enumerate(vm_dirs):
                vmx = os.path.join(vm_root, vm_name, f'{vm_name}.vmx')
                if not os.path.exists(vmx):
                    continue
                self.signals.progress.emit((i + 1) / len(vm_dirs))
                self.signals.status.emit(f'🔧 {vm_name}')

                if os.path.exists(self.VMRUN_PATH):
                    self.signals.log.emit(self.t('stop_log', name=vm_name), 'cmd')
                    self._run_logged(
                        [self.VMRUN_PATH, 'stop', vmx],
                        label=f'vmrun-stop[{vm_name}]',
                        timeout=10, allow_nonzero=True,
                    )
                    time.sleep(1)

                try:
                    with open(vmx, 'r', encoding='utf-8') as f:
                        content = f.read()
                    if 'hypervisor.cpuid.v0' not in content:
                        content += self.BYPASS_KEYS
                        self.signals.log.emit(self.t('inject_log', name=vm_name), 'info')
                    new_lines = []
                    for line in content.split('\n'):
                        if 'ide1:0.startConnected' in line:
                            new_lines.append('ide1:0.startConnected = "FALSE"')
                        elif 'bios.bootOrder' in line:
                            new_lines.append('bios.bootOrder = "hdd,cdrom"')
                        elif line.startswith('ide1:0.present') and 'TRUE' in line:
                            new_lines.append('ide1:0.present = "FALSE"')
                        elif 'bios.bootDelay' in line:
                            new_lines.append('bios.bootDelay = "1000"')
                        else:
                            new_lines.append(line)
                    with open(vmx, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(new_lines))
                    self.signals.log.emit(self.t('eject_log', name=vm_name), 'ok')
                    processed += 1
                except OSError as e:
                    self.signals.log.emit(f'io fail · {vm_name} · {e}', 'err')
                    continue
                time.sleep(0.15)

            self.signals.progress.emit(1.0)
            self.signals.status.emit(f'🎉 {processed} VM')
            self.signals.state.emit('done')
            if processed > 0:
                self.signals.log.emit(
                    self.t('bypass_done_log', n=processed), 'ok',
                )
                self.signals.finished.emit(
                    self.t('success_title'), self.t('bypass_done', n=processed),
                )
            else:
                self.signals.finished.emit(
                    self.t('info_title'), self.t('no_vm_processed'),
                )
        except Exception as e:
            self.signals.log.emit(f'exception · {e}', 'err')
            self.signals.error.emit(
                self.t('error_title'), self.t('bypass_error', msg=str(e)),
            )
        finally:
            self.signals.reset_btn.emit()


# ════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════
def _pick_mono_font() -> str:
    """Sistemde kurulu monospace yazı tiplerinden CSS family-list'i üret.

    Tercih sırası: JetBrains Mono > Fira Code > Cascadia Code > Consolas
    > Courier New > Menlo > generic monospace. İlk bulunanı döndürür;
    hiçbiri yoksa Consolas'a düşer (Windows'ta her zaman var).
    """
    families = QFontDatabase().families()
    for candidate in ('JetBrains Mono', 'Fira Code', 'Cascadia Code',
                      'Consolas', 'Courier New', 'Menlo', 'monospace'):
        if candidate in families or candidate == 'monospace':
            return f'"{candidate}", monospace'
    return '"Consolas", monospace'


if __name__ == '__main__':
    # Qt uygulamasını ayağa kaldır, monospace QSS ile renklendir, ana
    # pencereyi göster ve event loop'a gir.
    app = QApplication(sys.argv)
    mono = _pick_mono_font()
    app.setStyleSheet(cyber_qss(mono))
    win = SegeSystemsGUI()
    win.show()
    sys.exit(app.exec_())
