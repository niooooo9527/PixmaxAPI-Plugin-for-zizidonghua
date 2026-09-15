from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

_API_CLIENT_MODULE_NAME = "_typetale_pixmax_openapi_video_api_client"
_API_CLIENT_PATH = Path(__file__).resolve().with_name("api_client.py")
_api_client_spec = importlib.util.spec_from_file_location(_API_CLIENT_MODULE_NAME, _API_CLIENT_PATH)
if not _api_client_spec or not _api_client_spec.loader:
    raise ImportError(f"无法加载 Pixmax 视频 API 客户端：{_API_CLIENT_PATH}")
_api_client_module = importlib.util.module_from_spec(_api_client_spec)
sys.modules[_API_CLIENT_MODULE_NAME] = _api_client_module
_api_client_spec.loader.exec_module(_api_client_module)

try:
    from plugin_utils import load_plugin_config, save_plugin_config
except ImportError:
    def load_plugin_config(plugin_file: str) -> dict[str, Any]:
        path = Path(plugin_file).with_name("config.json")
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_plugin_config(plugin_file: str, params: dict[str, Any]) -> None:
        Path(plugin_file).with_name("config.json").write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

DEFAULT_BASE_URL = _api_client_module.DEFAULT_BASE_URL
PixmaxApiClient = _api_client_module.PixmaxApiClient
PixmaxApiError = _api_client_module.PixmaxApiError
find_asset_urls = _api_client_module.find_asset_urls
save_asset = _api_client_module.save_asset
normalize_base_url = _api_client_module.normalize_base_url


_PLUGIN_FILE = __file__
_DEFAULT_PARAMS = {
    "api_key": "",
    "api_base_url": DEFAULT_BASE_URL,
    "model": "",
    "resolution": "720P",
    "aspect_ratio": "16:9",
    "duration": "5",
    "include_audio": False,
    "bitrate_mode": "standard",
    "count": 1,
    "timeout": 1800,
    "poll_interval": 5.0,
    "download_timeout": 1800,
    "_project_uuid": "",
}


def get_info() -> dict[str, Any]:
    return {
        "name": "Pixmax API 视频",
        "description": "只需 Pixmax API Key，自动连接项目并读取视频模型。",
        "version": "1.1.0",
        "type": "video",
    }


def get_params() -> dict[str, Any]:
    params = _DEFAULT_PARAMS.copy()
    stored = load_plugin_config(_PLUGIN_FILE)
    if isinstance(stored, dict):
        params.update(stored)
    if not str(params.get("api_key") or "").strip() and os.getenv("PIXMAX_OPENAPI_KEY"):
        params["api_key"] = os.getenv("PIXMAX_OPENAPI_KEY", "").strip()
    params["api_base_url"] = DEFAULT_BASE_URL
    params["connected"] = bool(str(params.get("_project_uuid") or "").strip())
    return params


def _api_key(params: dict[str, Any]) -> str:
    return os.getenv("PIXMAX_OPENAPI_KEY", "").strip() or str(params.get("api_key") or "").strip()


def _api_base_url(params: dict[str, Any]) -> str:
    return DEFAULT_BASE_URL


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "有声"}
    return bool(value) if value is not None else default


def _schema_value(schema: Any, current: Any, fallback: str = "") -> str:
    if not isinstance(schema, dict):
        return str(current if current is not None else fallback)
    value = "" if current is None else str(current).strip()
    options = schema.get("options") or schema.get("values") or schema.get("enum") or schema.get("inValues")
    if isinstance(options, list):
        normalized: list[str] = []
        for option in options:
            if isinstance(option, dict):
                item = option.get("value") if option.get("value") is not None else option.get("name")
            else:
                item = option
            normalized.append("" if item is None else str(item))
        if value in normalized:
            return value
        default = schema.get("default") if schema.get("default") is not None else schema.get("defaultValue")
        if default is not None and str(default) in normalized:
            return str(default)
        if normalized:
            return normalized[0]
    default = schema.get("default") if schema.get("default") is not None else schema.get("defaultValue")
    return value or (str(default) if default is not None else fallback)


