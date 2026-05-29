"""
HTTP URL I/O helpers for adapter input fetch and output upload.

All I/O operations are streaming to avoid memory spikes on large files.
Uses stdlib urllib for GET (portable), curl for PUT (streaming via -T).

These helpers are engine-agnostic and used by all adapters.

Auth: when worker payload carries a `transfer_auth: TransferAuth`, the adapter
forwards it as a single header (`header_value` under `header_name`) on every
input GET and output PUT. The adapter never has to know the header name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPHandler, urlopen

from apps.common.helpers import env_bool, env_int, env_str, stream_subprocess
from .base_models import TransferAuth
from .stage import Stage

logger = logging.getLogger(__name__)

AGNET_ENGINE_NAME = "agnet"
AGNET_AVATAR_ASSETS_DIR = Path(env_str("AGNET_AVATAR_ASSETS_DIR", "/app/assets") or "/app/assets")
AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED = env_bool("AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED", True)
MAX_AVATAR_CONFIG_BYTES = env_int("MAX_AVATAR_CONFIG_BYTES", 64 * 1024)
_AVATAR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_AVATAR_MANIFEST_FILENAME = ".avatar_manifest.json"
_AVATAR_READY_FILENAME = ".ready"


@dataclass(frozen=True)
class AvatarRequest:
    """Structured avatar reference from params.avatar."""

    name: str
    uuid: Optional[str] = None
    version: Optional[int] = None
    config: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class MaterializedAvatar:
    """Avatar version available on local disk for the render service."""

    name: str
    uuid: str
    version: Optional[int]
    path: Path
    config: Optional[dict[str, Any]]


def probe_audio_duration_sec(path: Path, *, timeout: float = 10.0) -> float:
    """Return audio duration in seconds, or raise RuntimeError on failure.

    Uses ffprobe (already present in adapter containers — used by mux step).
    No model load, no decode, just header read; cheap enough to call on every
    job. Adapters that don't run ffmpeg-based mux may not have ffprobe; in that
    case this helper raises RuntimeError and callers must catch.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffprobe timed out probing {path}") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"ffprobe failed (rc={e.returncode}) probing {path}: {e.stderr.strip()}"
        ) from e
    out = result.stdout.strip()
    if not out:
        raise RuntimeError(f"ffprobe returned empty duration for {path}")
    try:
        return float(out)
    except ValueError as e:
        raise RuntimeError(f"ffprobe returned non-numeric duration {out!r}") from e


def _auth_header_dict(transfer_auth: Optional[TransferAuth]) -> Dict[str, str]:
    """Materialize the single auth header from a TransferAuth, or return {}."""
    if transfer_auth is None:
        return {}
    return {transfer_auth.header_name: transfer_auth.header_value}


