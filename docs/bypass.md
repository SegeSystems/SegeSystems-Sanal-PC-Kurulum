# VMware Bypass — Anti-Detection Anahtarları

`⚡ BYPASS ET` butonu, oluşturulmuş tüm VM'lerin `.vmx` dosyalarına 22 satır anti-detection anahtarı enjekte eder. Bu sayfa **her satırın ne yaptığını** ve **gerçekçi olarak neye karşı koruduğunu** açıklar.

⚠️ **Yasal Uyarı:** Bu liste araştırma, malware analizi, ve test amaçlıdır. Anti-cheat ihlali, lisans kötüye kullanımı gibi senaryolardaki sorumluluk kullanıcıya aittir.

---

## Enjekte Edilen Satırlar

### 1) CPUID Hypervisor Flag

```
hypervisor.cpuid.v0 = "FALSE"
```

**Ne yapar:** CPU'nun hypervisor altında çalıştığını gösteren `CPUID.1.ECX[31]` bitini gizler. `IsProcessorFeaturePresent` ve `__cpuid(0x40000000)` çağrıları "çıplak metal" gibi cevap verir.

**Geçtiği:** En yaygın anti-VM kontrolü. Pafish, Al-Khaser, çoğu malware sandbox detection.

---

### 2) SMBIOS Reflect

```
board-id.reflectHost = "TRUE"
hw.model.reflectHost = "TRUE"
serialNumber.reflectHost = "TRUE"
smbios.reflectHost = "TRUE"
SMBIOS.noOEMStrings = "TRUE"
```

**Ne yapar:** VM'in SMBIOS bilgilerini host'unkilerle eşleştirir. WMI sorguları:
- `SELECT * FROM Win32_BIOS` → host BIOS bilgisi
- `SELECT * FROM Win32_ComputerSystem` → host model
- `Win32_BaseBoard` → host anakart
- OEM string'lerden VMware logosu temizlenir

**Geçtiği:** WMI tabanlı VM kontrolü, ürün anahtarı / hardware ID kontrolleri.

**Geçmediği:** GPU PCI ID (VMware SVGA), network adapter device ID (vmxnet3 / e1000), TPM yokluğu.

---

### 3) VMware Tools API Kapatma

```
isolation.tools.getPtrLocation.disable = "TRUE"
isolation.tools.setPtrLocation.disable = "TRUE"
isolation.tools.setVersion.disable = "TRUE"
isolation.tools.getVersion.disable = "TRUE"
```

**Ne yapar:** VMware Tools'un guest'e açtığı introspection API'lerini kapatır. Guest, kendi VMware Tools sürümünü sorgularken hata alır.

**Geçtiği:** "Eğer VMware Tools API çağrısı çalışıyorsa = bu bir VM" kontrolü.

---

### 4) Binary Translation Diagnostics

```
monitor_control.disable_directexec = "TRUE"
monitor_control.disable_chksimd = "TRUE"
monitor_control.disable_ntreloc = "TRUE"
monitor_control.disable_selfmod = "TRUE"
monitor_control.disable_reloc = "TRUE"
```

**Ne yapar:** VMware'in dinamik kod analizini kapatır. Bazı zırhlı paketleyiciler/anti-cheat'ler bu davranıştan VM tespit eder.

**Geçtiği:** Themida, VMProtect gibi paketleyicilerin agresif anti-VM modu. Bazı eski oyun anti-cheat'leri.

⚠️ **Yan etki:** VM biraz yavaşlayabilir. Performans-kritik iş yüklerinde dikkat.

---

### 5) Backdoor I/O Port Kapatma

```
monitor_control.disable_btinout = "TRUE"
monitor_control.disable_btmemspace = "TRUE"
monitor_control.disable_btpriv = "TRUE"
monitor_control.disable_btseg = "TRUE"
monitor_control.restrict_backdoor = "TRUE"
```

**Ne yapar:** VMware backdoor port (`0x5658`, "VX") guest'ten erişilemez olur. Bu port host ile guest arası özel kanaldır; pek çok VM detection aracı `IN EAX, DX` ile bu porta sorgu atar ve cevap gelirse "VMware!" der.

**Geçtiği:** Klasik VMware detection (Joanna Rutkowska "Red Pill" varyantları), Pafish'in backdoor testi, VMDE.

⚠️ **Yan etki:** VMware Tools'un bazı entegrasyon özellikleri (drag-drop, copy-paste, shared folders) bozulabilir. Tools'u kurduktan sonra bypass etmek bu yüzden önemli.

---

### 6) SCSI Disk Kimliği Yansıtma

```
scsi0:0.productID = "970 EVO Plus"
scsi0:0.vendorID = "Samsung"
```

**Ne yapar:** SCSI diskin model/üretici stringini değiştirir. Bypass sonrası Aygıt Yöneticisi → Disk Sürücüleri'nde "Samsung 970 EVO Plus" olarak görünür (varsayılan: "VMware Virtual S").