def _schema_for(schemas: dict[str, Any], name: str) -> tuple[str, Any] | tuple[None, None]:
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())
    for key, value in schemas.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized == wanted:
            return str(key), value
    return None, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _range_bounds(value: Any) -> tuple[float | None, float | None]:

    if isinstance(value, dict):
        minimum = value.get("min") if value.get("min") is not None else value.get("minimum")
        maximum = value.get("max") if value.get("max") is not None else value.get("maximum")
        return _number(minimum), _number(maximum)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _number(value[0]), _number(value[1])
    text = str(value or "").strip()
    if text.startswith(("[", "(")) and text.endswith(("]", ")")) and "," in text:
        left, right = text[1:-1].split(",", 1)
        return _number(left), _number(right)
    return None, None


def _option_value(option: Any) -> str:
    if isinstance(option, dict):
        value = option.get("value")
        if value is None:
            value = option.get("name")
    else:
        value = option
    return str(value or "").strip()


def _refer_model_schema(model_row: dict[str, Any]) -> dict[str, Any] | None:
    schemas = model_row.get("params") if isinstance(model_row.get("params"), dict) else {}
    _, schema = _schema_for(schemas, "referModel")
    return schema if isinstance(schema, dict) else None


def _refer_model_options(model_row: dict[str, Any]) -> list[dict[str, Any]]:
    schema = _refer_model_schema(model_row)
    if not schema:
        return []
    raw_options = schema.get("options") or schema.get("values") or schema.get("enum") or schema.get("inValues")
    if not isinstance(raw_options, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_options:
        value = _option_value(raw)
        if not value:
            continue
        result.append(raw if isinstance(raw, dict) else {"value": value})
    return result


def _input_dependency_matches_count(dependency: dict[str, Any], reference_count: int) -> bool:
    dependency_type = str(dependency.get("type") or "").strip().lower()
    if dependency_type not in {"inputnodetypes", "inputnodes", "input", "asset", "assets", "reference", "references"}:
        return True
    bounds_value = dependency.get("length")
    if bounds_value is None:
        bounds_value = dependency.get("betweenValue")
    minimum, maximum = _range_bounds(bounds_value)
    if minimum is None and dependency.get("min") is not None:
        minimum = _number(dependency.get("min"))
    if maximum is None and dependency.get("max") is not None:
        maximum = _number(dependency.get("max"))
    return (minimum is None or reference_count >= minimum) and (maximum is None or reference_count <= maximum)


def _option_supports_reference_count(option: dict[str, Any], reference_count: int) -> bool:
    dependencies = option.get("dependOn") or []
    if isinstance(dependencies, dict):
        dependencies = [dependencies]
    for dependency in dependencies:
        if isinstance(dependency, dict) and not _input_dependency_matches_count(dependency, reference_count):
            return False
    mode = re.sub(r"[^a-z0-9]", "", _option_value(option).lower())
    has_explicit_input_limit = any(
        isinstance(dependency, dict)
        and str(dependency.get("type") or "").strip().lower()
        in {"inputnodetypes", "inputnodes", "input", "asset", "assets", "reference", "references"}
        for dependency in (dependencies if isinstance(dependencies, list) else [])
    )
    if reference_count == 0:
        return mode == "texttovideo"
    if mode == "texttovideo":
        return False
    if reference_count > 1 and mode == "imagetovideo" and not has_explicit_input_limit:
        return False
    if reference_count > 1 and mode in {"firstandlastframe", "firstlastframe"}:
        return False
    return True


def _model_reference_mode(model_row: dict[str, Any], reference_count: int) -> str:

    options = _refer_model_options(model_row)
    if not options:
        if reference_count == 0:
            return "textToVideo"
        if reference_count == 1:
            return "imageToVideo"
        return ""
    preferred = (
        ["textToVideo"]
        if reference_count == 0
        else ["imageToVideo", "imageRefer", "referToVideo"]
        if reference_count == 1
        else ["referToVideo", "imageRefer", "omniReference", "allReference"]
    )
    available = [
        (_option_value(option), option)
        for option in options
        if _option_supports_reference_count(option, reference_count)
    ]
    for wanted in preferred:
        wanted_normalized = re.sub(r"[^a-z0-9]", "", wanted.lower())
        for value, option in available:
            if re.sub(r"[^a-z0-9]", "", value.lower()) == wanted_normalized:
                return _option_value(option)
    return available[0][0] if available else ""


def _is_omni_reference_model(model_row: dict[str, Any]) -> bool:
    identity = " ".join((str(model_row.get("code") or ""), str(model_row.get("name") or ""))).upper()
    return "OMNI" in identity or "全能" in identity


def _select_model_for_references(
    models: list[dict[str, Any]], requested_model: str, reference_count: int
) -> tuple[dict[str, Any], str, bool]:

    if not models:
        raise PixmaxApiError(" API 没有返回可用的视频模型。")
    requested = next((row for row in models if row.get("code") == requested_model), models[0])
    current_mode = _model_reference_mode(requested, reference_count)
    if current_mode:
        return requested, current_mode, False

    candidates: list[tuple[int, int, dict[str, Any], str]] = []
    for index, row in enumerate(models):
        if row.get("restricted") is True:
            continue
        mode = _model_reference_mode(row, reference_count)
        if not mode:
            continue

        candidates.append((0 if _is_omni_reference_model(row) else 1, index, row, mode))
    if not candidates:
        code = str(requested.get("code") or requested_model or "当前模型")
        raise PixmaxApiError(f"模型 {code} 不支持 {reference_count} 张参考图，且没有可用的多图参考模型。")
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, selected, mode = candidates[0]
    return selected, mode, True


def _report_model_switch(callback: Any, requested: dict[str, Any], selected: dict[str, Any], reference_count: int) -> None:
    if not callable(callback):
        return
    requested_name = str(requested.get("name") or requested.get("code") or "当前模型")
    selected_name = str(selected.get("name") or selected.get("code") or "全能参考模型")
    message = f"当前模型“{requested_name}”不支持 {reference_count} 张参考图，已自动切换到“{selected_name}”本次生成。"
    try:
        callback(message, None)
    except TypeError:
        try:
            callback(message)
        except Exception:
            pass
    except Exception:
        pass


def _build_task_params(
    model_row: dict[str, Any],
    params: dict[str, Any],
    has_input_assets: bool,
    refer_model: str | None = None,
) -> dict[str, Any]:

    schemas = model_row.get("params") if isinstance(model_row.get("params"), dict) else {}
    if not schemas:
        return {}
    user_values = {
        "resolution": str(params.get("resolution") or "720P"),
        "aspectRatio": str(params.get("aspect_ratio") or "16:9"),
        "duration": str(params.get("duration") or "5"),
        "includeAudio": "true" if _as_bool(params.get("include_audio"), False) else "false",
        "bitrateMode": str(params.get("bitrate_mode") or "standard"),
        "referModel": refer_model or ("imageToVideo" if has_input_assets else "textToVideo"),
        "outputFormat": "mp4",
        "count": str(_as_int(params.get("count"), 1, 1, 4)),
    }
    task_params: dict[str, Any] = {}
    for schema_name, schema in schemas.items():
        if re.sub(r"[^a-z0-9]", "", str(schema_name).lower()) in {"prompt", "model", "nodetype"}:
            continue
        canonical = next(
            (name for name in user_values if re.sub(r"[^a-z0-9]", "", name.lower()) == re.sub(r"[^a-z0-9]", "", str(schema_name).lower())),
            None,
        )
        current = user_values.get(canonical, "") if canonical else ""
        value = _schema_value(schema, current)
        if value != "":
            task_params[str(schema_name)] = value
    return task_params


def _safe_component(value: Any, fallback: str) -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "").strip()).strip(" .")
    return text or fallback