def parse_avatar_request(render_params: Optional[dict]) -> Optional[AvatarRequest]:
    """Parse and validate params.avatar when present."""
    params = render_params or {}
    raw_avatar = params.get("avatar")
    if raw_avatar is None:
        return None
    if not isinstance(raw_avatar, dict) or not raw_avatar:
        raise RuntimeError("params.avatar must be a non-empty object")

    name = str(raw_avatar.get("name") or "").strip()
    uuid = str(raw_avatar.get("uuid") or "").strip() or None
    version_raw = raw_avatar.get("version")
    version: Optional[int] = None
    if version_raw is not None:
        try:
            version = int(version_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("params.avatar.version must be an integer") from exc
        if version < 1:
            raise RuntimeError("params.avatar.version must be >= 1")

    if not name:
        raise RuntimeError("params.avatar.name is required")
    if not _AVATAR_NAME_RE.match(name):
        raise RuntimeError("params.avatar.name has invalid characters")

    legacy_avatar_id = str(params.get("avatar_id") or "").strip()
    if legacy_avatar_id and legacy_avatar_id != name:
        raise RuntimeError("params.avatar.name and params.avatar_id differ")

    config = raw_avatar.get("config")
    if config is not None:
        if not isinstance(config, dict):
            raise RuntimeError("params.avatar.config must be an object")
        encoded = json.dumps(config, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_AVATAR_CONFIG_BYTES:
            raise RuntimeError(
                f"params.avatar.config exceeds {MAX_AVATAR_CONFIG_BYTES} bytes"
            )

    logger.info(
        "[AVATAR VERSION] io.parse_avatar_request name=%s version=%s uuid=%s",
        name,
        version if version is not None else "-",
        uuid or "-",
    )
    return AvatarRequest(name=name, uuid=uuid, version=version, config=config)


def parse_avatar_id(render_params: Optional[dict]) -> Optional[str]:
    """Parse and validate legacy params.avatar_id when present."""
    params = render_params or {}
    avatar_id = str(params.get("avatar_id") or "").strip()
    if not avatar_id:
        return None
    if not _AVATAR_NAME_RE.match(avatar_id):
        raise RuntimeError("params.avatar_id has invalid characters")
    logger.info("[AVATAR VERSION] io.parse_avatar_id avatar_id=%s", avatar_id)
    return avatar_id


def _avatar_dir(name: str, version: int) -> Path:
    return AGNET_AVATAR_ASSETS_DIR / name / str(version)


def _avatar_manifest_path(name: str, version: int) -> Path:
    return _avatar_dir(name, version) / _AVATAR_MANIFEST_FILENAME


def _avatar_ready_path(name: str, version: int) -> Path:
    return _avatar_dir(name, version) / _AVATAR_READY_FILENAME


def _avatar_required_files(name: str) -> dict[str, str]:
    return {
        "video_id": f"{name}.mp4",
        "config_id": "config.json",
        "animation_timeline_id": f"{name}.json",
    }


def _avatar_root_ready(name: str) -> bool:
    base_path = AGNET_AVATAR_ASSETS_DIR / name
    required_files = _avatar_required_files(name)
    return all((base_path / filename).is_file() for filename in required_files.values())


def _find_latest_local_avatar_version(name: str) -> Optional[int]:
    base_path = AGNET_AVATAR_ASSETS_DIR / name
    if not base_path.is_dir():
        return None

    versions: list[int] = []
    for child in base_path.iterdir():
        if not child.is_dir():
            continue
        try:
            version = int(child.name)
        except ValueError:
            continue
        if _avatar_version_ready(name=name, version=version):
            versions.append(version)

    if not versions:
        return None

    versions.sort(reverse=True)
    return versions[0]


def _resolve_local_avatar_id(name: str) -> Optional[MaterializedAvatar]:
    latest_version = _find_latest_local_avatar_version(name)
    if latest_version is not None:
        logger.info(
            "[AVATAR VERSION] io._resolve_local_avatar_id latest_local=%s name=%s",
            latest_version,
            name,
        )
        return MaterializedAvatar(
            name=name,
            uuid="",
            version=latest_version,
            path=_avatar_dir(name, latest_version),
            config=None,
        )

    if _avatar_root_ready(name):
        logger.info(
            "[AVATAR VERSION] io._resolve_local_avatar_id root_layout name=%s",
            name,
        )
        return MaterializedAvatar(
            name=name,
            uuid="",
            version=None,
            path=AGNET_AVATAR_ASSETS_DIR / name,
            config=None,
        )

    return None


def _avatar_version_ready(
    *,
    name: str,
    version: int,
    uuid: Optional[str] = None,
    assets: Optional[dict[str, str]] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    path = _avatar_dir(name, version)
    manifest_path = _avatar_manifest_path(name, version)
    ready_path = _avatar_ready_path(name, version)
    required_files = _avatar_required_files(name)
    if logger:
        logger.info(
            "io_helpers: checking avatar disk path name=%s version=%s uuid=%s path=%s",
            name,
            version,
            uuid or "-",
            path,
        )
    if not path.is_dir():
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=path_not_found path=%s",
                name,
                version,
                path,
            )
        return False

    files_present = all((path / filename).is_file() for filename in required_files.values())
    if not files_present:
        if logger:
            missing = [
                filename
                for filename in required_files.values()
                if not (path / filename).is_file()
            ]
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=missing_files missing=%s path=%s",
                name,
                version,
                ",".join(missing),
                path,
            )
        return False

    if not ready_path.is_file() or not manifest_path.is_file():
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready hit name=%s version=%s mode=files_only path=%s",
                name,
                version,
                path,
            )
        return True

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=manifest_read_error error=%s path=%s",
                name,
                version,
                exc,
                manifest_path,
            )
        return False
    if not isinstance(manifest, dict):
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=manifest_not_object path=%s",
                name,
                version,
                manifest_path,
            )
        return False

    manifest_name = str(manifest.get("name") or "").strip()
    manifest_uuid = str(manifest.get("uuid") or "").strip()
    manifest_engine = str(manifest.get("engine") or "").strip()
    manifest_version = manifest.get("version")
    manifest_assets = manifest.get("assets")
    if (
        manifest_name != name
        or manifest_engine != AGNET_ENGINE_NAME
        or manifest_version != version
        or not manifest_uuid
        or not isinstance(manifest_assets, dict)
    ):
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=manifest_mismatch manifest_name=%s manifest_uuid=%s manifest_engine=%s manifest_version=%s path=%s",
                name,
                version,
                manifest_name or "-",
                manifest_uuid or "-",
                manifest_engine or "-",
                manifest_version,
                manifest_path,
            )
        return False

    if uuid and manifest_uuid != uuid:
        if logger:
            logger.info(
                "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=uuid_mismatch expected_uuid=%s manifest_uuid=%s path=%s",
                name,
                version,
                uuid,
                manifest_uuid,
                manifest_path,
            )
        return False

    for asset_key, filename in required_files.items():
        asset_ref = manifest_assets.get(asset_key)
        if not isinstance(asset_ref, str) or not asset_ref.strip():
            if logger:
                logger.info(
                    "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=manifest_asset_missing asset_key=%s path=%s",
                    name,
                    version,
                    asset_key,
                    manifest_path,
                )
            return False
        if assets is not None and assets.get(asset_key) != asset_ref:
            if logger:
                logger.info(
                    "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=asset_ref_mismatch asset_key=%s expected=%s actual=%s path=%s",
                    name,
                    version,
                    asset_key,
                    assets.get(asset_key),
                    asset_ref,
                    manifest_path,
                )
            return False
        if not (path / filename).is_file():
            if logger:
                logger.info(
                    "[AVATAR VERSION] io._avatar_version_ready miss name=%s version=%s reason=file_missing_after_manifest filename=%s path=%s",
                    name,
                    version,
                    filename,
                    path,
                )
            return False

    if logger:
        logger.info(
            "[AVATAR VERSION] io._avatar_version_ready hit name=%s version=%s mode=manifest_verified path=%s",
            name,
            version,
            path,
        )
    return True


