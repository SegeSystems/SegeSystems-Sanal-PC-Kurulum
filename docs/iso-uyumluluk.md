# ISO Uyumluluk Listesi

Mevcut `iso.xml` şablonu **Türkçe x64 BIOS/MBR** kurulumu için yazılmıştır. Bu sayfa hangi ISO'larla doğrudan çalıştığını, hangileri için XML'i değiştirmen gerektiğini, hangilerinin çalışmadığını listeler.

## Mevcut iso.xml'in Varsayımları

```
processorArchitecture = "amd64"      → 64-bit
UILanguage = "tr-TR"                  → Türkçe dil paketi
InputLocale = "041f:0000041f"         → Türkçe Q klavye
WillWipeDisk = true                   → Disk 0 tamamen silinir
Partition layout: 100MB sys + uzayan  → MBR/BIOS
IMAGE/INDEX = 1                       → ISO'daki ilk edisyon
TimeZone = "Turkey Standard Time"     → UTC+3
```

---

## ✅ Doğrudan Çalışan ISO'lar

| ISO | Test Durumu | Notlar |
|---|---|---|
| **Windows 7 SP1 x64 Türkçe** | ✅ Test edildi | İlk hedef sürüm |
| **Windows 8.1 x64 Türkçe** | ✅ Beklenir | XML uyumlu |
| **Windows 10 1903 / 1909 x64 TR** | ✅ | |
| **Windows 10 20H2 / 21H1 / 21H2 x64 TR** | ✅ | |
| **Windows 10 22H2 x64 TR** | ✅ | En son Win10 |
| **Windows Server 2012 R2 x64 TR** | ✅ | |
| **Windows Server 2016 x64 TR** | ✅ | |
| **Windows Server 2019 x64 TR** | ✅ | |
| **Windows Server 2022 x64 TR** | ✅ | |

---

## ⚠️ Düzenleme Gerekiyor

### Windows 11 (21H2 / 22H2 / 23H2)

**Sorun:** TPM 2.0 + Secure Boot kontrolü.

**Çözüm:** `iso.xml`'in `<settings pass="windowsPE">` içine ekle (yeni component):

```xml
<component name="Microsoft-Windows-Setup" processorArchitecture="amd64" ...>
    <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
            <Order>1</Order>
            <Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
            <Order>2</Order>
            <Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
            <Order>3</Order>
            <Path>reg add HKLM\SYSTEM\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
    </RunSynchronous>
    <UserData>...</UserData>
    ...
</component>
```

### Windows 11 24H2

**Sorun:** UEFI/GPT zorunlu — mevcut XML MBR yazıyor.

**Çözüm:** Tüm `<DiskConfiguration>` bloğunu UEFI partition layout ile değiştir:

```xml
<DiskConfiguration>
    <Disk wcm:action="add">
        <DiskID>0</DiskID>
        <WillWipeDisk>true</WillWipeDisk>
        <CreatePartitions>
            <CreatePartition wcm:action="add">
                <Order>1</Order>
                <Type>EFI</Type>
                <Size>260</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
                <Order>2</Order>
                <Type>MSR</Type>
                <Size>16</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
                <Order>3</Order>
                <Type>Primary</Type>
                <Extend>true</Extend>
            </CreatePartition>
        </CreatePartitions>
        <ModifyPartitions>
            <ModifyPartition wcm:action="add">
                <Order>1</Order><PartitionID>1</PartitionID>
                <Format>FAT32</Format><Label>System</Label>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
                <Order>2</Order><PartitionID>2</PartitionID>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
                <Order>3</Order><PartitionID>3</PartitionID>
                <Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter>
            </ModifyPartition>
        </ModifyPartitions>
    </Disk>
</DiskConfiguration>
```

Ek olarak `.vmx`'e `firmware = "efi"` eklenmesi gerekebilir.

### İngilizce ISO'lar (en-us, en-gb)

`iso.xml`'de **dört** yerde `tr-TR`'yi değiştir + InputLocale:

```xml
<UILanguage>en-US</UILanguage>            ×3
<UserLocale>en-US</UserLocale>            ×2
<InputLocale>0409:00000409</InputLocale>  ×2  (US klavye)
<TimeZone>UTC</TimeZone>                  ×1
```

InputLocale kodları:
- `0409:00000409` — English (US)
- `0809:00000809` — English (UK)
- `0407:00000407` — German
- `040c:0000040c` — French
- `0410:00000410` — Italian
- `040a:0000040a` — Spanish
- `0419:00000419` — Russian

### Çok Edisyonlu ISO'lar

ISO'da birden fazla Windows edisyonu varsa `IMAGE/INDEX` hangisini kuracağını belirler. ISO'yu mount edip listeyi gör:

```cmd
dism /Get-WimInfo /WimFile:E:\sources\install.wim
```

Çıktı genelde böyle:
```
Index : 1   Name : Windows 10 Home
Index : 2   Name : Windows 10 Home N
Index : 3   Name : Windows 10 Pro
Index : 4   Name : Windows 10 Pro N
Index : 5   Name : Windows 10 Education
Index : 6   Name : Windows 10 Enterprise
```

İstediğini `<Value>3</Value>` yap. Veya isimle eşleştir:
```xml
<InstallFrom>
    <MetaData wcm:action="add">
        <Key>/IMAGE/NAME</Key>
        <Value>Windows 10 Pro</Value>
    </MetaData>
</InstallFrom>
```

### Çok Dilli ISO'lar (multi-edition + multi-language)

Bu ISO'larda dil paketi seçimi vardır. `iso.xml`'in ilk component'ini garanti altına almak için:

```xml
<SetupUILanguage>
    <UILanguage>en-US</UILanguage>
</SetupUILanguage>
```

---

## ❌ Çalışmaz

| ISO | Neden |
|---|---|
| **Windows 32-bit (x86)** | `processorArchitecture = "amd64"` yapısı uyumsuz — `x86` yapsan da install.wim 32-bit içeriyor olmalı |
| **Windows ARM64** | `processorArchitecture = "arm64"` yapsan bile guest OS tipi VMware'de değişir |
| **Linux dağıtımları** (Ubuntu, Debian, Arch, Fedora, Rocky) | autounattend.xml Windows'a özgü — Linux için `preseed.cfg` (Debian/Ubuntu) veya `kickstart.cfg` (RHEL/Fedora) kullanılır |
| **macOS / Hackintosh** | Apple tamamen farklı boot stack |
| **Hiren's Boot CD / Strelec WinPE** | İçinde Windows Setup yok — sadece WinPE kurtarma araçları |
| **Tiny11, Ghost Spectre, AtlasOS** modlu Win11 | Genelde kendi unattended yöntemleri var, çakışabilir. Test edip karar ver |
| **OEM recovery ISO** (HP / Dell / Lenovo) | OEM imajlarında autounattend genelde devre dışı |

---

## ISO'yu Test Etme

Yeni bir ISO'yu denemeden önce şunu yap:

1. **WIM içeriğini gör:** `dism /Get-WimInfo /WimFile:<isoroot>\sources\install.wim`
2. **autounattend zaten var mı?** ISO'yu mount et, kökte `autounattend.xml` veya `unattend.xml` ara. Varsa muhtemelen bizimki gerek yok.
3. **Architecture:** `dism /Get-WimInfo` çıktısında `Architecture : x64` veya `arm64` yazar.
4. **Boot type:** ISO'yu BIOS modunda boot etmeyi deniyorsa MBR. UEFI istiyorsa GPT layout gerekir.

## Profil Sistemi (Geliştirme Roadmap'inde)

`iso.xml` yerine `profiles/` klasörü altında çoklu profil:

```
profiles/
├── win10-tr-bios.xml
├── win10-en-bios.xml
├── win11-tr-uefi.xml      (TPM bypass + GPT)
├── win11-en-uefi.xml
├── server2022-tr.xml
└── win7-tr-bios.xml
```

GUI'den dropdown ile profil seçilir. Bu özelliği isteyen [issue açabilir](../).