def _position_at(positions: Any, index: int) -> Any:
    if isinstance(positions, (list, tuple)) and positions:
        return positions[index] if index < len(positions) else index
    return index


def _reference_paths(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    elif value:
        values = [value]
    else:
        return []
    paths: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("path") or item.get("file") or item.get("file_path") or item.get("url")
        text = str(item or "").strip()
        if text:
            paths.append(text)
    return paths


_ASSET_METADATA_SUFFIX = ".pixmax.json"
_COMPLIANCE_TTL_SECONDS = 30 * 24 * 60 * 60


def _reference_entries(value: Any) -> list[tuple[str, str]]:

    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    elif value:
        values = [value]
    else:
        return []
    entries: list[tuple[str, str]] = []
    for item in values:
        asset_uuid = ""
        if isinstance(item, dict):
            asset_uuid = str(
                item.get("asset_uuid")
                or item.get("assetsUuid")
                or item.get("assetUuid")
                or item.get("assetUUID")
                or ""
            ).strip()
            item = item.get("path") or item.get("file") or item.get("file_path") or item.get("url") or ""
        path = str(item or "").strip()
        if path or asset_uuid:
            entries.append((path, asset_uuid))
    return entries


def _read_asset_metadata(path: str, project_uuid: str) -> dict[str, Any]:

    if not path or path.startswith(("http://", "https://")):
        return {}
    metadata_path = Path(f"{Path(path)}{_ASSET_METADATA_SUFFIX}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    stored_project = str(metadata.get("project_uuid") or "").strip()
    if stored_project and project_uuid and stored_project != project_uuid:
        return {}
    return metadata


def _metadata_asset_uuid(metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("asset_uuid")
        or metadata.get("assetsUuid")
        or metadata.get("assetUuid")
        or metadata.get("assetUUID")
        or ""
    ).strip()


def _compliance_is_valid(metadata: dict[str, Any], asset_uuid: str, now: float | None = None) -> bool:
    if _metadata_asset_uuid(metadata) != str(asset_uuid or "").strip():
        return False
    try:
        checked_at = float(metadata.get("compliance_checked_at"))
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    return 0 <= current - checked_at < _COMPLIANCE_TTL_SECONDS


def _save_asset_metadata(
    path: str,
    asset_uuid: str,
    project_uuid: str,
    api_base_url: str,
    compliance_checked_at: float | None = None,
) -> None:

    if not path or path.startswith(("http://", "https://")) or not asset_uuid:
        return
    payload = {
        "asset_uuid": str(asset_uuid).strip(),
        "project_uuid": str(project_uuid or "").strip(),
        "api_base_url": str(api_base_url or "").strip(),
        "source": "pixmax_openapi",
    }
    if compliance_checked_at is not None:
        payload["compliance_checked_at"] = float(compliance_checked_at)
    metadata_path = Path(f"{Path(path)}{_ASSET_METADATA_SUFFIX}")
    try:
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _is_seedance_model(model: str, model_row: dict[str, Any] | None = None) -> bool:
    identity = " ".join((str(model or ""), str((model_row or {}).get("name") or ""))).upper()
    return "SEEDANCE" in identity or "PIXDANCE" in identity


def _is_recoverable_asset_url_error(error: Exception) -> bool:

    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "failed to download media",
            "server gave http response to https client",
            "fetch-object",
        )
    )


