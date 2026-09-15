from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

_API_CLIENT_MODULE_NAME = "_typetale_pixmax_openapi_image_api_client"
_API_CLIENT_PATH = Path(__file__).resolve().with_name("api_client.py")
_api_client_spec = importlib.util.spec_from_file_location(_API_CLIENT_MODULE_NAME, _API_CLIENT_PATH)
if not _api_client_spec or not _api_client_spec.loader:
    raise ImportError(f"无法加载 Pixmax 图片 API 客户端：{_API_CLIENT_PATH}")
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
find_asset_uuids = _api_client_module.find_asset_uuids
save_asset = _api_client_module.save_asset
normalize_base_url = _api_client_module.normalize_base_url


_PLUGIN_FILE = __file__
_DEFAULT_PARAMS = {
    "api_key": "",
    "api_base_url": DEFAULT_BASE_URL,
    "model": "",
    "resolution": "2K",
    "aspect_ratio": "1:1",
    "quality": "",
    "count": 1,
    "timeout": 1800,
    "poll_interval": 5.0,
    "download_timeout": 600,
    "_project_uuid": "",
}


def get_info() -> dict[str, Any]:
    return {
        "name": "Pixmax API 图片",
        "description": "只需 Pixmax API Key，自动连接项目并读取图片模型。",
        "version": "1.1.0",
        "type": "image",
    }


def get_params() -> dict[str, Any]:
    params = _DEFAULT_PARAMS.copy()
    stored = load_plugin_config(_PLUGIN_FILE)
    if isinstance(stored, dict):
        params.update(stored)
    if not str(params.get("api_key") or "").strip() and os.getenv("PIXMAX_OPENAPI_KEY"):
        params["api_key"] = os.getenv("PIXMAX_OPENAPI_KEY", "").strip()
    try:
        params["api_base_url"] = normalize_base_url(params.get("api_base_url") or DEFAULT_BASE_URL)
    except PixmaxApiError:
        params["api_base_url"] = DEFAULT_BASE_URL
    params["connected"] = bool(str(params.get("_project_uuid") or "").strip())
    return params


def _api_key(params: dict[str, Any]) -> str:
    return os.getenv("PIXMAX_OPENAPI_KEY", "").strip() or str(params.get("api_key") or "").strip()


def _api_base_url(params: dict[str, Any]) -> str:
    return normalize_base_url(params.get("api_base_url") or DEFAULT_BASE_URL)


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


def _safe_component(value: Any, fallback: str) -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "").strip()).strip(" .")
    return text or fallback


def _position_at(positions: Any, index: int) -> Any:
    if isinstance(positions, (list, tuple)) and positions:
        return positions[index] if index < len(positions) else index
    return index


def _schema_value(schema: Any, current: Any, fallback: str) -> str:
    if not isinstance(schema, dict):
        return str(current or fallback)
    value = str(current or "").strip()
    options = schema.get("options") or schema.get("values") or schema.get("enum") or schema.get("inValues")
    if isinstance(options, list):
        normalized = []
        for option in options:
            if isinstance(option, dict):
                item = option.get("value") if option.get("value") is not None else option.get("name")
            else:
                item = option
            normalized.append("" if item is None else str(item))
        if value in normalized:
            return value
        default = schema.get("default") if schema.get("default") is not None else schema.get("defaultValue")
        if default is None:
            default = schema.get("value")
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


