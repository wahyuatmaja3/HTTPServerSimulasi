import configparser
import ctypes
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import winreg
except ImportError:  # Non-Windows fallback (for unit tests / portability)
    winreg = None

from core.karyawan_db import load_users_from_karyawan_db, resolve_karyawan_db_path


BDE_REG_PATHS = (
    r"SOFTWARE\WOW6432Node\Borland\Database Engine",
    r"SOFTWARE\Borland\Database Engine",
)
BDE_SYSTEM_INIT_SUBKEY = r"Settings\SYSTEM\INIT"
DEFAULT_HTTP_SERVER_INI_PATH = r"D:\SJABPayroll\HTTPServerDB.ini"
DEFAULT_BDE_CFG_PATH = r"C:\BDE32\IDAPI32.CFG"


@dataclass
class BdeSettings:
    memsize: int = 16  # MB (16..205)
    sharedmemsize: int = 2048  # KB (min 2048)
    maxfilehandles: int = 48  # handles (5..4096)
    minbufsize: int = 128  # KB (32..65535)
    maxbufsize: int = 2048  # KB (MINBUFSIZE..65535, multiple of 128)
    low_memory_usage_limit: int = 32  # KB
    local_share: str = "FALSE"
    config_file: str = DEFAULT_BDE_CFG_PATH
    save_config: str = "WIN31"
    registry_found: bool = False
    source: str = "Default BDE (Registry tidak ditemukan)"
    cfg_settings: Dict[str, str] = field(default_factory=dict)
    cfg_mismatch: List[str] = field(default_factory=list)

    def clone(self) -> "BdeSettings":
        return BdeSettings(
            memsize=self.memsize,
            sharedmemsize=self.sharedmemsize,
            maxfilehandles=self.maxfilehandles,
            minbufsize=self.minbufsize,
            maxbufsize=self.maxbufsize,
            low_memory_usage_limit=self.low_memory_usage_limit,
            local_share=self.local_share,
            config_file=self.config_file,
            save_config=self.save_config,
            registry_found=self.registry_found,
            source=self.source,
            cfg_settings=dict(self.cfg_settings),
            cfg_mismatch=list(self.cfg_mismatch),
        )


@dataclass
class HttpServerIniSettings:
    ini_path: str = DEFAULT_HTTP_SERVER_INI_PATH
    exists: bool = False
    port: int = 56563
    max_connections: int = 20
    listen_queue: int = 5
    session_timeout: int = 0
    max_threads: int = 100
    detail_log: str = "Y"
    db_alias: str = ""
    net_dir: str = ""

    def clone(self) -> "HttpServerIniSettings":
        return HttpServerIniSettings(
            ini_path=self.ini_path,
            exists=self.exists,
            port=self.port,
            max_connections=self.max_connections,
            listen_queue=self.listen_queue,
            session_timeout=self.session_timeout,
            max_threads=self.max_threads,
            detail_log=self.detail_log,
            db_alias=self.db_alias,
            net_dir=self.net_dir,
        )


@dataclass
class ParameterAdvice:
    section: str  # "INI" | "BDE"
    name: str
    unit: str
    current: Any
    cfg_value: str
    recommended: Any
    changed: bool
    locked: bool
    status: str  # "Ubah" | "Sudah memadai" | "Tidak diubah"
    reason: str


@dataclass
class RecommendationReport:
    user_count: int
    total_rows: int
    total_all_accounts: int
    db_path: str
    db_format: str
    db_loaded: bool
    allow_bde: bool
    allow_ini: bool
    current_bde: BdeSettings
    current_ini: HttpServerIniSettings
    recommended_bde: BdeSettings
    recommended_ini: HttpServerIniSettings
    ini_items: List[ParameterAdvice]
    bde_items: List[ParameterAdvice]
    current_ini_capacity: int
    current_bde_capacity: int
    current_bde_bottleneck: str
    projected_ini_capacity: int
    projected_bde_capacity: int
    projected_bde_bottleneck: str
    projected_safe_concurrency: int
    can_achieve_full_burst_zero_fail: bool
    recommended_sim_concurrency: int
    recommended_sim_interval_ms: int
    recommended_sim_timeout_s: float
    system_ram_mb: Optional[int]
    cpu_threads: int
    headline_status: str
    warnings: List[str]
    notes: List[str]
    summary_text: str