def _replace_asset_uuid(input_assets: list[str], old_uuid: str, new_uuid: str) -> None:
    for index, value in enumerate(input_assets):
        if value == old_uuid:
            input_assets[index] = new_uuid


def _reupload_reference_assets(
    client: PixmaxApiClient,
    project_uuid: str,
    reference_assets: list[dict[str, Any]],
    input_assets: list[str],
    api_base_url: str,
) -> list[str]:

    replacements: dict[str, str] = {}
    for item in reference_assets:
        old_uuid = str(item.get("asset_uuid") or "").strip()
        path = str(item.get("path") or "").strip()
        if old_uuid in replacements:
            item["asset_uuid"] = replacements[old_uuid]
            item["metadata"] = {}
            continue
        if not path or path.startswith(("http://", "https://")):
            raise PixmaxApiError("参考图资产地址异常，且无法从本地文件重新上传。请重新选择参考图。")
        new_uuid = client.upload_asset(project_uuid, path)
        replacements[old_uuid] = new_uuid
        _replace_asset_uuid(input_assets, old_uuid, new_uuid)
        item["asset_uuid"] = new_uuid
        item["metadata"] = {}
        _save_asset_metadata(path, new_uuid, project_uuid, api_base_url)
    return list(dict.fromkeys(input_assets))


def _is_video_base64(value: Any) -> bool:
    text = str(value or "").strip()
    if text.startswith("data:"):
        return text.lower().startswith("data:video/")
    try:
        decoded = base64.b64decode(text, validate=True)
    except Exception:
        return False
    header = decoded[:64]
    return b"ftyp" in header or header.startswith(b"\x1a\x45\xdf\xa3") or (
        header.startswith(b"RIFF") and b"AVI" in header[:16]
    )


def _find_base64(value: Any, media_type: str = "") -> str:
    if isinstance(value, dict):
        for key in ("videoBase64", "videoData", "videoContent", "base64", "data", "content"):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate) > 100:
                if str(media_type or "").strip().lower() == "video" and not _is_video_base64(candidate):
                    continue
                try:
                    base64.b64decode(candidate.split(",", 1)[-1], validate=True)
                    return candidate.split(",", 1)[-1]
                except Exception:
                    pass
        for child in value.values():
            found = _find_base64(child, media_type)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_base64(child, media_type)
            if found:
                return found
    return ""


