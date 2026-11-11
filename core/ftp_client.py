import asyncio
import ftplib
import io


def _sync_ftp_upload(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    remote_filename: str,
    jpeg_bytes: bytes,
    timeout: float,
) -> None:
    ftp = ftplib.FTP()
    try:
        ftp.connect(host=host, port=port, timeout=timeout)
        ftp.login(user=user, passwd=password)
        ftp.set_pasv(True)
        if remote_dir:
            ftp.cwd(remote_dir)
        ftp.storbinary(f"STOR {remote_filename}", io.BytesIO(jpeg_bytes))
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise


async def upload_photo_ftp(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    remote_filename: str,
    jpeg_bytes: bytes,
    timeout: float = 30.0,
) -> None:
    await asyncio.to_thread(
        _sync_ftp_upload,
        host,
        port,
        user,
        password,
        remote_dir,
        remote_filename,
        jpeg_bytes,
        timeout,
    )
