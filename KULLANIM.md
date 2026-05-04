# Hızlı Kullanım Talimatı

## 1) Programı Başlat

```bash
python SegeSystems_SanalPC_Kurulum_v1.py
```

İlk kez kullanıyorsan:
```bash
pip install PyQt5
```

## 2) Sahnenin Adımları

```
┌─────────────────────────────────────────────────────────┐
│  1. ISO seç           → "SEÇ" butonu                     │
│  2. Mod seç           → TOPLU (varsayılan) / CUSTOM     │
│  3. Adet + İsim       → 3, "PC"                          │
│  4. Bellek            → 2GB seç                          │
│  5. İşlemci           → 2 çekirdek                       │
│  6. Disk              → slider 50GB                      │
│  7. ÇALIŞTIR          → Onay → kurulum başlar           │
│  8. (Bekle ~10-30 dk, ISO'ya göre)                       │
│  9. VMware Tools kur  → Her VM'de VM > Install Tools    │
│ 10. BYPASS ET         → Anti-detection enjeksiyonu      │
└─────────────────────────────────────────────────────────┘
```

## 3) Custom Mod ile Karışık VM

Sol panelde **CUSTOM** sekmesi:

| Ad | RAM | CPU | Disk |
|---|---|---|---|
| Web-1 | 2GB | 2 | 30 |
| DB-1 | 4GB | 4 | 100 |
| Cache-1 | 1GB | 1 | 20 |

**+ VM EKLE** ile satır eklenir, `×` ile silinir.

## 4) Otomatik Kurulum Çalışıyor mu?

Programın yanında `iso.xml` dosyası varsa **evet, soru sormadan kurulur**.
- Kullanıcı: `User`
- Şifre: yok (boş)
- Dil: Türkçe
- Saat dilimi: Turkey

`iso.xml` dosyası **yoksa** Windows Setup tüm soruları sana sorar.

## 5) Çıktıyı Nerede Görüyorum?

`~/Documents/Virtual Machines/PC-1/`
```
PC-1.vmx       # VM config
PC-1.vmdk      # Sanal disk
PC-1.flp       # autounattend floppy (program ürettiyse)
PC-1.nvram     # Boot durumu (VMware ürettikten sonra)
```

## 6) Loglar Ne Anlatıyor?

| Renk | Kategori | Anlamı |
|---|---|---|
| 🟢 Yeşil | `OK` | Başarılı adım |
| 🔵 Mavi | `INFO` | Bilgi (alloc, mount, vs.) |
| 🟣 Magenta | `CMD` | Komut çağrısı (vmrun, vmware) |
| 🟡 Sarı | `SYS` | Sistem mesajı (iso.xml bulundu, dil değişti) |
| 🔴 Kırmızı | `ERR` | Hata |

## 7) Önemli — Sırayı Bozma

```
1. KUR (ISO'dan boot, Windows yüklenir)
2. VMware Tools KUR  (VMware menüsünden, Windows içinden)
3. BYPASS ET (anti-detection)
```

Bu sırayı bozma. Tools'u kurmadan bypass yaparsan VMware'in entegrasyonu bozulur ve VM'in penceresi yanıt vermeyebilir.

## 8) Sorun Çıkarsa

`docs/iso-uyumluluk.md` ve ana README'deki "Sorun Giderme" tablosuna bak.

Genel teşhis:
- VMware bulunamadı → kurulu değil ya da farklı yola kurulu
- ISO bulunamadı → yol Türkçe karakter içeriyorsa, `\` yerine `/` dene
- Kurulum donuyor → ISO uyumsuz olabilir, [docs/iso-uyumluluk.md](docs/iso-uyumluluk.md)
- Floppy bağlanmadı → log'da `floppy fail` ara, izin sorunu olabilir

## 9) Manuel Kurulum İstersen

`iso.xml` dosyasını sil veya yeniden adlandır → program manuel moda düşer, Windows tüm soruları sana sorar.