def _is_probable_video_source(source: Any) -> bool:
    text = str(source or "").strip().lower()
    if text.startswith("data:image/"):
        return False
    if text.startswith("data:video/"):
        return True
    return not bool(re.search(r"\.(?:png|jpe?g|webp|gif|bmp|avif)(?:[?#].*)?$", text))


def _save_internal(params: dict[str, Any], api_key: str, project_uuid: str, api_base_url: str) -> None:
    stored = load_plugin_config(_PLUGIN_FILE)
    current = {
        key: value
        for key, value in (stored.items() if isinstance(stored, dict) else [])
        if key in _DEFAULT_PARAMS and key != "api_base_url"
    }
    current.update({"api_key": api_key, "api_base_url": api_base_url, "_project_uuid": project_uuid})
    save_plugin_config(_PLUGIN_FILE, current)


def _connect(params: dict[str, Any]) -> dict[str, Any]:
    api_key = _api_key(params)
    api_base_url = _api_base_url(params)
    client = PixmaxApiClient(api_key=api_key, base_url=api_base_url, timeout=30)
    project_uuid, models = client.connect("GENERATE_VIDEO", str(params.get("_project_uuid") or ""))
    _save_internal(params, api_key, project_uuid, api_base_url)
    return {
        "ok": True,
        "connected": True,
        "message": f"Pixmax 已连接，已获取 {len(models)} 个视频模型。",
        "models": models,
        "project_ready": True,
    }