✅ **Çalışıyor:** v1'den itibaren program artık VM'leri SCSI controller (LSI Logic Parallel) ile üretiyor; bu satırlar gerçekten etkili.

📝 **Not:** v1 öncesi sürümlerle (IDE) oluşturulmuş VM'lerde bu satırların etkisi yoktur. Eski VM'leri SCSI'ye migrate etmek için `.vmx`'i elle düzenlemek gerekir (README'de örnek).

---

## Bypass Sonrası Yapılan Diğer İşlemler

```
ide1:0.startConnected = "FALSE"   # ISO'yu çıkar
ide1:0.present = "FALSE"          # CD/DVD sürücüsünü kaldır
bios.bootOrder = "hdd,cdrom"      # HDD'den boot et
bios.bootDelay = "1000"           # 1 saniye boot delay
```

Bu satırlar bypass detection değil, kurulum sonrası temizliktir:
- Windows kurulduktan sonra ISO bağlı kalmasın (yoksa "Press any key to boot from CD" sorunu)
- Boot HDD önceliğine geçsin

---

## Detection Senaryolarına Göre Sonuçlar

### ✅ Yüksek Olasılıkla Geçer
| Test | Bypass Sonucu |
|---|---|
| `Pafish` | %80+ test geçer |
| `VMDE` (VM Detection Evader) | Çoğu testi geçer |
| `Al-Khaser` | Yarısı geçer |
| Basit malware sandbox detection | Geçer |
| WMI manufacturer kontrolü | Geçer |
| Registry "VMware" arama | **Bypass değil — kayıt defterini elle temizlemen gerek** |

### ⚠️ Tartışmalı / Sürüme Bağlı
| Test | Durum |
|---|---|
| FACEIT Anti-Cheat (eski) | Bazı sürümlerde geçer, yenisinde geçmez |
| VAC (Steam) | Genelde algılanmaz, ama oyun bazlı extra kontroller olabilir |
| Riot Vanguard | **Algılanır** — Vanguard kernel-level, VM içinde başlatılmıyor |
| EAC (kernel-mode) | **Algılanır** — modern EAC VM tespiti yapar |
| BattlEye | **Algılanır** — modern sürümler hypervisor flag dışında ek kontroller yapar |

### ❌ Geçmez
| Test | Neden |
|---|---|
| TPM 2.0 varlığı | VMware'de virtual TPM eklenmedikçe yok |
| GPU PCI vendor/device ID | "VMware SVGA II" cihaz string'i değişmiyor |
| Network adapter | `e1000` veya `vmxnet3` host'tan farklı |
| MAC OUI `00:50:56` (VMware) | Bypass etmiyor |
| Hardware breakpoint testleri | Hypervisor seviyesinde değişiklik gerekir |
| Modern Riot/EAC kernel | İçeride başlatma engelli |

---

## MAC Adresi Sorununu Manuel Çözmek

VMware MAC OUI'leri:
- `00:05:69:xx:xx:xx` (VMware ESX Server)
- `00:0C:29:xx:xx:xx` (VMware Workstation)
- `00:50:56:xx:xx:xx` (VMware Workstation manuel)
- `00:1C:14:xx:xx:xx` (VMware Workstation, eski)

Bu OUI'leri tespit eden script'leri atlamak için `.vmx`'e ekle:

```
ethernet0.addressType = "static"
ethernet0.address = "00:1A:2B:3C:4D:5E"
```

(Random gerçek üretici MAC OUI'si seç, mesela `00:1A:2B` Adam Elec.)

---

## Bypass Yetersizse Ek Adımlar

1. **Registry temizliği** (VM içinde):
   ```cmd
   reg delete "HKLM\HARDWARE\DESCRIPTION\System" /v SystemBiosVersion /f
   reg add "HKLM\HARDWARE\DESCRIPTION\System" /v SystemBiosVersion /t REG_MULTI_SZ /d "DELL  - 1072009"
   ```

2. **Service temizliği:**
   ```cmd
   sc config VMTools start= disabled
   sc config vmhgfs start= disabled
   ```

3. **Driver isim değiştirme:** `vmci.sys`, `vmmouse.sys` gibi sürücüleri yeniden adlandır (Tools kaldırıldıktan sonra).

4. **MAC override** (yukarıda).

5. **GPU passthrough** (Workstation Pro 17+) — gerçek GPU'yu VM'e ver, anti-cheat'in PCI vendor sorgusu gerçek donanım görür.

---

## Kaynaklar

- [VMware KB: Configuration parameters](https://kb.vmware.com/s/article/1014180)
- [Pafish (Paranoid Fish)](https://github.com/a0rtega/pafish)
- [Al-Khaser](https://github.com/LordNoteworthy/al-khaser)
- [VMware Detection / Bypass — Awesome list](https://github.com/topics/anti-vm)
