# Windows Compatibility Layer (Windows 7 / Windows Server 2008 / 2008 R2)

Python 3.9+ / 3.11 requires `api-ms-win-core-path-l1-1-0.dll` (`PathCch*` and `PathAlloc*` APIs).
On legacy Windows versions (Windows Server 2008, Windows Server 2008 R2, Windows 7), this API set does not exist natively in the OS.

The DLLs in this directory are the open-source Wine-based compatibility implementation from:
https://github.com/nalexandru/api-ms-win-core-path-HACK (v0.3.1)

- `compat/x64/api-ms-win-core-path-l1-1-0.dll`: 64-bit implementation
- `compat/x86/api-ms-win-core-path-l1-1-0.dll`: 32-bit implementation

When building `SGAbsensiSimulator.exe`, `compat/x64/api-ms-win-core-path-l1-1-0.dll` is bundled into the PyInstaller onefile package and copied next to the `.exe` in `dist/`.