def _build_task_params(model_row: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:

    schemas = model_row.get("params") if isinstance(model_row.get("params"), dict) else {}
    user_values = {
        "resolution": str(params.get("resolution") or "2K"),
        "aspectRatio": str(params.get("aspect_ratio") or "1:1"),
        "quality": str(params.get("quality") or ""),
        "count": str(_as_int(params.get("count"), 1, 1, 4)),
    }
    if not schemas:
        # 没有 schema 时不猜测可选字段，尤其是放大模型不能接收生图参数。
        return {}

    task_params: dict[str, Any] = {}
    for schema_name, schema in schemas.items():
        if re.sub(r"[^a-z0-9]", "", str(schema_name).lower()) in {"prompt", "model", "nodetype"}:
            continue
        canonical = next(
            (name for name in user_values if re.sub(r"[^a-z0-9]", "", name.lower()) == re.sub(r"[^a-z0-9]", "", str(schema_name).lower())),
            None,
        )
        current = user_values.get(canonical, "") if canonical else ""
        fallback = "standard" if canonical == "quality" else ""
        value = _schema_value(schema, current, fallback)
        if value != "":
            task_params[str(schema_name)] = value
    return task_params


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


def _find_base64(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("base64", "data", "content"):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate) > 100:
                try:
                    base64.b64decode(candidate.split(",", 1)[-1], validate=True)
                    return candidate.split(",", 1)[-1]
                except Exception:
                    pass
        for child in value.values():
            found = _find_base64(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_base64(child)
            if found:
                return found
    return ""


_ASSET_METADATA_SUFFIX = ".pixmax.json"


def _asset_metadata_path(output_path: str | Path) -> Path:
    return Path(f"{Path(output_path)}{_ASSET_METADATA_SUFFIX}")


def _save_asset_metadata(output_path: str | Path, asset_uuid: str, project_uuid: str, api_base_url: str) -> None:

    metadata_path = _asset_metadata_path(output_path)
    if not asset_uuid:
        try:
            metadata_path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    payload = {
        "asset_uuid": str(asset_uuid).strip(),
        "project_uuid": str(project_uuid or "").strip(),
        "api_base_url": str(api_base_url or "").strip(),
        "source": "pixmax_openapi",
    }
    try:
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 元数据仅用于优化复用；写入失败时视频插件仍可回退到重新上传。
        pass


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
    project_uuid, models = client.connect("GENERATE_IMAGE", str(params.get("_project_uuid") or ""))
    _save_internal(params, api_key, project_uuid, api_base_url)
    return {
        "ok": True,
        "connected": True,
        "message": f"Pixmax 已连接，已获取 {len(models)} 个图片模型。",
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
        project_uuid, models = client.connect("GENERATE_IMAGE", str(params.get("_project_uuid") or ""))
        _save_internal(params, api_key, project_uuid, api_base_url)
        requested_model = str(params.get("model") or "").strip()
        model_row = next((row for row in models if row["code"] == requested_model), models[0])
        model = model_row["code"]
        input_assets: list[str] = []
        for path in _reference_paths(context.get("reference_images")):
            if path.startswith(("http://", "https://")):
                raise PixmaxApiError("参考图必须是本地文件。")
            input_assets.append(client.upload_asset(project_uuid, path))
        client.check_asset_compliance(input_assets)
        task_params = _build_task_params(model_row, params)
        response, sources = client.generate(
            project_uuid,
            "GENERATE_IMAGE",
            model,
            prompt,
            task_params,
            input_asset_uuids=input_assets,
            timeout=_as_int(params.get("timeout"), 1800, 10, 7200),
            interval=_as_float(params.get("poll_interval"), 5.0, 0.2, 60.0),
            progress_callback=context.get("progress_callback"),
        )
        if not sources:
            sources = find_asset_urls(response)
        if not sources:
            encoded = _find_base64(response)
            if encoded:
                sources = [f"data:image/png;base64,{encoded}"]
        if not sources:
            raise PixmaxApiError("Pixmax 已完成，但响应中没有可下载的图片地址。")
        asset_uuids = find_asset_uuids(response)
        viewer_index = _as_int(context.get("viewer_index"), 0, 0, 999999)
        unique_name = _safe_component(context.get("unique_name"), "pixmax_image")
        generation_round = _as_int(context.get("generation_round"), 0, 0, 999999)
        generated: list[str] = []
        for index, source in enumerate(sources):
            position = _safe_component(_position_at(context.get("output_position"), index), str(index))
            filename = f"{viewer_index:04d}_{unique_name}_{generation_round}_{position}.png"
            output_path = str((output_dir / filename).resolve())
            save_asset(source, output_path, _as_int(params.get("download_timeout"), 600, 10, 7200), api_key, api_base_url)
            asset_uuid = asset_uuids[index] if index < len(asset_uuids) else ""
            _save_asset_metadata(output_path, asset_uuid, project_uuid, api_base_url)
            generated.append(output_path)
        return generated
    except PixmaxApiError as exc:
        raise Exception(f"PLUGIN_ERROR:::{exc}") from exc
    except Exception as exc:
        if str(exc).startswith("PLUGIN_ERROR:::"):
            raise
        raise Exception(f"PLUGIN_ERROR:::Pixmax 图片生成失败：{exc}") from exc


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
            result["message"] = f"已获取 {len(result['models'])} 个 Pixmax 图片模型。"
        return result
    except PixmaxApiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Pixmax  API 操作失败：{exc}"}