def _safe_int(val: Any, default: int) -> int:
    try:
        s = str(val).strip()
        if not s:
            return default
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _normalize_yn(val: Any, default: str = "Y") -> str:
    s = str(val or "").strip().upper()
    if s in ("N", "NO", "FALSE", "0", "T"):
        return "N"
    if s in ("Y", "YES", "TRUE", "1"):
        return "Y"
    return default


def _normalize_bool_str(val: Any, default: str = "FALSE") -> str:
    s = str(val or "").strip().upper()
    if s in ("TRUE", "1", "YES", "Y"):
        return "TRUE"
    if s in ("FALSE", "0", "NO", "N"):
        return "FALSE"
    return default


def _ceil_step(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def _pick_at_least(value: int, candidates: List[int]) -> int:
    for c in candidates:
        if c >= value:
            return c
    return candidates[-1]


def get_system_hardware_info() -> Tuple[Optional[int], int]:
    """Return (total_ram_mb_or_None, logical_cpu_count)."""
    cpus = os.cpu_count() or 1
    ram_mb: Optional[int] = None
    if sys.platform.startswith("win"):
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                ram_mb = int(stat.ullTotalPhys // (1024 * 1024))
        except Exception:
            ram_mb = None
    return ram_mb, cpus


def parse_idapi_cfg_bytes(data: bytes) -> Dict[str, str]:
    """Extract SYSTEM/INIT key-value pairs from Borland binary IDAPI32.CFG bytes."""
    result: Dict[str, str] = {}
    if not data:
        return result

    stack: List[str] = []
    pos = 0
    n = len(data)
    while pos < n:
        if data.startswith(b"\x03\x00\x00\x00", pos):
            pos += 4
            end = data.find(b"\x00", pos)
            if end < 0:
                break
            folder = data[pos:end].decode("ascii", errors="replace").strip()
            pos = end + 1
            if data.startswith(b"\x01\x00", pos):
                pos += 2
            stack.append(folder)
        elif data.startswith(b"\x03\x00\x01\x00", pos):
            pos += 4
            k_end = data.find(b"\x00", pos)
            if k_end < 0:
                break
            key = data[pos:k_end].decode("ascii", errors="replace").strip()
            pos = k_end + 1
            if not data.startswith(b"\x04\x00", pos):
                break
            pos += 2
            v_end = data.find(b"\x00", pos)
            if v_end < 0:
                break
            val = data[pos:v_end].decode("ascii", errors="replace").strip()
            pos = v_end + 1
            path = "/".join(s.upper() for s in stack)
            if path == "SYSTEM/INIT" and key:
                result[key.upper()] = val
        elif data.startswith(b"\x02\x00", pos):
            if stack:
                stack.pop()
            pos += 2
        else:
            pos += 1
    return result


def parse_idapi_cfg_system_init(cfg_path: str) -> Dict[str, str]:
    """Read and parse SYSTEM/INIT entries from IDAPI32.CFG, returning {} on failure."""
    if not cfg_path or not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path, "rb") as f:
            return parse_idapi_cfg_bytes(f.read())
    except OSError:
        return {}


def _read_registry_key_values(key_path: str) -> Optional[Dict[str, str]]:
    if winreg is None:
        return None
    flags = winreg.KEY_READ
    if hasattr(winreg, "KEY_WOW64_32KEY"):
        flags |= winreg.KEY_WOW64_32KEY
    for access in (flags, winreg.KEY_READ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access) as key:
                values: Dict[str, str] = {}
                idx = 0
                while True:
                    try:
                        name, val, _typ = winreg.EnumValue(key, idx)
                        values[str(name).strip().upper()] = str(val).strip()
                        idx += 1
                    except OSError:
                        break
                return values
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return None


def read_bde_settings(
    registry_reader: Optional[Callable[[str], Optional[Dict[str, str]]]] = None,
    cfg_parser: Optional[Callable[[str], Dict[str, str]]] = None,
) -> BdeSettings:
    """Read active 32-bit BDE SYSTEM\\INIT settings from Windows Registry and compare with IDAPI32.CFG."""
    reader = registry_reader or _read_registry_key_values
    parser = cfg_parser or parse_idapi_cfg_system_init

    root_vals: Dict[str, str] = {}
    init_vals: Dict[str, str] = {}
    found_path = ""

    for base in BDE_REG_PATHS:
        iv = reader(f"{base}\\{BDE_SYSTEM_INIT_SUBKEY}")
        if iv is not None:
            init_vals = iv
            root_vals = reader(base) or {}
            found_path = f"HKLM\\{base}\\{BDE_SYSTEM_INIT_SUBKEY}"
            break

    cfg_file = root_vals.get("CONFIGFILE01") or DEFAULT_BDE_CFG_PATH
    save_cfg = root_vals.get("SAVECONFIG") or "WIN31"
    cfg_vals = parser(cfg_file) if cfg_file else {}

    if not init_vals and not found_path:
        return BdeSettings(
            config_file=cfg_file,
            save_config=save_cfg,
            registry_found=False,
            source="Default BDE (Registry HKLM tidak ditemukan / bukan Windows)",
            cfg_settings=cfg_vals,
            cfg_mismatch=[],
        )

    bde = BdeSettings(
        memsize=max(16, min(205, _safe_int(init_vals.get("MEMSIZE"), 16))),
        sharedmemsize=max(2048, _safe_int(init_vals.get("SHAREDMEMSIZE"), 2048)),
        maxfilehandles=max(5, min(4096, _safe_int(init_vals.get("MAXFILEHANDLES"), 48))),
        minbufsize=max(32, min(65535, _safe_int(init_vals.get("MINBUFSIZE"), 128))),
        maxbufsize=max(128, min(65535, _safe_int(init_vals.get("MAXBUFSIZE"), 2048))),
        low_memory_usage_limit=max(16, _safe_int(init_vals.get("LOW MEMORY USAGE LIMIT"), 32)),
        local_share=_normalize_bool_str(init_vals.get("LOCAL SHARE"), "FALSE"),
        config_file=cfg_file,
        save_config=save_cfg,
        registry_found=True,
        source=found_path,
        cfg_settings=cfg_vals,
    )

    mismatches: List[str] = []
    if cfg_vals:
        check_pairs = [
            ("MEMSIZE", str(bde.memsize), "MB"),
            ("SHAREDMEMSIZE", str(bde.sharedmemsize), "KB"),
            ("MAXFILEHANDLES", str(bde.maxfilehandles), ""),
            ("MINBUFSIZE", str(bde.minbufsize), "KB"),
            ("MAXBUFSIZE", str(bde.maxbufsize), "KB"),
            ("LOCAL SHARE", bde.local_share, ""),
        ]
        for key, reg_v, unit in check_pairs:
            cfg_v = cfg_vals.get(key)
            if cfg_v is not None and cfg_v.strip().upper() != reg_v.strip().upper():
                suffix = f" {unit}" if unit else ""
                mismatches.append(f"{key}: Registry={reg_v}{suffix} vs CFG={cfg_v}{suffix}")
    bde.cfg_mismatch = mismatches
    return bde


def resolve_http_server_ini_path(configured_path: str, app_root: str = "") -> str:
    """Resolve HTTPServerDB.ini path without modifying target files."""
    raw = (configured_path or "").strip()
    if raw:
        if os.path.isabs(raw):
            return raw
        if app_root:
            cand = os.path.join(app_root, raw)
            if os.path.isfile(cand):
                return cand

    candidates = [
        DEFAULT_HTTP_SERVER_INI_PATH,
        os.path.join(app_root, "HTTPServerDB.ini") if app_root else "",
        os.path.join(os.path.dirname(app_root), "HTTPServerDB.ini") if app_root else "",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return raw or DEFAULT_HTTP_SERVER_INI_PATH


def read_http_server_ini(ini_path: str) -> HttpServerIniSettings:
    """Read HTTPServerDB.ini [Settings] values safely (read-only)."""
    settings = HttpServerIniSettings(ini_path=ini_path, exists=False)
    if not ini_path or not os.path.isfile(ini_path):
        return settings

    cp = configparser.RawConfigParser(strict=False)
    content = ""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(ini_path, "r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue
        except OSError:
            return settings

    try:
        cp.read_string(content)
    except configparser.Error:
        return settings

    sec_name = None
    for s in cp.sections():
        if s.strip().lower() == "settings":
            sec_name = s
            break

    settings.exists = True
    if not sec_name:
        return settings

    sec = {k.strip().lower(): v.strip() for k, v in cp.items(sec_name)}
    settings.port = max(1, min(65535, _safe_int(sec.get("port"), 56563)))
    settings.max_connections = max(0, _safe_int(sec.get("maxconnections"), 20))
    settings.listen_queue = max(0, _safe_int(sec.get("listenqueue"), 5))
    settings.session_timeout = max(0, _safe_int(sec.get("sessiontimeout"), 0))
    settings.max_threads = max(0, _safe_int(sec.get("maxthreads"), 100))
    settings.detail_log = _normalize_yn(sec.get("detaillog"), "Y")
    settings.db_alias = sec.get("dbalias", "")
    settings.net_dir = sec.get("netdir", "")
    return settings


def read_karyawan_summary(
    db_path_cfg: str,
    app_root: str,
    tables_dir: str,
    only_active: bool = True,
    fallback_user_count: int = 0,
) -> Dict[str, Any]:
    """Read user counts directly from Karyawan.DB without modifying anything."""
    resolved = resolve_karyawan_db_path(db_path_cfg, app_root, tables_dir)
    if not os.path.isfile(resolved):
        return {
            "loaded": False,
            "db_path": resolved,
            "format": "-",
            "user_count": max(1, fallback_user_count),
            "active_count": max(0, fallback_user_count),
            "all_accounts_count": max(0, fallback_user_count),
            "total_rows": 0,
            "error": f"File tidak ditemukan: {resolved}",
        }

    try:
        users_filtered, info_filtered = load_users_from_karyawan_db(resolved, only_active=only_active)
        if only_active:
            users_all, _ = load_users_from_karyawan_db(resolved, only_active=False)
            all_accounts = len(users_all)
            active_cnt = len(users_filtered)
        else:
            users_act, _ = load_users_from_karyawan_db(resolved, only_active=True)
            all_accounts = len(users_filtered)
            active_cnt = len(users_act)

        return {
            "loaded": True,
            "db_path": resolved,
            "format": info_filtered.get("format", "Paradox"),
            "user_count": max(1, len(users_filtered)),
            "raw_loaded_count": len(users_filtered),
            "active_count": active_cnt,
            "all_accounts_count": all_accounts,
            "total_rows": int(info_filtered.get("total_rows", 0)),
            "skipped_inactive": int(info_filtered.get("skipped_inactive", 0)),
            "user_col": info_filtered.get("user_col", ""),
            "password_col": info_filtered.get("password_col", ""),
            "error": "",
        }
    except Exception as exc:
        return {
            "loaded": False,
            "db_path": resolved,
            "format": "-",
            "user_count": max(1, fallback_user_count),
            "active_count": max(0, fallback_user_count),
            "all_accounts_count": max(0, fallback_user_count),
            "total_rows": 0,
            "error": str(exc),
        }


def estimate_bde_safe_concurrency(bde: BdeSettings) -> Tuple[int, str]:
    """Estimate safe simultaneous Paradox BDE query concurrency calibrated to HTTPServerDB.exe."""
    cap_mem = max(5, int((bde.memsize - 2) / 0.42))
    cap_shmem = max(5, int((bde.sharedmemsize - 400) / 48))
    cap_handles = max(5, int((bde.maxfilehandles - 10) / 1.15))

    raw_cap = min(cap_mem, cap_shmem, cap_handles)
    if bde.local_share == "TRUE":
        raw_cap = max(5, int(raw_cap * 0.85))

    bottlenecks: List[Tuple[int, str]] = [
        (cap_mem, f"MEMSIZE={bde.memsize} MB (~{cap_mem} paralel)"),
        (cap_shmem, f"SHAREDMEMSIZE={bde.sharedmemsize} KB (~{cap_shmem} paralel)"),
        (cap_handles, f"MAXFILEHANDLES={bde.maxfilehandles} (~{cap_handles} paralel)"),
    ]
    bottlenecks.sort(key=lambda x: x[0])
    return raw_cap, bottlenecks[0][1]


def estimate_ini_safe_concurrency(ini: HttpServerIniSettings) -> Tuple[int, str]:
    """Estimate safe simultaneous burst capacity of HTTPServerDB.ini (Indy TIdHTTPServer)."""
    eff_conn = ini.max_connections if ini.max_connections > 0 else (ini.max_threads if ini.max_threads > 0 else 500)
    eff_threads = ini.max_threads if ini.max_threads > 0 else 500
    eff_queue_burst = max(1, ini.listen_queue * 2) if ini.listen_queue > 0 else eff_conn

    cap = min(eff_conn, eff_threads, eff_queue_burst)
    reasons = [
        (eff_conn, f"MaxConnections={ini.max_connections}"),
        (eff_threads, f"MaxThreads={ini.max_threads}"),
        (eff_queue_burst, f"ListenQueue={ini.listen_queue} (~{eff_queue_burst} burst SYN)"),
    ]
    reasons.sort(key=lambda x: x[0])
    return max(1, cap), reasons[0][1]


def _build_recommended_ini(current: HttpServerIniSettings, user_count: int) -> Tuple[HttpServerIniSettings, List[ParameterAdvice]]:
    target = max(1, user_count)
    rec = current.clone()

    needed_conn = max(50, _ceil_step(math.ceil(target * 1.25), 10))
    rec.max_connections = max(current.max_connections, needed_conn) if current.max_connections > 0 else needed_conn

    needed_lq_raw = max(64, math.ceil(target * 1.25))
    needed_lq = _pick_at_least(needed_lq_raw, [64, 128, 200, 256, 384, 512])
    rec.listen_queue = max(current.listen_queue, needed_lq)

    needed_threads = max(150, rec.max_connections * 2)
    rec.max_threads = max(current.max_threads, needed_threads) if current.max_threads > 0 else needed_threads

    rec.session_timeout = 0
    rec.detail_log = "N" if target >= 15 else current.detail_log

    reasons = {
        "MaxConnections": f"Minimal 1.25x target {target} user (+cadangan socket TIME_WAIT/tutup-buka)",
        "ListenQueue": f"Antrean TCP OS agar burst {target} koneksi serentak tidak terkena RST/refused",
        "MaxThreads": f"Minimal 2x MaxConnections ({rec.max_connections}) untuk pool thread Indy",
        "SessionTimeOut": "0 = nonaktifkan timer session Indy (Flutter bersifat stateless per request)",
        "DetailLog": "N = hindari bottleneck UI listbox & flush file log saat burst paralel",
    }

    items: List[ParameterAdvice] = []
    specs = [
        ("MaxConnections", "", current.max_connections, rec.max_connections),
        ("ListenQueue", "", current.listen_queue, rec.listen_queue),
        ("MaxThreads", "", current.max_threads, rec.max_threads),
        ("SessionTimeOut", "detik", current.session_timeout, rec.session_timeout),
        ("DetailLog", "", current.detail_log, rec.detail_log),
    ]
    for name, unit, cur_val, rec_val in specs:
        changed = cur_val != rec_val
        items.append(
            ParameterAdvice(
                section="INI",
                name=name,
                unit=unit,
                current=cur_val,
                cfg_value="-",
                recommended=rec_val,
                changed=changed,
                locked=False,
                status="Ubah" if changed else "Sudah memadai",
                reason=reasons[name],
            )
        )
    return rec, items


def _build_locked_ini(current: HttpServerIniSettings) -> Tuple[HttpServerIniSettings, List[ParameterAdvice]]:
    rec = current.clone()
    specs = [
        ("MaxConnections", "", current.max_connections),
        ("ListenQueue", "", current.listen_queue),
        ("MaxThreads", "", current.max_threads),
        ("SessionTimeOut", "detik", current.session_timeout),
        ("DetailLog", "", current.detail_log),
    ]
    items = [
        ParameterAdvice(
            section="INI",
            name=name,
            unit=unit,
            current=val,
            cfg_value="-",
            recommended=val,
            changed=False,
            locked=True,
            status="Tidak diubah",
            reason="Centangan perubahan .ini tidak aktif",
        )
        for name, unit, val in specs
    ]
    return rec, items


def _build_recommended_bde(current: BdeSettings, user_count: int) -> Tuple[BdeSettings, List[ParameterAdvice]]:
    target = max(1, user_count)
    rec = current.clone()

    # MEMSIZE: 16..205 MB (Borland BDE cap = 205 MB)
    needed_mem = min(205, max(64, _ceil_step(target * 1.5 + 16, 32)))
    if needed_mem > 192:
        needed_mem = 205
    rec.memsize = min(205, max(current.memsize, needed_mem))

    # SHAREDMEMSIZE: KB (min 2048)
    needed_shmem_raw = max(4096, target * 96 + 400)
    needed_shmem = _pick_at_least(needed_shmem_raw, [4096, 8192, 16384, 32768, 65536])
    rec.sharedmemsize = min(65536, max(current.sharedmemsize, needed_shmem))

    # MAXFILEHANDLES: 5..4096
    needed_handles_raw = max(255, target * 4 + 64)
    needed_handles = _pick_at_least(needed_handles_raw, [255, 512, 1024, 2048, 4096])
    rec.maxfilehandles = min(4096, max(current.maxfilehandles, needed_handles))

    # Buffer sizes: multiple of 128 KB
    needed_minbuf = 1024 if target > 50 else 512
    needed_maxbuf = 8192 if target > 50 else 4096
    rec.minbufsize = max(current.minbufsize, needed_minbuf)
    rec.maxbufsize = max(current.maxbufsize, needed_maxbuf)
    rec.maxbufsize = max(rec.minbufsize, _ceil_step(rec.maxbufsize, 128))
    rec.maxbufsize = min(65535, rec.maxbufsize)

    cfg_vals = current.cfg_settings or {}
    specs = [
        (
            "MEMSIZE",
            "MB",
            current.memsize,
            cfg_vals.get("MEMSIZE", "-"),
            rec.memsize,
            f"Heap BDE untuk query temp (_QSQ*) & buffer Paradox (maks resmi BDE 205 MB)",
        ),
        (
            "SHAREDMEMSIZE",
            "KB",
            current.sharedmemsize,
            cfg_vals.get("SHAREDMEMSIZE", "-"),
            rec.sharedmemsize,
            f"Memori bersama tabel/cursor/lock untuk {target} request paralel agar bebas Insufficient memory",
        ),
        (
            "MAXFILEHANDLES",
            "",
            current.maxfilehandles,
            cfg_vals.get("MAXFILEHANDLES", "-"),
            rec.maxfilehandles,
            "Handle file DB/PX/MB/temp paralel (~4-6 handle per request aktif)",
        ),
        (
            "MINBUFSIZE",
            "KB",
            current.minbufsize,
            cfg_vals.get("MINBUFSIZE", "-"),
            rec.minbufsize,
            "Cache buffer minimum database BDE",
        ),
        (
            "MAXBUFSIZE",
            "KB",
            current.maxbufsize,
            cfg_vals.get("MAXBUFSIZE", "-"),
            rec.maxbufsize,
            "Cache buffer maksimum BDE (kelipatan 128 KB, kurangi disk thrashing Absen.DB)",
        ),
        (
            "LOCAL SHARE",
            "",
            current.local_share,
            cfg_vals.get("LOCAL SHARE", "-"),
            rec.local_share,
            "Pertahankan FALSE jika hanya HTTPServerDB.exe yang mengakses tabel (lebih cepat)",
        ),
    ]

    items: List[ParameterAdvice] = []
    for name, unit, cur_val, cfg_val, rec_val, reason in specs:
        changed = cur_val != rec_val
        items.append(
            ParameterAdvice(
                section="BDE",
                name=name,
                unit=unit,
                current=cur_val,
                cfg_value=str(cfg_val),
                recommended=rec_val,
                changed=changed,
                locked=False,
                status="Ubah" if changed else "Sudah memadai",
                reason=reason,
            )
        )
    return rec, items


def _build_locked_bde(current: BdeSettings) -> Tuple[BdeSettings, List[ParameterAdvice]]:
    rec = current.clone()
    cfg_vals = current.cfg_settings or {}
    specs = [
        ("MEMSIZE", "MB", current.memsize, cfg_vals.get("MEMSIZE", "-")),
        ("SHAREDMEMSIZE", "KB", current.sharedmemsize, cfg_vals.get("SHAREDMEMSIZE", "-")),
        ("MAXFILEHANDLES", "", current.maxfilehandles, cfg_vals.get("MAXFILEHANDLES", "-")),
        ("MINBUFSIZE", "KB", current.minbufsize, cfg_vals.get("MINBUFSIZE", "-")),
        ("MAXBUFSIZE", "KB", current.maxbufsize, cfg_vals.get("MAXBUFSIZE", "-")),
        ("LOCAL SHARE", "", current.local_share, cfg_vals.get("LOCAL SHARE", "-")),
    ]
    items = [
        ParameterAdvice(
            section="BDE",
            name=name,
            unit=unit,
            current=val,
            cfg_value=str(cfg_val),
            recommended=val,
            changed=False,
            locked=True,
            status="Tidak diubah",
            reason="Centangan perubahan BDE tidak aktif — menggunakan setting BDE saat ini",
        )
        for name, unit, val, cfg_val in specs
    ]
    return rec, items


def calculate_recommendations(
    user_count: int,
    current_bde: BdeSettings,
    current_ini: HttpServerIniSettings,
    allow_bde: bool = True,
    allow_ini: bool = True,
    db_info: Optional[Dict[str, Any]] = None,
) -> RecommendationReport:
    """Compute INI & BDE recommendations strictly honoring allow_bde / allow_ini checkboxes."""
    target_users = max(1, int(user_count))
    info = db_info or {}
    ram_mb, cpu_threads = get_system_hardware_info()

    cur_ini_cap, _ = estimate_ini_safe_concurrency(current_ini)
    cur_bde_cap, cur_bde_bn = estimate_bde_safe_concurrency(current_bde)

    if allow_ini:
        rec_ini, ini_items = _build_recommended_ini(current_ini, target_users)
    else:
        rec_ini, ini_items = _build_locked_ini(current_ini)

    if allow_bde:
        rec_bde, bde_items = _build_recommended_bde(current_bde, target_users)
    else:
        rec_bde, bde_items = _build_locked_bde(current_bde)

    proj_ini_cap, proj_ini_bn = estimate_ini_safe_concurrency(rec_ini)
    proj_bde_cap, proj_bde_bn = estimate_bde_safe_concurrency(rec_bde)
    proj_safe_conc = min(proj_ini_cap, proj_bde_cap)
    can_full_burst = proj_safe_conc >= target_users and (rec_ini.listen_queue >= target_users or rec_ini.listen_queue == 0)

    # Recommended simulator settings to achieve 0% failure under projected server state
    if can_full_burst:
        rec_sim_conc = target_users
        rec_sim_interval = 0
        rec_sim_timeout = 15.0 if target_users >= 80 else 10.0
    else:
        rec_sim_conc = min(target_users, max(1, int(math.floor(proj_safe_conc * 0.85))))
        if proj_safe_conc <= 10:
            rec_sim_conc = min(target_users, max(1, proj_safe_conc))
        rec_sim_interval = 50 if target_users > rec_sim_conc else 25
        rec_sim_timeout = 20.0 if target_users >= 80 else 15.0

    warnings: List[str] = []
    notes: List[str] = []

    if current_bde.cfg_mismatch:
        warnings.append(
            "Ditemukan perbedaan antara Windows Registry BDE (yang dipakai HTTPServerDB.exe) dan "
            f"{current_bde.config_file}: " + "; ".join(current_bde.cfg_mismatch)
            + ". Gunakan BDE Administrator (Run as Administrator) atau edit Registry WOW6432Node lalu klik Apply/Save."
        )

    if not current_ini.exists:
        warnings.append(
            f"File HTTPServerDB.ini tidak ditemukan di '{current_ini.ini_path}'. Perhitungan memakai nilai default standar."
        )

    if can_full_burst:
        headline = (
            f"SIAP 0% GAGAL UNTUK {target_users} USER SERENTAK: "
            f"Kapasitas gabungan menjadi ~{proj_safe_conc} request paralel "
            f"(.ini ~{proj_ini_cap} | BDE ~{proj_bde_cap})."
        )
    else:
        limiting_reasons: List[str] = []
        if not allow_bde and proj_bde_cap < target_users:
            limiting_reasons.append(
                f"BDE tidak diubah (kapasitas aman ~{proj_bde_cap} serentak karena {proj_bde_bn})"
            )
        elif proj_bde_cap < target_users:
            limiting_reasons.append(f"Batas BDE (~{proj_bde_cap} karena {proj_bde_bn})")

        if not allow_ini and proj_ini_cap < target_users:
            limiting_reasons.append(
                f".ini tidak diubah (kapasitas aman ~{proj_ini_cap} serentak karena {proj_ini_bn})"
            )
        elif proj_ini_cap < target_users:
            limiting_reasons.append(f"Batas .ini (~{proj_ini_cap} karena {proj_ini_bn})")

        reason_txt = " & ".join(limiting_reasons) if limiting_reasons else "keterbatasan kapasitas"
        headline = (
            f"KAPASITAS TERBATAS (~{proj_safe_conc} PARALEL AMAN < {target_users} USER): {reason_txt}. "
            f"Untuk 0% gagal tanpa mengubah bagian terkunci, jalankan simulator dengan "
            f"Concurrency={rec_sim_conc} & Jeda Start={rec_sim_interval} ms."
        )
        warnings.append(
            f"Target {target_users} user serentak 0 ms melebihi batas aman {proj_safe_conc} paralel karena {reason_txt}."
        )

    if allow_ini:
        if any(item.changed for item in ini_items):
            notes.append(
                "Penting saat mengubah HTTPServerDB.ini: tutup HTTPServerDB.exe terlebih dahulu sebelum edit manual "
                "(karena HTTPServerDB.exe menimpa ulang .ini saat FormDestroy/keluar), lalu jalankan kembali."
            )
        else:
            notes.append("Seluruh parameter HTTPServerDB.ini saat ini sudah memadai.")

    if allow_bde and any(item.changed for item in bde_items):
        notes.append(
            "Penting saat mengubah BDE: ubah pada HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Borland\\Database Engine\\Settings\\SYSTEM\\INIT "
            "(atau BDE Administrator sebagai Administrator -> Object | Apply), lalu restart HTTPServerDB.exe agar memori baru terbaca."
        )

    summary_lines = [
        f"=== REKOMENDASI SERVER SGABSENSI ({target_users} USER) ===",
        f"Mode Kalkulasi: Ubah BDE={'YA' if allow_bde else 'TIDAK (Tetap)'} | Ubah .ini={'YA' if allow_ini else 'TIDAK (Tetap)'}",
        f"Kapasitas Saat Ini : ~{min(cur_ini_cap, cur_bde_cap)} paralel (.ini ~{cur_ini_cap} | BDE ~{cur_bde_cap})",
        f"Kapasitas Proyeksi : ~{proj_safe_conc} paralel (.ini ~{proj_ini_cap} | BDE ~{proj_bde_cap})",
        f"Simulator 0% Gagal : User Bersamaan={rec_sim_conc}, Jeda Start={rec_sim_interval} ms, Timeout={rec_sim_timeout:.0f} dtk",
        "",
        f"[HTTPServerDB.ini] ({current_ini.ini_path})",
    ]
    if allow_ini:
        summary_lines.append("[Settings]")
        for item in ini_items:
            mark = "  <-- UBAH" if item.changed else "  (sudah cukup)"
            summary_lines.append(f"{item.name}={item.recommended}  ; saat ini: {item.current}{mark}")
    else:
        summary_lines.append("-> TIDAK DIUBAH (sesuai centangan). Nilai aktif:")
        for item in ini_items:
            summary_lines.append(f"   {item.name}={item.current}")

    summary_lines.extend(["", f"[BDE SYSTEM\\INIT] ({current_bde.source})"])
    if allow_bde:
        for item in bde_items:
            mark = "  <-- UBAH" if item.changed else "  (tetap)"
            unit = f" {item.unit}" if item.unit else ""
            summary_lines.append(f"{item.name}={item.recommended}{unit}  ; saat ini Reg: {item.current}{unit}{mark}")
    else:
        summary_lines.append("-> TIDAK DIUBAH (sesuai centangan). Nilai Registry aktif:")
        for item in bde_items:
            unit = f" {item.unit}" if item.unit else ""
            summary_lines.append(f"   {item.name}={item.current}{unit}")

    if warnings:
        summary_lines.extend(["", "Peringatan:"])
        summary_lines.extend(f"- {w}" for w in warnings)

    return RecommendationReport(
        user_count=target_users,
        total_rows=int(info.get("total_rows", 0)),
        total_all_accounts=int(info.get("all_accounts_count", target_users)),
        db_path=str(info.get("db_path", "")),
        db_format=str(info.get("format", "-")),
        db_loaded=bool(info.get("loaded", False)),
        allow_bde=allow_bde,
        allow_ini=allow_ini,
        current_bde=current_bde,
        current_ini=current_ini,
        recommended_bde=rec_bde,
        recommended_ini=rec_ini,
        ini_items=ini_items,
        bde_items=bde_items,
        current_ini_capacity=cur_ini_cap,
        current_bde_capacity=cur_bde_cap,
        current_bde_bottleneck=cur_bde_bn,
        projected_ini_capacity=proj_ini_cap,
        projected_bde_capacity=proj_bde_cap,
        projected_bde_bottleneck=proj_bde_bn,
        projected_safe_concurrency=proj_safe_conc,
        can_achieve_full_burst_zero_fail=can_full_burst,
        recommended_sim_concurrency=rec_sim_conc,
        recommended_sim_interval_ms=rec_sim_interval,
        recommended_sim_timeout_s=rec_sim_timeout,
        system_ram_mb=ram_mb,
        cpu_threads=cpu_threads,
        headline_status=headline,
        warnings=warnings,
        notes=notes,
        summary_text="\n".join(summary_lines),
    )
