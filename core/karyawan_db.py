import codecs
import os
import sqlite3
import struct
from typing import Any, Dict, List, Optional, Tuple

from core.models import UserAccount


_USER_COL_CANDIDATES = ("userapk", "user_apk", "userid", "user_id", "username")
_PASS_COL_CANDIDATES = ("passwordapk", "password_apk", "password", "passapk", "pwd")
_KODE_COL_CANDIDATES = ("kode", "kodekaryawan", "kode_karyawan")
_SALES_COL_CANDIDATES = ("sales", "issales", "is_sales")
_AKTIF_COL_CANDIDATES = ("aktif", "active", "isactive", "is_active")


def _safe_encoding(codepage: int) -> str:
    if codepage <= 0:
        return "cp1252"
    name = f"cp{codepage}"
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return "cp1252"


def _decode_paradox_field(raw: bytes, ftype: int, encoding: str) -> Any:
    if ftype == 0x01:  # Alpha
        trimmed = raw.split(b"\x00", 1)[0]
        return trimmed.decode(encoding, errors="replace").strip()
    if ftype == 0x09:  # Logical
        if not raw or raw[0] == 0:
            return None
        return (raw[0] & 0x7F) != 0
    if ftype == 0x03 and len(raw) >= 2:  # Short int
        val = struct.unpack(">H", raw[:2])[0]
        return None if val == 0 else (val - 0x8000)
    if ftype in (0x04, 0x16) and len(raw) >= 4:  # Long int / Autoincrement
        val = struct.unpack(">I", raw[:4])[0]
        return None if val == 0 else (val - 0x80000000)
    if ftype in (0x05, 0x06) and len(raw) >= 8:  # Currency / Number
        if not any(raw[:8]):
            return None
        buf = bytearray(raw[:8])
        if buf[0] & 0x80:
            buf[0] ^= 0x80
        else:
            buf = bytearray(b ^ 0xFF for b in buf)
        return struct.unpack(">d", bytes(buf))[0]
    if ftype == 0x02 and len(raw) >= 4:  # Date (days since 1-1-0000)
        val = struct.unpack(">I", raw[:4])[0]
        return None if val == 0 else (val - 0x80000000)
    if ftype == 0x0C:  # Memo (first bytes may hold inline text)
        trimmed = raw.split(b"\x00", 1)[0]
        return trimmed.decode(encoding, errors="replace").strip()
    return None