def generate(context: dict[str, Any]) -> list[str]:
    prompt = str(context.get("prompt") or "").strip()
    if not prompt:
        raise Exception("PLUGIN_ERROR:::提示词不能为空。")
    params = context.get("plugin_params") or get_params()
    if not isinstance(params, dict):
        params = get_params()
    api_key = _api_key(params)
    if not api_key:
        raise Exception("PLUGIN_ERROR:::请先在插件设置中填写 Pixmax API Key，并点击“连接并获取模型”。")
    output_dir = Path(context.get("output_dir") or context.get("project_path") or ".").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        api_base_url = _api_base_url(params)
        client = PixmaxApiClient(api_key=api_key, base_url=api_base_url, timeout=min(_as_int(params.get("timeout"), 1800, 10, 7200), 300))
        project_uuid, models = client.connect("GENERATE_VIDEO", str(params.get("_project_uuid") or ""))
        _save_internal(params, api_key, project_uuid, api_base_url)
        requested_model = str(params.get("model") or "").strip()
        requested_row = next((row for row in models if row.get("code") == requested_model), models[0])
        input_assets: list[str] = []
        reference_assets: list[dict[str, Any]] = []
        for path, known_asset_uuid in _reference_entries(context.get("reference_images")):
            metadata = _read_asset_metadata(path, project_uuid)
            asset_uuid = known_asset_uuid or _metadata_asset_uuid(metadata)
            if asset_uuid:
                input_assets.append(asset_uuid)
                reference_assets.append({"path": path, "asset_uuid": asset_uuid, "metadata": metadata})
                continue
            if not path:
                continue
            if path.startswith(("http://", "https://")):
                raise PixmaxApiError("参考图必须是本地文件。")
            asset_uuid = client.upload_asset(project_uuid, path)
            input_assets.append(asset_uuid)
            reference_assets.append({"path": path, "asset_uuid": asset_uuid, "metadata": {}})
            _save_asset_metadata(path, asset_uuid, project_uuid, api_base_url)
        input_assets = list(dict.fromkeys(input_assets))
        model_row, refer_model, switched = _select_model_for_references(
            models,
            str(requested_row.get("code") or requested_model),
            len(input_assets),
        )
        model = str(model_row.get("code") or "").strip()
        if switched:
            _report_model_switch(context.get("progress_callback"), requested_row, model_row, len(input_assets))
        if _is_seedance_model(model, model_row) and input_assets:
            pending_assets = list(dict.fromkeys(
                item["asset_uuid"]
                for item in reference_assets
                if item["asset_uuid"] and not _compliance_is_valid(item["metadata"], item["asset_uuid"])
            ))
            for pending_uuid in pending_assets:
                item = next(
                    (entry for entry in reference_assets if entry["asset_uuid"] == pending_uuid),
                    None,
                )
                if item is None:
                    continue
                try:
                    client.check_asset_compliance([pending_uuid])
                except PixmaxApiError as compliance_error:
                    path = str(item.get("path") or "").strip()
                    if not path or not _is_recoverable_asset_url_error(compliance_error):
                        raise
                    input_assets = _reupload_reference_assets(
                        client,
                        project_uuid,
                        [item],
                        input_assets,
                        api_base_url,
                    )
                    new_uuid = item["asset_uuid"]
                    try:
                        client.check_asset_compliance([new_uuid])
                    except PixmaxApiError as retry_error:
                        raise PixmaxApiError(
                            f"参考图资产审核失败，重新上传后仍无法访问：{retry_error}"
                        ) from retry_error
                checked_at = time.time()
                _save_asset_metadata(item["path"], item["asset_uuid"], project_uuid, api_base_url, checked_at)
        task_params = _build_task_params(model_row, params, bool(input_assets), refer_model)
        try:
            response, sources = client.generate(
                project_uuid,
                "GENERATE_VIDEO",
                model,
                prompt,
                task_params,
                input_asset_uuids=input_assets,
                timeout=_as_int(params.get("timeout"), 1800, 10, 7200),
                interval=_as_float(params.get("poll_interval"), 5.0, 0.2, 60.0),
                progress_callback=context.get("progress_callback"),
            )
        except PixmaxApiError as generation_error:
            if not input_assets or not reference_assets or not _is_recoverable_asset_url_error(generation_error):
                raise
            callback = context.get("progress_callback")
            if callable(callback):
                try:
                    callback("参考素材地址无法访问，正在重新上传参考图并重试视频生成。", None)
                except TypeError:
                    try:
                        callback("参考素材地址无法访问，正在重新上传参考图并重试视频生成。")
                    except Exception:
                        pass
                except Exception:
                    pass
            input_assets = _reupload_reference_assets(
                client,
                project_uuid,
                reference_assets,
                input_assets,
                api_base_url,
            )
            if _is_seedance_model(model, model_row) and input_assets:
                client.check_asset_compliance(input_assets)
                checked_at = time.time()
                for item in reference_assets:
                    _save_asset_metadata(item["path"], item["asset_uuid"], project_uuid, api_base_url, checked_at)
            response, sources = client.generate(
                project_uuid,
                "GENERATE_VIDEO",
                model,
                prompt,
                task_params,
                input_asset_uuids=input_assets,
                timeout=_as_int(params.get("timeout"), 1800, 10, 7200),
                interval=_as_float(params.get("poll_interval"), 5.0, 0.2, 60.0),
                progress_callback=context.get("progress_callback"),
            )
        if not sources:
            sources = find_asset_urls(response, api_base_url, input_assets)
        if not sources:
            encoded = _find_base64(response, "video")
            if encoded:
                sources = [f"data:video/mp4;base64,{encoded}"]
        sources = [source for source in sources if _is_probable_video_source(source)]
        if not sources:
            raise PixmaxApiError("Pixmax 已完成，但响应中没有可下载的视频地址。")
        viewer_index = _as_int(context.get("viewer_index"), 0, 0, 999999)
        unique_name = _safe_component(context.get("unique_name"), "pixmax_video")
        generation_round = _as_int(context.get("generation_round"), 0, 0, 999999)
        generated: list[str] = []
        for index, source in enumerate(sources):
            position = _safe_component(_position_at(context.get("output_position"), index), str(index))
            filename = f"{viewer_index:04d}_{unique_name}_{generation_round}_{position}.mp4"
            output_path = str((output_dir / filename).resolve())
            save_asset(
                source,
                output_path,
                _as_int(params.get("download_timeout"), 1800, 10, 7200),
                api_key,
                api_base_url,
                expected_media_type="video",
            )
            generated.append(output_path)
        return generated
    except PixmaxApiError as exc:
        raise Exception(f"PLUGIN_ERROR:::{exc}") from exc
    except Exception as exc:
        if str(exc).startswith("PLUGIN_ERROR:::"):
            raise
        raise Exception(f"PLUGIN_ERROR:::Pixmax 视频生成失败：{exc}") from exc


def handle_action(action: str, data: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    if action not in {"connect_and_get_models", "get_models", "ping"}:
        return {"ok": False, "error": f"未知动作：{action}"}
    if action == "ping":
        return {"ok": True, "plugin": get_info()}
    params = get_params()
    payload = data if isinstance(data, dict) else {}
    params.update({key: value for key, value in payload.items() if key in _DEFAULT_PARAMS})
    try:
        result = _connect(params)
        if action == "get_models":
            result["message"] = f"已获取 {len(result['models'])} 个 Pixmax 视频模型。"
        return result
    except PixmaxApiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"API操作失败：{exc}"}