def _data_plane_url(data_plane_base_url: Optional[str], path: str) -> str:
    base_url = (data_plane_base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("data-plane base URL is required for avatar materialization")
    return f"{base_url}{path}"


def _data_plane_json(
    path: str,
    *,
    data_plane_base_url: Optional[str],
    timeout: float,
    transfer_auth: Optional[TransferAuth],
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    url = _data_plane_url(data_plane_base_url, path)
    if logger:
        logger.info("io_helpers: data-plane GET json path=%s", path)
        logger.info("[AVATAR VERSION] io._data_plane_json path=%s", path)
    req = Request(url, method="GET")
    for key, value in _auth_header_dict(transfer_auth).items():
        req.add_header(key, value)

    req.add_header("Accept", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"avatar resolve failed: HTTP {exc.code} url={url}") from exc
    except (URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"avatar resolve failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("avatar resolve response must be a JSON object")
    return payload


def _download_avatar_asset(
    asset_uuid: str,
    dest: Path,
    *,
    data_plane_base_url: Optional[str],
    timeout: float,
    transfer_auth: Optional[TransferAuth],
    logger: Optional[logging.Logger] = None,
) -> None:
    url = _data_plane_url(data_plane_base_url, f"/internal/avatar-assets/{quote(asset_uuid)}/content")
    if logger:
        logger.info(
            "io_helpers: downloading avatar asset asset_uuid=%s dest=%s",
            asset_uuid,
            dest,
        )
    req = Request(url, method="GET")
    for key, value in _auth_header_dict(transfer_auth).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    except HTTPError as exc:
        raise RuntimeError(f"avatar asset download failed: HTTP {exc.code} asset_uuid={asset_uuid}") from exc
    except URLError as exc:
        raise RuntimeError(f"avatar asset download failed: {exc}") from exc


def _resolve_avatar(
    avatar: AvatarRequest,
    *,
    data_plane_base_url: Optional[str],
    timeout: float,
    transfer_auth: Optional[TransferAuth],
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    if logger:
        logger.info(
            "[AVATAR VERSION] io._resolve_avatar name=%s version=%s uuid=%s",
            avatar.name,
            avatar.version if avatar.version is not None else "-",
            avatar.uuid or "-",
        )
    if avatar.uuid:
        return _data_plane_json(
            f"/internal/avatars/{quote(avatar.uuid)}",
            data_plane_base_url=data_plane_base_url,
            timeout=timeout,
            transfer_auth=transfer_auth,
            logger=logger,
        )

    query = {"name": avatar.name, "engine": AGNET_ENGINE_NAME}
    if avatar.version is not None:
        query["version"] = str(avatar.version)
    return _data_plane_json(
        f"/internal/avatars/resolve?{urlencode(query)}",
        data_plane_base_url=data_plane_base_url,
        timeout=timeout,
        transfer_auth=transfer_auth,
        logger=logger,
    )


def _resolved_avatar_version(resolved: dict[str, Any]) -> int:
    try:
        return int(resolved.get("version"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("resolved avatar.version must be an integer") from exc


def _materialize_avatar_version(
    resolved: dict[str, Any],
    *,
    data_plane_base_url: Optional[str],
    timeout: float,
    transfer_auth: Optional[TransferAuth],
    logger: Optional[logging.Logger] = None,
) -> MaterializedAvatar:
    name = str(resolved.get("name") or "").strip()
    uuid = str(resolved.get("uuid") or "").strip()
    engine = str(resolved.get("engine") or "").strip()
    version = _resolved_avatar_version(resolved)
    assets = resolved.get("assets")
    if not name or not uuid or engine != AGNET_ENGINE_NAME:
        raise RuntimeError(
            f"resolved avatar does not match Agnet adapter: name={name!r} uuid={uuid!r} engine={engine!r}"
        )
    if not isinstance(assets, dict):
        raise RuntimeError("resolved avatar.assets must be an object")

    required = {
        "video_id": assets.get("video_id"),
        "config_id": assets.get("config_id"),
        "animation_timeline_id": assets.get("animation_timeline_id"),
    }
    if any(not isinstance(value, str) or not value for value in required.values()):
        raise RuntimeError("resolved avatar.assets is missing required asset uuids")
    if logger:
        logger.info(
            "[AVATAR VERSION] io._materialize_avatar_version resolved version=%s name=%s uuid=%s",
            version,
            name,
            uuid,
        )

    final_path = _avatar_dir(name, version)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if _avatar_version_ready(
        name=name,
        version=version,
        uuid=uuid,
        assets=required,
        logger=logger,
    ):
        return MaterializedAvatar(name=name, uuid=uuid, version=version, path=final_path, config=None)

    if not AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED:
        raise RuntimeError(
            "avatar assets are not materialized locally and AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED=false"
        )

    final_path.mkdir(parents=True, exist_ok=True)
    _download_avatar_asset(
        required["video_id"],
        final_path / f"{name}.mp4",
        data_plane_base_url=data_plane_base_url,
        timeout=timeout,
        transfer_auth=transfer_auth,
        logger=logger,
    )
    _download_avatar_asset(
        required["config_id"],
        final_path / "config.json",
        data_plane_base_url=data_plane_base_url,
        timeout=timeout,
        transfer_auth=transfer_auth,
        logger=logger,
    )
    _download_avatar_asset(
        required["animation_timeline_id"],
        final_path / f"{name}.json",
        data_plane_base_url=data_plane_base_url,
        timeout=timeout,
        transfer_auth=transfer_auth,
        logger=logger,
    )
    (final_path / _AVATAR_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "uuid": uuid,
                "engine": AGNET_ENGINE_NAME,
                "version": version,
                "assets": required,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (final_path / _AVATAR_READY_FILENAME).write_text("", encoding="utf-8")

    return MaterializedAvatar(name=name, uuid=uuid, version=version, path=final_path, config=None)


async def materialize_avatar_assets(
    render_params: Optional[dict],
    *,
    data_plane_base_url: Optional[str],
    timeout: float,
    transfer_auth: Optional[TransferAuth],
    logger: Optional[logging.Logger] = None,
) -> Optional[MaterializedAvatar]:
    """Resolve and prepare avatar assets for params.avatar or legacy params.avatar_id."""
    if logger:
        logger.info("[AVATAR VERSION] io.materialize_avatar_assets start")
    avatar = parse_avatar_request(render_params)
    if avatar is not None:
        resolved_uuid = avatar.uuid
        if not resolved_uuid:
            identity = await asyncio.to_thread(
                _resolve_avatar,
                AvatarRequest(
                    name=avatar.name,
                    uuid=None,
                    version=avatar.version,
                    config=None,
                ),
                data_plane_base_url=data_plane_base_url,
                timeout=timeout,
                transfer_auth=transfer_auth,
                logger=logger,
            )
            resolved_uuid = str(identity.get("uuid") or "").strip()
            if not resolved_uuid:
                raise RuntimeError("resolved avatar.uuid is required")
            if logger:
                logger.info(
                    "[AVATAR VERSION] io.materialize_avatar_assets resolved uuid=%s for name=%s",
                    resolved_uuid,
                    avatar.name,
                )

        if avatar.version is not None and _avatar_version_ready(
            name=avatar.name,
            version=avatar.version,
            uuid=resolved_uuid,
            logger=logger,
        ):
            return MaterializedAvatar(
                name=avatar.name,
                uuid=resolved_uuid,
                version=avatar.version,
                path=_avatar_dir(avatar.name, avatar.version),
                config=avatar.config,
            )

        resolved = await asyncio.to_thread(
            _resolve_avatar,
            AvatarRequest(
                name=avatar.name,
                uuid=resolved_uuid,
                version=None,
                config=None,
            ),
            data_plane_base_url=data_plane_base_url,
            timeout=timeout,
            transfer_auth=transfer_auth,
            logger=logger,
        )
        resolved_version = _resolved_avatar_version(resolved)
        if avatar.version is not None and resolved_version != avatar.version:
            raise RuntimeError(
                f"resolved avatar version mismatch for uuid={resolved_uuid}: requested={avatar.version} resolved={resolved_version}"
            )
        materialized = await asyncio.to_thread(
            _materialize_avatar_version,
            resolved,
            data_plane_base_url=data_plane_base_url,
            timeout=timeout,
            transfer_auth=transfer_auth,
            logger=logger,
        )
        if logger:
            logger.info(
                "[AVATAR VERSION] io.materialize_avatar_assets materialized version=%s name=%s",
                materialized.version if materialized.version is not None else "-",
                materialized.name,
            )
        return MaterializedAvatar(
            name=materialized.name,
            uuid=materialized.uuid,
            version=materialized.version,
            path=materialized.path,
            config=avatar.config,
        )

    avatar_id = parse_avatar_id(render_params)
    if avatar_id is None:
        return None

    if AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED:
        local_avatar = _resolve_local_avatar_id(avatar_id)
        if local_avatar is not None:
            if logger:
                logger.info(
                    "io_helpers: using local avatar_id=%s version=%s path=%s before data-plane resolve",
                    local_avatar.name,
                    local_avatar.version if local_avatar.version is not None else "-",
                    local_avatar.path,
                )
                logger.info(
                    "[AVATAR VERSION] io.materialize_avatar_assets local-first avatar_id version=%s name=%s",
                    local_avatar.version if local_avatar.version is not None else "-",
                    local_avatar.name,
                )
            return local_avatar

        resolved = await asyncio.to_thread(
            _resolve_avatar,
            AvatarRequest(name=avatar_id),
            data_plane_base_url=data_plane_base_url,
            timeout=timeout,
            transfer_auth=transfer_auth,
            logger=logger,
        )
        return await asyncio.to_thread(
            _materialize_avatar_version,
            resolved,
            data_plane_base_url=data_plane_base_url,
            timeout=timeout,
            transfer_auth=transfer_auth,
            logger=logger,
        )

    local_avatar = _resolve_local_avatar_id(avatar_id)
    if local_avatar is not None:
        if logger:
            logger.info(
                "io_helpers: using local avatar_id=%s version=%s path=%s with downloads disabled",
                local_avatar.name,
                local_avatar.version if local_avatar.version is not None else "-",
                local_avatar.path,
            )
            logger.info(
                "[AVATAR VERSION] io.materialize_avatar_assets local avatar_id version=%s name=%s",
                local_avatar.version if local_avatar.version is not None else "-",
                local_avatar.name,
            )
        return local_avatar

    raise RuntimeError(
        f"avatar_id={avatar_id} is not available locally and AGNET_AVATAR_ASSET_DOWNLOAD_ENABLED=false"
    )


def _http_range_probe(url: str, timeout: float, headers: Optional[Dict[str, str]] = None) -> None:
    """
    Validate worker-provided input URL via Range GET (HEAD alternative).

    Fetches only the first byte to verify URL is reachable and valid.
    Faster than full GET, more reliable than HEAD (some CDNs don't support HEAD).

    Args:
        url: Input URL to validate
        timeout: HTTP timeout in seconds

    Raises:
        RuntimeError: If status is not 200/206
        URLError: If network error or timeout
    """
    req = Request(url, method="GET")
    req.add_header("Range", "bytes=0-0")
    for key, value in (headers or {}).items():
        k = str(key or "").strip()
        v = str(value or "").strip()
        if k and v:
            req.add_header(k, v)
    opener = build_opener(HTTPHandler())
    with opener.open(req, timeout=timeout) as resp:
        if resp.status not in (200, 206):
            raise RuntimeError(f"probe unexpected status {resp.status}")


def _http_stream_get(
    url: str,
    dest: Path,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    """
    Download input URL to file (streaming, 1MB chunks).

    Streams download in 1MB chunks to avoid loading entire file into memory.

    Args:
        url: Input URL
        dest: Destination file path
        timeout: HTTP timeout in seconds

    Raises:
        URLError: If network error or timeout
        IOError: If file write fails
    """
    opener = build_opener(HTTPHandler())
    req = Request(url, method="GET")
    for key, value in (headers or {}).items():
        k = str(key or "").strip()
        v = str(value or "").strip()
        if k and v:
            req.add_header(k, v)
    with opener.open(req, timeout=timeout) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)  # 1MB chunks
            if not chunk:
                break
            f.write(chunk)


async def validate_inputs_via_range(
    audio_url: str,
    photo_url: Optional[str],
    timeout: float,
    *,
    transfer_auth: Optional[TransferAuth] = None,
) -> None:
    """
    Validate both input URLs via Range probe (parallel).

    Fast validation before full download: checks URLs are reachable and valid.
    Runs probes in parallel to minimize latency.

    Args:
        audio_url: Worker-provided audio input URL
        photo_url: Worker-provided photo input URL
        timeout: HTTP timeout in seconds per request
        transfer_auth: Optional credential applied to both probes.

    Raises:
        RuntimeError: If either URL probe fails (404, 403, auth mismatch)
    """
    headers = _auth_header_dict(transfer_auth)
    probes = [
        asyncio.to_thread(_http_range_probe, audio_url, timeout, headers),
    ]
    if photo_url:
        probes.append(asyncio.to_thread(_http_range_probe, photo_url, timeout, headers))
    await asyncio.gather(*probes)


async def fetch_inputs_streaming(
    stage: Stage,
    audio_url: str,
    photo_url: Optional[str],
    timeout: float,
    *,
    audio_filename: str = "audio.wav",
    photo_filename: str = "photo.png",
    transfer_auth: Optional[TransferAuth] = None,
) -> Dict[str, Path]:
    """
    Fetch inputs from URLs to stage.inputs/ (parallel, streaming).

    Downloads audio and photo in parallel to minimize fetch time.
    Streams downloads in 1MB chunks to avoid memory spikes.

    Args:
        stage: Per-job staging directory structure
        audio_url: Input URL for audio
        photo_url: Input URL for photo
        timeout: HTTP timeout in seconds per request
        transfer_auth: Optional credential applied to both downloads.

    Returns:
        Dict mapping "audio" → audio_path, "photo" → photo_path

    Raises:
        URLError: If download fails (network error, timeout)
        IOError: If file write fails
    """
    audio_path = stage.inputs / (audio_filename or "audio.wav")
    photo_path = stage.inputs / (photo_filename or "photo.png")
    headers = _auth_header_dict(transfer_auth)
    tasks = [
        asyncio.to_thread(_http_stream_get, audio_url, audio_path, timeout, headers),
    ]
    if photo_url:
        tasks.append(asyncio.to_thread(_http_stream_get, photo_url, photo_path, timeout, headers))
    await asyncio.gather(*tasks)
    return {"audio": audio_path, "photo": photo_path}


async def upload_output_streaming(
    stage: Stage,
    put_url: str,
    content_type: str,
    path: Path,
    timeout: float,
    transfer_auth: Optional[TransferAuth] = None,
    logger=None,
) -> None:
    """
    Upload output file to worker-provided upload URL via curl (streaming).

    Uses curl -T (PUT) to stream upload without loading entire file into memory.
    Retries up to 3 times on network errors.

    Args:
        stage: Per-job staging directory (logs upload.txt here)
        put_url: Upload URL for output destination
        content_type: MIME type (e.g., "video/mp4")
        path: Local file path to upload
        timeout: HTTP connect timeout in seconds
        transfer_auth: Optional credential applied to the PUT.
        logger: Optional logger for diagnostics

    Raises:
        RuntimeError: If upload fails after retries (logs stderr to stage.logs/upload.txt)
    """
    size = (await asyncio.to_thread(path.stat)).st_size if path.exists() else 0
    ct = content_type or "application/octet-stream"
    if logger:
        logger.info("runner: uploading size=%d content_type=%s", size, ct)

    log_path = stage.logs / "upload.txt"
    cmd = [
        "curl", "-sS", "--fail", "--retry", "3",
        "--connect-timeout", str(int(max(1, timeout))),
        "-T", str(path),
        "-H", f"Content-Type: {ct}",
    ]
    for key, value in _auth_header_dict(transfer_auth).items():
        cmd.extend(["-H", f"{key}: {value}"])
    cmd.append(put_url)
    rc = await stream_subprocess(
        cmd,
        cwd=None,
        log_path=log_path,
        logger=logger,
        prefix="upload",
    )
    if rc != 0:
        raise RuntimeError(f"upload failed with exit code {rc}; see {log_path}")