def read_paradox_table(db_path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Read rows from an unencrypted Paradox (.DB) table (v3.5 / v4 / v5 / v7)."""
    with open(db_path, "rb") as f:
        data = f.read()

    if len(data) < 0x78:
        raise ValueError(f"File '{db_path}' bukan tabel Paradox yang valid (ukuran terlalu kecil).")

    rec_size, hdr_size, file_type, max_tbl_size = struct.unpack_from("<HHBB", data, 0)
    num_recs, _next_blk, _file_blocks, first_blk, _last_blk = struct.unpack_from("<IHHHH", data, 6)
    num_fields = struct.unpack_from("<H", data, 0x21)[0]
    ver_id = data[0x39]

    if file_type not in (0x00, 0x02):
        raise ValueError(
            f"File '{os.path.basename(db_path)}' bertipe {file_type:#x}, bukan tabel data Paradox (.DB)."
        )
    if rec_size <= 0 or hdr_size < 0x78 or hdr_size > len(data) or num_fields <= 0 or max_tbl_size <= 0:
        raise ValueError(f"Header tabel Paradox '{db_path}' tidak valid atau rusak.")

    enc1 = struct.unpack_from("<I", data, 0x25)[0]
    enc2 = struct.unpack_from("<I", data, 0x5C)[0] if ver_id >= 0x05 else 0
    if (ver_id >= 0x05 and enc2 != 0) or (ver_id < 0x05 and enc1 not in (0, 0xFF00FF00)):
        raise ValueError(f"Tabel Paradox '{os.path.basename(db_path)}' terenkripsi password.")

    block_size = max_tbl_size * 1024
    cp_num = struct.unpack_from("<H", data, 0x6A)[0] if ver_id >= 0x05 else 437
    encoding = _safe_encoding(cp_num)

    field_info_off = 0x78 if ver_id >= 0x05 else 0x58
    tbl_name_len = 261 if ver_id >= 0x0C else 79
    names_off = field_info_off + 2 * num_fields + 4 + 4 * num_fields + tbl_name_len
    if names_off >= hdr_size:
        raise ValueError(f"Struktur definisi kolom Paradox pada '{db_path}' di luar batas header.")

    fields: List[Tuple[str, int, int, int]] = []
    rec_offset = 0
    pos = names_off
    for i in range(num_fields):
        ftype, fsize = struct.unpack_from("<BB", data, field_info_off + i * 2)
        end = data.find(b"\x00", pos, hdr_size)
        if end < 0:
            raise ValueError(f"Gagal membaca nama kolom ke-{i + 1} di '{db_path}'.")
        fname = data[pos:end].decode(encoding, errors="replace").strip()
        pos = end + 1
        fields.append((fname, ftype, fsize, rec_offset))
        rec_offset += fsize

    if rec_offset > rec_size:
        raise ValueError(f"Ukuran total kolom ({rec_offset}) melebihi recordSize ({rec_size}) di '{db_path}'.")

    field_names = [f[0] for f in fields]
    if num_recs == 0 or first_blk == 0:
        return field_names, []

    rows: List[Dict[str, Any]] = []
    blk_num = first_blk
    visited = set()
    max_blk = (len(data) - hdr_size) // block_size + 1

    while blk_num != 0 and blk_num not in visited and blk_num <= max_blk:
        visited.add(blk_num)
        blk_off = hdr_size + (blk_num - 1) * block_size
        if blk_off + 6 > len(data):
            break
        next_blk, _prev_blk, add_data_size = struct.unpack_from("<HHh", data, blk_off)
        recs_in_blk = (add_data_size // rec_size) + 1 if add_data_size >= 0 else 0
        for r in range(recs_in_blk):
            r_off = blk_off + 6 + r * rec_size
            if r_off + rec_size > len(data):
                break
            rec = data[r_off : r_off + rec_size]
            row_dict: Dict[str, Any] = {}
            for fname, ftype, fsize, foff in fields:
                row_dict[fname] = _decode_paradox_field(rec[foff : foff + fsize], ftype, encoding)
            rows.append(row_dict)
        blk_num = next_blk

    return field_names, rows


def read_sqlite_karyawan_table(db_path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Read employee rows from an SQLite .db file containing UserAPK/UserID columns."""
    abs_path = os.path.abspath(db_path)
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        if not tables:
            raise ValueError(f"Database SQLite '{db_path}' tidak memiliki tabel.")

        chosen_table: Optional[str] = None
        chosen_cols: List[str] = []
        # Prefer table named karyawan or first table having user/password columns
        ordered_tables = sorted(tables, key=lambda t: (0 if "karyawan" in t.lower() else 1, t.lower()))
        for tbl in ordered_tables:
            safe_tbl = tbl.replace('"', '""')
            info_rows = conn.execute(f'PRAGMA table_info("{safe_tbl}")').fetchall()
            cols = [str(c["name"]) for c in info_rows]
            cols_lower = {c.lower() for c in cols}
            if any(c in cols_lower for c in _USER_COL_CANDIDATES):
                chosen_table = safe_tbl
                chosen_cols = cols
                break

        if not chosen_table:
            raise ValueError(
                f"Tidak ditemukan tabel dengan kolom UserAPK / UserID di database SQLite '{db_path}'."
            )

        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{chosen_table}"').fetchall()]
        return chosen_cols, rows
    finally:
        conn.close()


def read_karyawan_rows(db_path: str) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """Detect format (SQLite vs Paradox .DB) and return (format_name, columns, rows)."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"File database tidak ditemukan: {db_path}")

    with open(db_path, "rb") as f:
        magic = f.read(16)

    if magic.startswith(b"SQLite format 3\x00"):
        cols, rows = read_sqlite_karyawan_table(db_path)
        return "SQLite", cols, rows

    cols, rows = read_paradox_table(db_path)
    return "Paradox", cols, rows


def _find_case_insensitive_file(directory: str, target_filename: str) -> Optional[str]:
    if not os.path.isdir(directory):
        return None
    target_lower = target_filename.lower()
    try:
        for entry in os.listdir(directory):
            if entry.lower() == target_lower:
                full = os.path.join(directory, entry)
                if os.path.isfile(full):
                    return full
    except OSError:
        return None
    return None


def resolve_karyawan_db_path(configured_path: str, app_root: str, tables_dir: Optional[str] = None) -> str:
    """Resolve DB path relative to app_root, defaulting to <tables_dir>/Karyawan.DB."""
    t_dir = tables_dir or os.path.join(app_root, "tables")
    raw = (configured_path or "").strip()

    if raw:
        norm = raw.replace("\\", "/")
        if not os.path.isabs(raw) and norm.lower().startswith("tables/"):
            rel_in_tables = norm.split("/", 1)[1]
            alt = os.path.join(t_dir, rel_in_tables)
            if os.path.isfile(alt):
                return alt
            found = _find_case_insensitive_file(t_dir, os.path.basename(rel_in_tables))
            return found or alt

        candidate = raw if os.path.isabs(raw) else os.path.join(app_root, raw)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        found = _find_case_insensitive_file(parent, os.path.basename(candidate))
        return found or candidate

    found_default = _find_case_insensitive_file(t_dir, "karyawan.db")
    return found_default or os.path.join(t_dir, "Karyawan.DB")


def _parse_bool_flag(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "ya", "y", "t"):
        return True
    if s in ("false", "0", "no", "tidak", "n", "f"):
        return False
    return None


def _pick_column(col_map: Dict[str, str], candidates: Tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in col_map:
            return col_map[c]
    return None


def load_users_from_karyawan_db(
    db_path: str,
    only_active: bool = True,
    require_password: bool = False,
) -> Tuple[List[UserAccount], Dict[str, Any]]:
    """Load UserAccount credentials directly from karyawan.db (Paradox or SQLite)."""
    fmt, cols, rows = read_karyawan_rows(db_path)
    col_map = {c.lower(): c for c in cols}

    user_col = _pick_column(col_map, _USER_COL_CANDIDATES)
    pass_col = _pick_column(col_map, _PASS_COL_CANDIDATES)
    kode_col = _pick_column(col_map, _KODE_COL_CANDIDATES)
    sales_col = _pick_column(col_map, _SALES_COL_CANDIDATES)
    aktif_col = _pick_column(col_map, _AKTIF_COL_CANDIDATES)

    if not user_col:
        raise ValueError(
            f"Kolom UserAPK / UserID tidak ditemukan di {os.path.basename(db_path)}."
        )
    if not pass_col:
        raise ValueError(
            f"Kolom PasswordAPK / PasswordApk tidak ditemukan di {os.path.basename(db_path)}."
        )

    users: List[UserAccount] = []
    skipped_inactive = 0
    skipped_empty_user = 0
    skipped_empty_password = 0

    for row in rows:
        uid = str(row.get(user_col, "") or "").strip()
        if not uid:
            skipped_empty_user += 1
            continue

        if only_active and aktif_col:
            aktif_flag = _parse_bool_flag(row.get(aktif_col))
            if aktif_flag is False:
                skipped_inactive += 1
                continue

        pwd = str(row.get(pass_col, "") or "").strip()
        if require_password and not pwd:
            skipped_empty_password += 1
            continue

        kode = str(row.get(kode_col, "") or "").strip() if kode_col else ""
        sales_flag = _parse_bool_flag(row.get(sales_col)) if sales_col else None
        if sales_flag is True:
            sales_str = "True"
        elif sales_flag is False:
            sales_str = "False"
        else:
            sales_str = "Auto"

        users.append(
            UserAccount(
                user_id=uid,
                password=pwd,
                kode=kode,
                sales=sales_str,
            )
        )

    info = {
        "db_path": db_path,
        "format": fmt,
        "total_rows": len(rows),
        "loaded_count": len(users),
        "skipped_inactive": skipped_inactive,
        "skipped_empty_user": skipped_empty_user,
        "skipped_empty_password": skipped_empty_password,
        "user_col": user_col,
        "password_col": pass_col,
        "kode_col": kode_col,
        "sales_col": sales_col,
        "aktif_col": aktif_col,
    }
    return users, info
