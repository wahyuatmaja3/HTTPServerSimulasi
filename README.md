# SGAbsensi Simulator & Load Tester

Aplikasi desktop GUI (PyQt5) untuk mensimulasikan beban request absensi secara massal dan konkuren ke endpoint `HTTPServer` Delphi SJAB, menganalisis kapasitas server, serta merekomendasikan konfigurasi optimal BDE dan `.ini`.

---

## Fitur Utama

1. **High-Concurrency Bulk Simulation**
   - Mensimulasikan perilaku aplikasi Flutter saat melakukan absensi masuk/pulang.
   - Mengirim request `multipart/form-data` dengan payload data absensi dan gambar JPEG yang valid secara asinkron (`aiohttp`).
   - Mendukung ribuan request dengan concurrency terkontrol, proteksi keep-alive socket closing Delphi Indy, dan reporting real-time (Sukses, Gagal, Rata-rata Latensi, Durasi).

2. **Kalkulator Kapasitas & Rekomendasi BDE / INI (0% Gagal)**
   - Otomatis membaca jumlah karyawan dari database.
   - Otomatis membaca konfigurasi aktif BDE (`SHAREDMEMSIZE`, `SHAREDMEMLOCATION`, `MINBUFSIZE`, `MAXBUFSIZE`) dari Registry / `IDAPI32.CFG`.
   - Otomatis membaca konfigurasi `HTTPServer.ini` (`MaxConnections`, `PoolThreads`, `BdePoolSize`, dll).
   - Memiliki 2 opsi checkbox terpisah (**BDE** dan **INI**). Jika hanya mencentang salah satu, rekomendasi disesuaikan tanpa menyarankan perubahan pada parameter yang tidak dicentang.
   - Memberikan kalkulasi rekomendasi parameter presisi agar tingkat kegagalan 0%.

3. **Integrasi Data Karyawan (Paradox & SQLite)**
   - Mampu membaca langsung tabel Paradox (`karyawan.db` / `karyawan.DB`) secara native tanpa dependensi driver BDE eksternal.
   - Mendukung fallback ke database SQLite.

4. **Standalone Portable Executable (.exe)**
   - Dapat dijalankan langsung tanpa perlu menginstal Python di komputer target.
   - Kompatibel penuh dari **Windows Server 2008 / Windows 7** hingga **Windows 10 / 11**.

---

## Persyaratan Sistem

- **Sistem Operasi**: Windows Server 2008 R2 / Windows 7 SP1 / Windows 8.1 / Windows 10 / Windows 11 / Windows Server 2012-2022 (64-bit).
- **Runtime Dependency di Komputer Klien/Server Target**:
  - [Microsoft Visual C++ 2015–2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)

---

## Cara Menjalankan

### 1. Menggunakan Executable Standalone (Langsung Pakai)
Buka folder `dist/` dan jalankan:
```cmd
dist\SGAbsensiSimulator.exe
```

### 2. Menjalankan dari Source Code (Development)
Pastikan Python 3.8 atau 3.11 terpasang.
```cmd
# Install dependensi
pip install -r requirements.txt

# Jalankan simulator
python main.py
# atau
run_simulator.bat
```

---

## Cara Build Standalone Executable (.exe)

Cukup jalankan script batch:
```cmd
build_exe.bat
```
*Catatan:* Script build akan memprioritaskan Python 3.8 jika terpasang di sistem (`py -3.8`) guna memastikan kompatibilitas native pada sistem operasi legacy (Windows Server 2008 / Win7).

Hasil build akan berada di `dist/SGAbsensiSimulator.exe`.

---

## Unit & Integration Tests

Proyek ini dilengkapi dengan 18 unit & integration test komprehensif yang mencakup HTTP flow, Paradox DB reader, Server Advisor calculation, dan GUI smoke testing:
```cmd
python -m unittest discover -s tests -v
```

---

## Struktur Direktori

```
sgabsensi_simulator/
├── compat/              # DLL kompatibilitas legacy Windows
├── config/              # Model konfigurasi aplikasi
├── core/                # Engine simulasi HTTP, generator payload, Paradox reader, server advisor
├── dist/                # Output executable standalone (SGAbsensiSimulator.exe)
├── tables/              # Sample table Paradox/SQLite karyawan
├── tests/               # Unit, E2E, Advisor, dan GUI tests
├── ui/                  # Tampilan desktop GUI PyQt5 dan dialog advisor
├── build_exe.bat        # Script kompilasi PyInstaller
├── main.py              # Entrypoint utama aplikasi GUI
├── requirements.txt     # Daftar dependensi Python
├── run_simulator.bat    # Script cepat menjalankan via Python
├── SGAbsensiSimulator.spec # Konfigurasi build PyInstaller
└── simulator_config.json # Pengaturan default endpoint & simulasi
```
