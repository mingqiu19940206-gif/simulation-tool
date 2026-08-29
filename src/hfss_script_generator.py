"""生成可脱离GUI独立运行的 PyAEDT/HFSS 3D Modeler 脚本。

生成器在本机完成参数校验和几何计算，然后把规范化配置及全部几何坐标嵌入
独立 Python 文件。生成的脚本不依赖 ``config_manager.py``、
``geometry_calculator.py`` 或 Tkinter，可以直接复制到安装了 AEDT 和 PyAEDT
的 Windows 计算机上运行。

V2 可选创建GND VIA、电容模型、端口、空气盒、开放边界、Setup、扫频、Optimetrics和报告。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from build_info import get_build_info
from config_manager import (
    ConfigManager,
    ConfigError,
    config_hash,
    format_validation_report,
    geometry_hash,
    make_generation_snapshot,
    materialize_snapshot_config,
    resolve_output_directory,
    snapshot_from_config,
    snapshot_metadata,
    validate_generation_snapshot,
    validate_config,
)
from geometry_calculator import GeometryCalculator, GeometryResult
from model_validator import ModelValidator, ValidationItem, ValidationReport, export_report


class ScriptGenerationError(RuntimeError):
    """配置或文件状态导致脚本无法生成时抛出的异常。"""


@dataclass(frozen=True)
class GenerationResult:
    """记录一次脚本生成操作产生的文件。"""

    project_directory: Path
    config_path: Path
    script_path: Path
    geometry_summary_path: Path
    build_log_path: Path
    aedt_project_path: Path
    local_validate_json_path: Path
    local_validate_txt_path: Path
    hfss_validate_json_path: Path
    corrections: Tuple[str, ...]
    warnings: Tuple[str, ...]


def _encode_embedded_json(data: Mapping[str, Any]) -> str:
    """把JSON数据编码成不会受引号和Windows反斜杠影响的Base64文本。"""

    serialized = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return base64.b64encode(serialized).decode("ascii")


def _atomic_write_text(path: Path, text: str) -> None:
    """以UTF-8和LF换行原子写入文本文件。"""

    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        temporary.replace(path)
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise ScriptGenerationError(f"无法写入文件 {path}：{exc}") from exc


def _safe_project_name(value: Any) -> str:
    """再次确认工程名适合同时用作AEDT工程名和Windows文件名。"""

    name = str(value).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ScriptGenerationError(
            "工程名必须以英文字母开头，并且只能包含英文字母、数字和下划线。"
        )
    return name


class HfssScriptGenerator:
    """校验配置、计算几何并生成独立HFSS建模脚本。"""

    def __init__(
        self,
        config: Mapping[str, Any],
        base_directory: Optional[Path] = None,
    ) -> None:
        """创建生成器。

        Args:
            config: GUI或JSON提供的配置。
            base_directory: 解析相对输出目录的基准，默认是当前模块目录。
        """

        self.raw_config = dict(config)
        self.base_directory = (
            Path(base_directory).expanduser().resolve()
            if base_directory
            else Path(__file__).resolve().parent
        )
        self.snapshot: Optional[Dict[str, Any]] = None

    def validate_and_calculate(
        self,
    ) -> Tuple[Dict[str, Any], GeometryResult, List[str], List[str]]:
        """执行配置检查和纯Python几何计算。

        Returns:
            ``(规范化配置, 几何结果, 自动修正, 警告)``。

        Raises:
            ScriptGenerationError: 存在严重参数或几何错误。
        """

        try:
            normalized, validation = validate_config(
                self.raw_config,
                base_directory=self.base_directory,
                check_output_directory=True,
                normalize=True,
            )
        except ConfigError as exc:
            raise ScriptGenerationError(str(exc)) from exc

        if not validation.is_valid:
            raise ScriptGenerationError(format_validation_report(validation))

        calculator = GeometryCalculator(normalized)
        geometry = calculator.calculate()
        if geometry.errors:
            messages = "\n".join(f"  [错误] {item}" for item in geometry.errors)
            raise ScriptGenerationError("几何检查失败：\n" + messages)

        corrections = list(validation.corrections)
        warnings = list(validation.warnings)
        for warning in geometry.warnings:
            if warning not in warnings and warning not in corrections:
                warnings.append(warning)
        structured, structured_config, structured_geometry = ModelValidator(
            ConfigManager(self.base_directory)
        ).run_local(normalized)
        if structured.counts["errors"]:
            detail = "\n".join(
                f"  [{item.rule_id}] {item.message}" for item in structured.items if item.level == "error"
            )
            raise ScriptGenerationError("快速Validate失败：\n" + detail)
        self.local_validate_report = structured
        normalized = structured_config or normalized
        geometry = structured_geometry or geometry
        return normalized, geometry, corrections, warnings

    def generate(self, output_directory: Optional[Path] = None,
                 snapshot: Optional[Mapping[str, Any]] = None) -> GenerationResult:
        """生成配置副本、几何摘要、独立脚本和初始日志文件。

        ``项目名.aedt`` 由生成脚本在Windows/AEDT中运行后创建。
        """

        # GUI callers pass the immutable preview snapshot.  In that path this
        # method must not collect controls or recalculate geometry again.
        if snapshot is None:
            snapshot = snapshot_from_config(self.raw_config)
        if snapshot is None:
            config, geometry_object, corrections, warnings = self.validate_and_calculate()
            snapshot = make_generation_snapshot(config, geometry_object)
            geometry = snapshot["geometry_snapshot"]
        else:
            try:
                validate_generation_snapshot(snapshot)
            except ConfigError as exc:
                raise ScriptGenerationError(str(exc)) from exc
            config = dict(snapshot["config_snapshot"])
            geometry = snapshot["geometry_snapshot"]
            corrections, warnings = [], []
            if config_hash(config) != str(snapshot["config_hash"]):
                raise ScriptGenerationError("预览与生成的 config_hash 不一致，已停止生成。")
            if geometry_hash(geometry) != str(snapshot["geometry_hash"]):
                raise ScriptGenerationError("预览与生成的 geometry_hash 不一致，已停止生成。")
            # A hash proves immutability, not manufacturability.  Re-run the
            # same authoritative semantic preflight even for a GUI snapshot;
            # otherwise an invalid red preview could still be handed to HFSS.
            snapshot_report, _snapshot_config, snapshot_geometry = ModelValidator(
                ConfigManager(self.base_directory)
            ).run_local(config)
            if snapshot_report.counts["errors"]:
                detail = "\n".join(
                    f"  [{item.rule_id}] {item.message}"
                    for item in snapshot_report.items if item.level == "error"
                )
                raise ScriptGenerationError("预览快照未通过 HFSS fail-closed 预检：\n" + detail)
            if snapshot_geometry is not None and geometry_hash(snapshot_geometry) != str(snapshot["geometry_hash"]):
                raise ScriptGenerationError("预览、校验与 HFSS 的权威轮廓点不一致，已停止生成。")
            self.local_validate_report = snapshot_report
        self.snapshot = dict(snapshot)
        project_name = _safe_project_name(config["project"]["project_name"])

        if output_directory is None:
            output_root = resolve_output_directory(config, self.base_directory)
        else:
            output_root = Path(output_directory).expanduser()
            if not output_root.is_absolute():
                output_root = self.base_directory / output_root
            output_root = output_root.resolve()

        project_directory = output_root / project_name
        config_path = project_directory / f"{project_name}.json"
        script_path = project_directory / f"{project_name}_build_hfss.py"
        summary_path = project_directory / f"{project_name}_geometry_summary.txt"
        build_log_path = project_directory / f"{project_name}_build.log"
        local_validate_json = project_directory / f"{project_name}_validate_local.json"
        local_validate_txt = project_directory / f"{project_name}_validate_local.txt"
        hfss_validate_json = project_directory / f"{project_name}_validate_hfss.json"
        hfss_validate_txt = project_directory / f"{project_name}_validate_hfss.txt"
        aedt_location_json = project_directory / f"{project_name}_aedt_location.json"
        aedt_path = project_directory / f"{project_name}.aedt"
        failed_aedt_path = project_directory / f"{project_name}_FAILED.aedt"
        overwrite = bool(config["project"]["overwrite_existing"])

        existing_targets = [
            path
            for path in (
                config_path,
                script_path,
                summary_path,
                build_log_path,
                local_validate_json,
                local_validate_txt,
                hfss_validate_json,
                hfss_validate_txt,
                aedt_location_json,
                aedt_path,
                failed_aedt_path,
            )
            if path.exists()
        ]
        if existing_targets and not overwrite:
            formatted = "\n".join(f"  {path}" for path in existing_targets)
            raise ScriptGenerationError(
                "目标文件已存在，且 overwrite_existing=false：\n" + formatted
            )

        try:
            project_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ScriptGenerationError(
                f"无法创建工程输出目录 {project_directory}：{exc}"
            ) from exc

        if overwrite:
            for stale_path in (hfss_validate_json, hfss_validate_txt, aedt_location_json):
                try:
                    if stale_path.exists():
                        stale_path.unlink()
                except OSError as exc:
                    raise ScriptGenerationError(f"无法清理旧运行报告 {stale_path}：{exc}") from exc

        # Save the exact contract consumed by the generated script.
        try:
            persisted_snapshot = materialize_snapshot_config(snapshot)
            _atomic_write_text(
                config_path,
                json.dumps(persisted_snapshot, ensure_ascii=False, indent=2) + "\n",
            )
        except ConfigError as exc:
            raise ScriptGenerationError(str(exc)) from exc

        if isinstance(geometry, Mapping):
            geometry_summary = self._geometry_snapshot_summary(config, geometry, snapshot)
        else:  # legacy fallback; the normal snapshot path never enters here.
            geometry_summary = GeometryCalculator(config).geometry_summary(geometry)
        if corrections:
            geometry_summary += "\n\n配置自动修正：\n"
            geometry_summary += "\n".join(f"  [修正] {item}" for item in corrections)
        if warnings:
            geometry_summary += "\n\n配置警告：\n"
            geometry_summary += "\n".join(f"  [警告] {item}" for item in warnings)
        geometry_summary += "\n"

        script_text = self.build_script_text(config, geometry, snapshot_metadata(snapshot))
        initial_log = (
            "脚本生成完成，尚未运行PyAEDT建模脚本。\n"
            f"脚本：{script_path.name}\n"
            f"目标AEDT工程：{aedt_path.name}\n"
            f"generation_id：{snapshot['generation_id']}\n"
            f"config_hash：{snapshot['config_hash']}\n"
            f"geometry_hash：{snapshot['geometry_hash']}\n"
            f"software_version：{get_build_info()['software_version']}\n"
            f"build_time_utc：{get_build_info()['build_time_utc']}\n"
            f"source_hash：{get_build_info()['source_hash']}\n"
            f"sweep_backend_version：{get_build_info()['sweep_backend_version']}\n"
            f"config_schema_version：{get_build_info()['config_schema_version']}\n"
        )
        _atomic_write_text(summary_path, geometry_summary)
        _atomic_write_text(script_path, script_text)
        _atomic_write_text(build_log_path, initial_log)
        export_report(self.local_validate_report, local_validate_json)
        export_report(self.local_validate_report, local_validate_txt)

        return GenerationResult(
            project_directory=project_directory,
            config_path=config_path,
            script_path=script_path,
            geometry_summary_path=summary_path,
            build_log_path=build_log_path,
            aedt_project_path=aedt_path,
            local_validate_json_path=local_validate_json,
            local_validate_txt_path=local_validate_txt,
            hfss_validate_json_path=hfss_validate_json,
            corrections=tuple(corrections),
            warnings=tuple(warnings),
        )

    @staticmethod
    def build_script_text(
        config: Mapping[str, Any], geometry: Any,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """返回独立PyAEDT脚本的完整文本。"""

        config_payload = _encode_embedded_json(config)
        geometry_payload = _encode_embedded_json(
            geometry.to_dict() if hasattr(geometry, "to_dict") else geometry
        )
        snapshot_payload = _encode_embedded_json(dict(metadata or {
            "generation_id": "legacy",
            "config_hash": config_hash(config),
            "geometry_hash": geometry_hash(geometry),
        }))
        build_payload = _encode_embedded_json(get_build_info())
        return (
            INDEPENDENT_SCRIPT_TEMPLATE.replace(
                "__EMBEDDED_CONFIG_BASE64__", config_payload
            )
            .replace("__EMBEDDED_GEOMETRY_BASE64__", geometry_payload)
            .replace("__GENERATION_METADATA_BASE64__", snapshot_payload)
            .replace("__BUILD_METADATA_BASE64__", build_payload)
            .lstrip()
        )

    @staticmethod
    def _geometry_snapshot_summary(config: Mapping[str, Any], geometry: Mapping[str, Any],
                                   snapshot: Mapping[str, Any]) -> str:
        """Summarize the manifest without invoking GeometryCalculator again."""

        lines = [
            "PCIe AC耦合电容区域 V2 几何快照",
            "=" * 48,
            f"generation_id: {snapshot['generation_id']}",
            f"config_hash: {snapshot['config_hash']}",
            f"geometry_hash: {snapshot['geometry_hash']}",
            f"信号对象: P={len(geometry.get('signal_shapes', {}).get('P', []))}, "
            f"N={len(geometry.get('signal_shapes', {}).get('N', []))}",
            f"GND VIA: {len(geometry.get('gnd_vias', []))}",
            f"Port: {len(geometry.get('ports', []))}",
            f"电容本体: {len(geometry.get('capacitor_bodies', []))}",
            f"AIRBOX: {'有' if geometry.get('airbox') else '无'}",
            f"geometry_units: {geometry.get('units', 'mm')}",
            f"HFSS/output_units: {config.get('units', 'mm')}",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _snapshot_validation_report(snapshot: Mapping[str, Any]) -> ValidationReport:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        item = ValidationItem(
            "SNAPSHOT-001", "info", "参数与文件",
            "生成直接复用了预览快照；本次生成未重新计算几何。",
            timestamp=now, config_revision=str(snapshot["config_hash"])[:12],
        )
        return ValidationReport(
            "预览快照生成", str(snapshot["config_hash"])[:12], now, [item],
            raw_log="generation_id={} config_hash={} geometry_hash={}".format(
                snapshot["generation_id"], snapshot["config_hash"], snapshot["geometry_hash"]
            ),
        )


def generate_hfss_script(
    config: Mapping[str, Any],
    output_directory: Optional[Path] = None,
    base_directory: Optional[Path] = None,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> GenerationResult:
    """模块级便捷函数：生成完整的独立HFSS脚本及配套文件。"""

    return HfssScriptGenerator(
        config=config, base_directory=base_directory
    ).generate(output_directory=output_directory, snapshot=snapshot)


INDEPENDENT_SCRIPT_TEMPLATE = r'''
"""由表层差分线串联电容反焊盘参数化建模工具自动生成。

本脚本使用嵌入的统一几何创建HFSS工程，并在建模后检查实际对象、材料、
端口、边界、Setup、Sweep及AEDT原生Validate消息。检查失败时保留工程和日志。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


CONFIG = json.loads(
    base64.b64decode("__EMBEDDED_CONFIG_BASE64__").decode("utf-8")
)
GEOMETRY = json.loads(
    base64.b64decode("__EMBEDDED_GEOMETRY_BASE64__").decode("utf-8")
)
GENERATION_METADATA = json.loads(
    base64.b64decode("__GENERATION_METADATA_BASE64__").decode("utf-8")
)
BUILD_METADATA = json.loads(
    base64.b64decode("__BUILD_METADATA_BASE64__").decode("utf-8")
)

AEDT_VERSION = str(CONFIG["project"].get("aedt_version", "")).strip()
SUPPORTED_AEDT_VERSIONS = {"2022.2", "2023.2"}
NON_GRAPHICAL = bool(CONFIG["project"].get("non_graphical", False))
NEW_DESKTOP_SESSION = bool(
    CONFIG["project"].get("new_desktop_session", True)
)
OPEN_AEDT_AFTER_BUILD = bool(
    CONFIG["project"].get("open_aedt_after_build", True)
)
OVERWRITE_EXISTING = bool(
    CONFIG["project"].get("overwrite_existing", False)
)
PROJECT_NAME = str(CONFIG["project"]["project_name"])
DESIGN_NAME = str(CONFIG["project"].get("design_name", "Surface_Cap_Antipad"))
SOLUTION_TYPE = str(CONFIG["project"].get("solution_type", "Modal"))
UNITS = str(CONFIG.get("units", "mm"))

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REQUESTED_AEDT_PROJECT_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + ".aedt")
REQUESTED_FAILED_PROJECT_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + "_FAILED.aedt")
BUILD_LOG_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + "_build.log")
HFSS_VALIDATE_JSON_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + "_validate_hfss.json")
HFSS_VALIDATE_TXT_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + "_validate_hfss.txt")
AEDT_LOCATION_JSON_PATH = SCRIPT_DIRECTORY / (PROJECT_NAME + "_aedt_location.json")
GENERATION_ID = str(GENERATION_METADATA.get("generation_id", ""))
CONFIG_HASH = str(GENERATION_METADATA.get("config_hash", ""))
GEOMETRY_HASH = str(GENERATION_METADATA.get("geometry_hash", ""))
CONFIG_REVISION = CONFIG_HASH[:12]


def _is_ascii_path(path):
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _runtime_aedt_project_path():
    """Return an ASCII-only AEDT path for legacy 2022 R2 gRPC Rename."""

    if _is_ascii_path(REQUESTED_AEDT_PROJECT_PATH):
        return REQUESTED_AEDT_PROJECT_PATH
    roots = []
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "SurfaceCapAntipadTool" / "HFSSProjects")
    if os.environ.get("PUBLIC"):
        roots.append(Path(os.environ["PUBLIC"]) / "Documents" / "SurfaceCapAntipadTool" / "HFSSProjects")
    system_drive = os.environ.get("SystemDrive", "C:")
    roots.append(Path(system_drive + "\\") / "HFSS_Projects")
    for root in roots:
        candidate = root / PROJECT_NAME / GENERATION_ID / (PROJECT_NAME + ".aedt")
        if not _is_ascii_path(candidate):
            continue
        return candidate
    raise RuntimeError(
        "AEDT 2022 R2不能在当前非ASCII输出路径创建工程，且无法建立英文运行目录：{}".format(
            REQUESTED_AEDT_PROJECT_PATH
        )
    )


AEDT_PROJECT_PATH = _runtime_aedt_project_path()
FAILED_AEDT_PROJECT_PATH = AEDT_PROJECT_PATH.with_name(PROJECT_NAME + "_FAILED.aedt")


def _hash_payload(value):
    """Hash the same canonical payload as config_manager without importing it."""

    payload = dict(value)
    payload.pop("_generation", None)
    payload.pop("_geometry_snapshot", None)
    payload.pop("_internal_units", None)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _verify_snapshot_integrity():
    actual_config = _hash_payload(CONFIG)
    actual_geometry = hashlib.sha256(
        json.dumps(GEOMETRY, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    embedded = CONFIG.get("_generation", {})
    expected_config = str(embedded.get("config_hash", CONFIG_HASH))
    expected_geometry = str(embedded.get("geometry_hash", GEOMETRY_HASH))
    if not GENERATION_ID or actual_config != CONFIG_HASH or actual_geometry != GEOMETRY_HASH:
        raise RuntimeError(
            "生成快照完整性校验失败："
            f"generation_id={GENERATION_ID}, "
            f"config expected={CONFIG_HASH} actual={actual_config}, "
            f"geometry expected={GEOMETRY_HASH} actual={actual_geometry}"
        )
    if expected_config != CONFIG_HASH or expected_geometry != GEOMETRY_HASH:
        raise RuntimeError("配置JSON中的快照哈希与脚本内嵌哈希不一致。")


def configure_logging():
    """创建同时写入控制台和UTF-8文件的日志器。"""

    logger = logging.getLogger("surface_cap_antipad_builder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(
        str(BUILD_LOG_PATH), mode="w", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


LOGGER = configure_logging()
NATIVE_SWEEP_RUNTIME = {}
try:
    _verify_snapshot_integrity()
    LOGGER.info("generation_id=%s | config_hash=%s | geometry_hash=%s", GENERATION_ID, CONFIG_HASH, GEOMETRY_HASH)
    LOGGER.info(
        "software_version=%s | build_time_utc=%s | source_hash=%s | sweep_backend_version=%s | config_schema_version=%s",
        BUILD_METADATA.get("software_version"), BUILD_METADATA.get("build_time_utc"),
        BUILD_METADATA.get("source_hash"), BUILD_METADATA.get("sweep_backend_version"),
        BUILD_METADATA.get("config_schema_version"),
    )
    if AEDT_PROJECT_PATH != REQUESTED_AEDT_PROJECT_PATH:
        LOGGER.warning(
            "检测到AEDT 2022 R2非ASCII路径兼容风险；工程将在英文运行路径创建：%s",
            AEDT_PROJECT_PATH,
        )
except Exception:
    LOGGER.error("快照哈希校验失败：%s", traceback.format_exc())
    raise


def length(value):
    """把内部mm数值无损换算为当前HFSS模型单位表达式。"""

    if isinstance(value, str):
        return value
    model_value = model_length_value(value)
    return "{:.12g}{}".format(model_value, UNITS)


def model_length_value(value):
    """把内部mm数值转换为PyAEDT点坐标所需的当前模型单位数值。"""

    return float(value) if UNITS == "mm" else float(value) / 0.0254


def angle(value):
    """把数值转换为显式带deg单位的AEDT表达式。"""

    if isinstance(value, str):
        return value
    return "{:.12g}deg".format(float(value))


def scalar(value):
    """把无量纲数值转换为稳定字符串。"""

    if isinstance(value, str):
        return value
    return "{:.12g}".format(float(value))


def safe_identifier(value):
    """把层名转换为安全的小写Design Variable标识符。"""

    text = "".join(
        character.lower() if character.isalnum() else "_"
        for character in str(value)
    )
    text = text.strip("_")
    if not text or text[0].isdigit():
        text = "layer_" + text
    return text


def antipad_expansion_variable(layer_name):
    """Preserve layer-name case in the user-visible antipad derivation chain."""

    suffix = "".join(
        character if character.isalnum() else "_"
        for character in str(layer_name)
    ).strip("_")
    if not suffix or suffix[0].isdigit():
        suffix = "layer_" + suffix
    return "antipad_expansion_" + suffix


def import_hfss_class():
    """兼容现代ansys.aedt.core和旧版pyaedt导入路径。"""

    try:
        from ansys.aedt.core import Hfss

        LOGGER.info("使用现代导入路径：ansys.aedt.core.Hfss")
        return Hfss
    except ImportError as modern_error:
        try:
            from pyaedt import Hfss

            LOGGER.warning(
                "使用旧版兼容导入路径 pyaedt.Hfss；"
                "建议后续升级并改用 ansys.aedt.core。"
            )
            return Hfss
        except ImportError as legacy_error:
            raise RuntimeError(
                "无法导入PyAEDT。请在Windows环境安装 pyaedt，并确认AEDT已安装。"
            ) from legacy_error


def object_name(value):
    """从Object3d、字符串或布尔返回对象名称。"""

    if isinstance(value, str):
        return value
    return getattr(value, "name", None)


def model_object_names(app):
    """兼容读取3D Modeler中的全部对象名称。"""

    modeler = app.modeler
    for attribute in ("object_names", "model_objects"):
        try:
            names = getattr(modeler, attribute)
            if callable(names):
                names = names()
            if names is not None:
                return list(names)
        except Exception:
            pass
    names = []
    for attribute in ("solid_names", "sheet_names", "line_names"):
        try:
            values = getattr(modeler, attribute)
            if callable(values):
                values = values()
            names.extend(list(values or []))
        except Exception:
            pass
    return sorted(set(names))


def object_exists(app, name):
    """兼容检查HFSS对象是否存在。"""

    try:
        method = getattr(app.modeler, "does_object_exists")
        return bool(method(name))
    except Exception:
        return name in model_object_names(app)


AUDIT_LOG = []


def audit_event(app, stage, requested, returned=None, material="", before_count=None,
                inputs=None, result=None, exception=None):
    """Record a structured create/boolean operation in build.log/report."""

    try:
        after_names = model_object_names(app)
        after_count = len(after_names)
    except Exception:
        after_names, after_count = [], None
    actual = object_name(returned) if returned is not None else returned
    bbox = None
    target = actual or requested
    try:
        if target and object_exists(app, target):
            bbox = [float(value) for value in app.modeler[target].bounding_box]
    except Exception:
        bbox = None
    item = {
        "stage": str(stage), "requested_name": str(requested),
        "actual_returned_name": str(actual or ""),
        "object_count_before": before_count, "object_count_after": after_count,
        "return_value": repr(result), "material": str(material or ""),
        "bounding_box": bbox, "inputs": list(inputs or []),
        "exception": str(exception or ""),
    }
    AUDIT_LOG.append(item)
    LOGGER.info("AUDIT %s", json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    return item


def require_object(app, name, step):
    """对象不存在时立即抛出带步骤名称的异常。"""

    if not object_exists(app, name):
        raise RuntimeError("{}失败：对象 {} 不存在。".format(step, name))


def set_object_name_compat(app, assignment, new_name):
    """兼容修改Object3d名称。"""

    current_name = object_name(assignment)
    if current_name == new_name:
        return new_name
    target = assignment
    if isinstance(assignment, str):
        try:
            target = app.modeler[assignment]
        except Exception:
            try:
                target = app.modeler.objects_by_name[assignment]
            except Exception:
                target = None
    if target is not None:
        try:
            target.name = new_name
            if object_exists(app, new_name):
                return new_name
        except Exception:
            pass
    try:
        app.modeler.oeditor.ChangeProperty(
            [
                "NAME:AllTabs",
                [
                    "NAME:Geometry3DAttributeTab",
                    ["NAME:PropServers", current_name],
                    [
                        "NAME:ChangedProps",
                        ["NAME:Name", "Value:=", new_name],
                    ],
                ],
            ]
        )
    except Exception as exc:
        raise RuntimeError(
            "无法把对象 {} 重命名为 {}：{}".format(
                current_name, new_name, exc
            )
        ) from exc
    require_object(app, new_name, "对象重命名")
    return new_name


def set_variable_compat(app, name, value):
    """兼容创建或更新HFSS Design Variable。"""

    try:
        app[name] = value
        LOGGER.info("Design Variable: %s = %s", name, value)
        return True
    except Exception as first_error:
        try:
            manager = app.variable_manager
            result = manager.set_variable(
                variable_name=name,
                expression=value,
                is_post_processing=False,
            )
            if result is False:
                raise RuntimeError("set_variable返回False")
            LOGGER.info("Design Variable: %s = %s", name, value)
            return True
        except Exception as second_error:
            raise RuntimeError(
                "无法设置Design Variable {}={}；{}；{}".format(
                    name, value, first_error, second_error
                )
            ) from second_error


def assign_material_compat(app, assignment, material):
    """兼容为一个或多个对象分配材料。"""

    names = assignment if isinstance(assignment, list) else [assignment]
    names = [object_name(item) or item for item in names]
    try:
        result = app.assign_material(assignment=names, material=material)
        if result is False:
            raise RuntimeError("assign_material返回False")
        return True
    except Exception:
        try:
            result = app.assign_material(names, material)
            if result is False:
                raise RuntimeError("assign_material返回False")
            return True
        except Exception:
            for name in names:
                try:
                    app.modeler[name].material_name = material
                except Exception as exc:
                    raise RuntimeError(
                        "无法为对象 {} 分配材料 {}：{}".format(
                            name, material, exc
                        )
                    ) from exc
            return True


def create_box_compat(app, origin, sizes, name, material):
    """兼容创建具有明确名称、材料和三维尺寸的Box。"""

    if object_exists(app, name):
        raise RuntimeError("创建前已存在同名对象：{}".format(name))
    before_count = len(model_object_names(app))
    modeler = app.modeler
    try:
        result = modeler.create_box(
            origin=origin,
            sizes=sizes,
            name=name,
            material=material,
        )
    except TypeError:
        try:
            result = modeler.create_box(
                position=origin,
                dimensions_list=sizes,
                name=name,
                matname=material,
            )
        except TypeError:
            result = modeler.create_box(origin, sizes, name, material)
    if result is False or result is None:
        raise RuntimeError("create_box返回失败：{}".format(name))
    actual_name = object_name(result) or name
    if actual_name != name:
        set_object_name_compat(app, actual_name, name)
    require_object(app, name, "创建Box")
    assign_material_compat(app, name, material)
    audit_event(app, "create_box", name, result, material, before_count, result=result)
    LOGGER.info("已创建Box：%s，材料=%s", name, material)
    return name



def create_cylinder_compat(app, center, radius, height, name, material):
    """兼容创建Z向圆柱。"""
    if object_exists(app, name):
        raise RuntimeError("创建前已存在同名对象：{}".format(name))
    before_count = len(model_object_names(app))
    try:
        result = app.modeler.create_cylinder(
            orientation="Z", origin=center, radius=radius, height=height,
            name=name, material=material,
        )
    except TypeError:
        try:
            result = app.modeler.create_cylinder(
                cs_axis="Z", position=center, radius=radius, height=height,
                name=name, matname=material,
            )
        except TypeError:
            result = app.modeler.create_cylinder("Z", center, radius, height, name=name, matname=material)
    if result is False or result is None:
        raise RuntimeError("create_cylinder返回失败：{}".format(name))
    actual = object_name(result) or name
    if actual != name:
        set_object_name_compat(app, actual, name)
    assign_material_compat(app, name, material)
    require_object(app, name, "创建Cylinder")
    audit_event(app, "create_cylinder", name, result, material, before_count, result=result)
    return name
def create_rectangle_compat(
    app, origin, sizes, name, material, orientation="XY"
):
    """兼容创建二维矩形面，供扩展接口使用。"""

    if object_exists(app, name):
        raise RuntimeError("创建前已存在同名对象：{}".format(name))
    before_count = len(model_object_names(app))
    modeler = app.modeler
    try:
        result = modeler.create_rectangle(
            orientation=orientation,
            origin=origin,
            sizes=sizes,
            name=name,
            material=material,
        )
    except TypeError:
        try:
            result = modeler.create_rectangle(
                cs_plane=orientation,
                position=origin,
                dimension_list=sizes,
                name=name,
                matname=material,
            )
        except TypeError:
            result = modeler.create_rectangle(
                orientation, origin, sizes, name=name, material=material
            )
    if result is False or result is None:
        raise RuntimeError("create_rectangle返回失败：{}".format(name))
    actual_name = object_name(result) or name
    if actual_name != name:
        set_object_name_compat(app, actual_name, name)
    require_object(app, name, "创建Rectangle")
    audit_event(app, "create_rectangle", name, result, material, before_count, result=result)
    return name


def create_polyline_compat(app, points, name, material):
    """兼容创建仅含直线段的闭合覆盖Polyline面。"""

    if object_exists(app, name):
        raise RuntimeError("创建前已存在同名对象：{}".format(name))
    before_count = len(model_object_names(app))
    modeler = app.modeler
    try:
        result = modeler.create_polyline(
            points=points,
            cover_surface=True,
            close_surface=True,
            name=name,
            material=material,
        )
    except TypeError:
        try:
            result = modeler.create_polyline(
                points,
                cover_surface=True,
                close_surface=True,
                name=name,
                matname=material,
            )
        except TypeError:
            result = modeler.create_polyline(
                points,
                cover_surface=True,
                close_surface=True,
                name=name,
            )
    if result is False or result is None:
        raise RuntimeError("create_polyline返回失败：{}".format(name))
    actual_name = object_name(result) or name
    if actual_name != name:
        set_object_name_compat(app, actual_name, name)
    require_object(app, name, "创建Polyline面")
    assign_material_compat(app, name, material)
    audit_event(app, "create_polyline", name, result, material, before_count, result=result)
    return name


def thicken_sheet_compat(app, assignment, thickness):
    """Extrude an XY profile by an explicit +Z vector.

    AEDT 2022 R2 ``ThickenSheet`` follows the profile normal.  Closed
    polylines generated from opposite trace directions can therefore thicken
    toward -Z and intersect the dielectric below.  ``SweepAlongVector`` makes
    the layer contract deterministic for every polygon winding order.
    """

    vector = [length(0.0), length(0.0), thickness]
    try:
        result = app.modeler.sweep_along_vector(
            assignment=assignment,
            sweep_vector=vector,
        )
    except TypeError:
        result = app.modeler.sweep_along_vector(assignment, vector)
    if result is False or result is None:
        raise RuntimeError(
            "SweepAlongVector返回失败：{}，厚度={}".format(
                assignment, thickness
            )
        )
    require_object(app, object_name(result) or assignment, "面拉伸")
    return object_name(result) or assignment


def create_polygon_solid_compat(
    app, points_xy, z_bottom, thickness, name, material
):
    """从二维多边形创建具有真实厚度的HFSS实体。"""

    points = [
        [length(point[0]), length(point[1]), length(z_bottom)]
        for point in points_xy
    ]
    # Keep the construction profile under a temporary name.  HFSS versions
    # differ on whether ThickenSheet consumes or retains its source sheet;
    # using the final object name for the profile is what left SIG_* fragments
    # in the model tree in older builds.
    profile_name = name + "_PROFILE"
    sheet_name = create_polyline_compat(
        app=app,
        points=points,
        name=profile_name,
        material=material,
    )
    solid_before_count = len(model_object_names(app))
    solid_name = thicken_sheet_compat(app, sheet_name, thickness)
    if solid_name != name:
        set_object_name_compat(app, solid_name, name)
    if object_exists(app, profile_name):
        try:
            app.modeler.delete([profile_name])
        except Exception as exc:
            raise RuntimeError("ThickenSheet后无法删除构造Profile {}：{}".format(profile_name, exc)) from exc
    if object_exists(app, profile_name):
        raise RuntimeError("ThickenSheet后仍存在构造Profile：{}".format(profile_name))
    assign_material_compat(app, name, material)
    require_object(app, name, "创建多边形实体")
    audit_event(
        app, "thicken_sheet", name, solid_name, material,
        solid_before_count, inputs=[profile_name], result=solid_name,
    )
    LOGGER.info(
        "已创建多边形实体：%s，点数=%d，厚度=%s，材料=%s",
        name,
        len(points_xy),
        thickness,
        material,
    )
    return name


def unite_compat(app, assignments, final_name):
    """兼容合并导体对象并保留确定的最终名称。"""

    names = [object_name(item) or item for item in assignments]
    if not names:
        raise RuntimeError("Unite对象列表为空：{}".format(final_name))
    for name in names:
        require_object(app, name, "Unite前检查")
    before_count = len(model_object_names(app))
    if len(names) == 1:
        result = set_object_name_compat(app, names[0], final_name)
        audit_event(app, "unite", final_name, result, before_count=before_count,
                    inputs=names, result=result)
        return result
    try:
        try:
            result = app.modeler.unite(
                assignment=names,
                purge=False,
                keep_originals=False,
            )
        except TypeError:
            try:
                result = app.modeler.unite(names, keep_originals=False)
            except TypeError:
                result = app.modeler.unite(names)
    except Exception as exc:
        audit_event(
            app, "unite", final_name, None, before_count=before_count,
            inputs=names, exception=traceback.format_exc(),
        )
        raise RuntimeError("Unite API异常：{}".format(exc)) from exc
    if result is False or result is None:
        # 一些旧版本成功时返回True或None，先检查第一个对象是否仍存在。
        if not object_exists(app, names[0]):
            audit_event(
                app, "unite", final_name, None, before_count=before_count,
                inputs=names, result=result,
                exception="Unite返回空值且首对象已不存在",
            )
            raise RuntimeError(
                "Unite失败或返回空值：{}".format(", ".join(names))
            )
        united_name = names[0]
    elif result is True:
        united_name = names[0]
    else:
        united_name = object_name(result) or names[0]
    if united_name != final_name:
        set_object_name_compat(app, united_name, final_name)
    require_object(app, final_name, "Unite结果检查")
    remaining_parts = [
        name
        for name in names
        if name != final_name and object_exists(app, name)
    ]
    if remaining_parts:
        audit_event(
            app, "unite", final_name, united_name, before_count=before_count,
            inputs=names, result=result,
            exception="Unite后仍存在未合并对象：{}".format(", ".join(remaining_parts)),
        )
        raise RuntimeError(
            "Unite后仍存在未合并对象：{}".format(", ".join(remaining_parts))
        )
    audit_event(app, "unite", final_name, united_name, before_count=before_count,
                inputs=names, result=result)
    LOGGER.info("Unite完成：%s -> %s", ", ".join(names), final_name)
    return final_name


def subtract_compat(app, blank, tools, keep_originals=False):
    """兼容从参考平面扣除反焊盘工具体。"""

    blank_name = object_name(blank) or blank
    tool_names = [object_name(item) or item for item in tools]
    require_object(app, blank_name, "Subtract前平面检查")
    for tool_name in tool_names:
        require_object(app, tool_name, "Subtract前工具体检查")
    before_count = len(model_object_names(app))
    try:
        try:
            result = app.modeler.subtract(
                blank_list=blank_name,
                tool_list=tool_names,
                keep_originals=keep_originals,
            )
        except TypeError:
            try:
                result = app.modeler.subtract(
                    blank_name, tool_names, keep_originals=keep_originals
                )
            except TypeError:
                result = app.modeler.subtract(blank_name, tool_names)
    except Exception as exc:
        audit_event(
            app, "subtract", blank_name, None, before_count=before_count,
            inputs=[blank_name, *tool_names], exception=traceback.format_exc(),
        )
        raise RuntimeError("Subtract API异常：{}".format(exc)) from exc
    if result is False:
        audit_event(
            app, "subtract", blank_name, blank_name, before_count=before_count,
            inputs=[blank_name, *tool_names], result=result,
            exception="Subtract返回False",
        )
        raise RuntimeError(
            "Subtract返回False：{} - {}".format(
                blank_name, ", ".join(tool_names)
            )
        )
    require_object(app, blank_name, "Subtract结果检查")
    if not keep_originals:
        remaining_tools = [
            name for name in tool_names if object_exists(app, name)
        ]
        if remaining_tools:
            try:
                app.modeler.delete(remaining_tools)
            except Exception as exc:
                raise RuntimeError(
                    "Subtract后无法删除工具体 {}：{}".format(
                        ", ".join(remaining_tools), exc
                    )
                ) from exc
    audit_event(app, "subtract", blank_name, blank_name, before_count=before_count,
                inputs=[blank_name, *tool_names], result=result)
    LOGGER.info(
        "Subtract完成：平面=%s，工具=%s",
        blank_name,
        ", ".join(tool_names),
    )
    return blank_name


def save_project_compat(app, path):
    """兼容保存AEDT工程，并检查文件是否实际存在。"""

    try:
        result = app.save_project(
            file_name=str(path),
            overwrite=True,
            refresh_ids=False,
        )
    except TypeError:
        try:
            result = app.save_project(str(path), overwrite=True)
        except TypeError:
            result = app.save_project(str(path))
    if result is False:
        raise RuntimeError("save_project返回False：{}".format(path))
    if not Path(path).exists():
        LOGGER.warning(
            "save_project已返回，但文件暂未能从Python侧确认存在：%s", path
        )
    else:
        LOGGER.info("AEDT工程已保存：%s", path)
    return True


def release_desktop_compat(app):
    """在用户明确关闭保持AEDT选项时释放Desktop。"""

    try:
        app.release_desktop(
            close_projects=True,
            close_desktop=NEW_DESKTOP_SESSION,
        )
    except TypeError:
        try:
            app.release_desktop(True, NEW_DESKTOP_SESSION)
        except Exception:
            app.close_desktop()


def create_hfss_project():
    """启动或连接AEDT，并创建指定HFSS工程。"""

    if AEDT_VERSION not in SUPPORTED_AEDT_VERSIONS:
        raise RuntimeError(
            "AEDT版本仅支持2022 R2（2022.2）或2023 R2（2023.2），当前为：{}".format(
                AEDT_VERSION or "未指定"
            )
        )
    try:
        AEDT_PROJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError("无法创建AEDT英文运行目录 {}：{}".format(
            AEDT_PROJECT_PATH.parent, exc)) from exc
    if AEDT_PROJECT_PATH.exists() and not OVERWRITE_EXISTING:
        raise FileExistsError(
            "AEDT工程已存在且禁止覆盖：{}".format(AEDT_PROJECT_PATH)
        )
    if AEDT_PROJECT_PATH.exists() and OVERWRITE_EXISTING:
        try:
            AEDT_PROJECT_PATH.unlink()
            LOGGER.warning("已按配置覆盖旧AEDT工程文件：%s", AEDT_PROJECT_PATH)
        except OSError as exc:
            raise RuntimeError(
                "无法覆盖旧AEDT工程，可能正在被AEDT占用：{}".format(exc)
            ) from exc

    Hfss = import_hfss_class()
    modern_kwargs = {
        "project": str(AEDT_PROJECT_PATH),
        "design": DESIGN_NAME,
        "solution_type": SOLUTION_TYPE,
        "version": AEDT_VERSION,
        "non_graphical": NON_GRAPHICAL,
        "new_desktop": NEW_DESKTOP_SESSION,
        "close_on_exit": False,
        "remove_lock": False,
    }
    try:
        app = Hfss(**modern_kwargs)
    except TypeError:
        legacy_kwargs = {
            "projectname": str(AEDT_PROJECT_PATH),
            "designname": DESIGN_NAME,
            "solution_type": SOLUTION_TYPE,
            "specified_version": AEDT_VERSION,
            "non_graphical": NON_GRAPHICAL,
            "new_desktop_session": NEW_DESKTOP_SESSION,
            "close_on_exit": False,
        }
        app = Hfss(**legacy_kwargs)
    ensure_solution_type_compat(app)
    if app is None or not getattr(app, "valid_design", True):
        raise RuntimeError("HFSS工程或设计创建失败。")
    LOGGER.info("PyAEDT版本：%s", _pyaedt_version())
    LOGGER.info("请求的AEDT目标版本：%s", AEDT_VERSION)
    LOGGER.info("AEDT版本：%s", getattr(app, "aedt_version_id", AEDT_VERSION))
    LOGGER.info("工程路径：%s", AEDT_PROJECT_PATH)
    return app


class _SolutionTypeDesignProxy:
    """Work around AEDT 2022 R2 gRPC GetSolutionType gaps in PyAEDT 1.3."""

    def __init__(self, wrapped, solution_type):
        self._wrapped = wrapped
        self._solution_type = solution_type

    def GetSolutionType(self):
        if self._solution_type == "Terminal":
            return "HFSS Terminal Network"
        if self._solution_type == "Modal":
            return "HFSS Modal Network"
        return self._solution_type

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def ensure_solution_type_compat(app):
    """Make the requested HFSS solution type observable to all PyAEDT APIs."""

    target = (
        "HFSS Terminal Network" if SOLUTION_TYPE == "Terminal"
        else "HFSS Modal Network" if SOLUTION_TYPE == "Modal"
        else SOLUTION_TYPE
    )
    try:
        try:
            app.odesign.SetSolutionType(target)
        except TypeError:
            app.odesign.SetSolutionType(target, "")
    except Exception as exc:
        LOGGER.warning("显式设置HFSS解类型%s失败，将核对构造器结果：%s", target, exc)
    try:
        observed = app.solution_type
    except Exception as exc:
        observed = None
        LOGGER.warning("PyAEDT读取HFSS解类型失败，将启用2022 R2兼容代理：%s", exc)
    if observed == SOLUTION_TYPE:
        LOGGER.info("HFSS解类型已确认：%s", observed)
        return
    design_solutions = getattr(app, "design_solutions", None)
    wrapped = getattr(design_solutions, "_odesign", None)
    if design_solutions is None or wrapped is None:
        raise RuntimeError("无法确认HFSS解类型：期望{}，实际{}。".format(SOLUTION_TYPE, observed))
    design_solutions._odesign = _SolutionTypeDesignProxy(wrapped, SOLUTION_TYPE)
    design_solutions._solution_type = SOLUTION_TYPE
    observed = app.solution_type
    if observed != SOLUTION_TYPE:
        raise RuntimeError("HFSS解类型兼容修复失败：期望{}，实际{}。".format(SOLUTION_TYPE, observed))
    LOGGER.warning("已启用AEDT 2022 R2解类型读取兼容代理：%s", observed)


def create_hfss_design(app):
    """确认当前设计是指定的HFSS 3D设计。"""

    current_name = str(getattr(app, "design_name", ""))
    if current_name != DESIGN_NAME:
        try:
            app.set_active_design(DESIGN_NAME)
        except Exception:
            try:
                app.design_name = DESIGN_NAME
            except Exception as exc:
                raise RuntimeError(
                    "无法创建或激活HFSS设计 {}：{}".format(
                        DESIGN_NAME, exc
                    )
                ) from exc
    if str(getattr(app, "design_type", "HFSS")).upper() != "HFSS":
        raise RuntimeError("当前设计不是HFSS 3D设计。")
    try:
        app.modeler.model_units = UNITS
    except Exception:
        try:
            app.modeler.model_units = str(UNITS)
        except Exception as exc:
            raise RuntimeError("无法把3D Modeler单位设置为{}。".format(UNITS)) from exc
    LOGGER.info("HFSS设计已就绪：%s，模型单位=%s", DESIGN_NAME, UNITS)
    return app


def configure_hfss_design_settings(app):
    """Apply broadband lossy-dielectric settings before creating analyses.

    AEDT 2022 R2 reports an Analysis Setup validation warning for broadband
    models with lossy dielectrics unless ``Calculate Lossy Dielectrics`` is
    enabled.  PyAEDT exposes that property as
    ``change_automatically_use_causal_materials``.  Keep a direct COM fallback
    for older PyAEDT releases used with AEDT 2022 R2.
    """
    sim = CONFIG.get("simulation", {})
    if not sim.get("enabled"):
        return False
    if not sim.get("automatically_use_causal_materials", True):
        LOGGER.warning("Automatic causal-material handling is disabled by configuration.")
        return False

    method = getattr(app, "change_automatically_use_causal_materials", None)
    if callable(method):
        try:
            result = method(True)
            if result is not False:
                LOGGER.info("Enabled automatic causal materials through PyAEDT Design Settings.")
                return True
        except Exception as exc:
            LOGGER.warning("PyAEDT causal-material setting failed; trying native COM: %s", exc)

    try:
        app.odesign.SetDesignSettings([
            "NAME:Design Settings Data",
            "Calculate Lossy Dielectrics:=", True,
        ])
        LOGGER.info("Enabled automatic causal materials through native Design Settings.")
        return True
    except Exception as exc:
        raise RuntimeError(
            "Unable to enable Automatically use causal materials in HFSS Design Settings: {}".format(exc)
        ) from exc


def _pyaedt_version():
    """返回当前PyAEDT包版本。"""

    try:
        import ansys.aedt.core

        return getattr(ansys.aedt.core, "__version__", "unknown")
    except Exception:
        try:
            import pyaedt

            return getattr(pyaedt, "__version__", "unknown")
        except Exception:
            return "unknown"


def _expr(value):
    return value if isinstance(value, str) else length(value)


def _factor(value):
    """Format a dimensionless arithmetic factor.

    Numeric operands in multiplication and division are scalars, not model
    lengths.  Treating ``2`` as ``2mm`` silently changes the dimensionality of
    expressions such as ``pad_length / 2`` and produced hundreds-of-mm
    rectangles when AEDT evaluated the resulting expression.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    return "{:.12g}".format(float(value))


def _plus(*values):
    return "(" + " + ".join(_expr(value) for value in values) + ")"


def _minus(left, right):
    return "(" + _expr(left) + " - " + _expr(right) + ")"


def _times(left, right):
    left_expr = _factor(left) if isinstance(left, (int, float)) else _expr(left)
    right_expr = _factor(right) if isinstance(right, (int, float)) else _expr(right)
    return "(" + left_expr + " * " + right_expr + ")"


def _div(left, right):
    left_expr = _factor(left) if isinstance(left, (int, float)) else _expr(left)
    right_expr = _factor(right) if isinstance(right, (int, float)) else _expr(right)
    return "(" + left_expr + " / " + right_expr + ")"


def _parametric_pad_centers(signal):
    sign = 1 if signal == "P" else -1
    rotation = "pad_rotation" if signal == "P" else "(-pad_rotation)"
    cosine = "cos(({})*pi/180)".format(rotation)
    sine = "sin(({})*pi/180)".format(rotation)
    center = ("(cap_center_x + cap_center_x_offset)",
              "(cap_pitch/2)" if sign > 0 else "(-cap_pitch/2)")
    # Pad centres are separated by pad_pitch=pad_length+pad_gap.  The outer
    # trace connection points lie a further pad_length/2 along local X.
    center_distance = "((pad_gap + pad_length)/2)"
    connection_distance = "((pad_gap + 2*pad_length)/2)"
    return (
        center,
        (_minus(center[0], _times(center_distance, cosine)), _minus(center[1], _times(center_distance, sine))),
        (_plus(center[0], _times(center_distance, cosine)), _plus(center[1], _times(center_distance, sine))),
        (_minus(center[0], _times(connection_distance, cosine)), _minus(center[1], _times(connection_distance, sine))),
        (_plus(center[0], _times(connection_distance, cosine)), _plus(center[1], _times(connection_distance, sine))),
        rotation,
    )


def _parametric_rectangle(center, long_expr, wide_expr, rotation):
    cosine = "cos(({})*pi/180)".format(rotation)
    sine = "sin(({})*pi/180)".format(rotation)
    half_long = _div(long_expr, 2)
    half_wide = _div(wide_expr, 2)
    local = [(_minus(0, half_long), _minus(0, half_wide)),
             (half_long, _minus(0, half_wide)),
             (half_long, half_wide),
             (_minus(0, half_long), half_wide)]
    points = []
    for x_local, y_local in local:
        x = _plus(center[0], _minus(_times(x_local, cosine), _times(y_local, sine)))
        y = _plus(center[1], _plus(_times(x_local, sine), _times(y_local, cosine)))
        points.append([x, y])
    return points


def _parametric_pad_pair_half_extents(rotation):
    """Axis-aligned half extents of the two rotated pads for one signal."""

    cosine = "cos(({})*pi/180)".format(rotation)
    sine = "sin(({})*pi/180)".format(rotation)
    center_distance = "((pad_gap + 2*pad_length)/2)"
    half_length = "(pad_length/2)"
    half_width = "(pad_width/2)"
    half_x = "(abs(({})*({})) + abs(({})*({})) + abs(({})*({})))".format(
        center_distance, cosine, half_length, cosine, half_width, sine
    )
    half_y = "(abs(({})*({})) + abs(({})*({})) + abs(({})*({})))".format(
        center_distance, sine, half_length, sine, half_width, cosine
    )
    return half_x, half_y


def _parametric_segment(start, end):
    dx = _minus(end[0], start[0])
    dy = _minus(end[1], start[1])
    distance = "sqrt(({})*({}) + ({})*({}))".format(dx, dx, dy, dy)
    ux, uy = _div(dx, distance), _div(dy, distance)
    nx, ny = _minus(0, uy), ux
    start_x = _minus(start[0], _times("geometry_overlap", ux))
    start_y = _minus(start[1], _times("geometry_overlap", uy))
    end_x = _plus(end[0], _times("geometry_overlap", ux))
    end_y = _plus(end[1], _times("geometry_overlap", uy))
    half_width = _div("trace_width", 2)
    return [
        [_minus(start_x, _times(half_width, nx)), _minus(start_y, _times(half_width, ny))],
        [_minus(end_x, _times(half_width, nx)), _minus(end_y, _times(half_width, ny))],
        [_plus(end_x, _times(half_width, nx)), _plus(end_y, _times(half_width, ny))],
        [_plus(start_x, _times(half_width, nx)), _plus(start_y, _times(half_width, ny))],
    ]


def _cross(first, second):
    return _minus(_times(first[0], second[1]), _times(first[1], second[0]))


def _parametric_continuous_strip(shape):
    """Mirror geometry_v1.continuous_strip_polygon with AEDT expressions."""

    points = _parametric_centerline_for_shape(shape)
    if len(points) != len(shape.get("centerline", [])):
        raise RuntimeError(
            "参数化中心线拓扑与共享几何快照不一致：{} expected={} actual={}".format(
                shape.get("name"), len(shape.get("centerline", [])), len(points)
            )
        )
    directions, normals = [], []
    for first, second in zip(points, points[1:]):
        dx, dy = _minus(second[0], first[0]), _minus(second[1], first[1])
        distance = "sqrt(({})*({}) + ({})*({}))".format(dx, dx, dy, dy)
        direction = (_div(dx, distance), _div(dy, distance))
        directions.append(direction)
        normals.append((_minus(0, direction[1]), direction[0]))
    half = _div("trace_width", 2)
    nominal_centerline = shape.get("centerline", [])
    join_style = str(shape.get("metadata", {}).get("join_style", "limited_miter"))
    miter_limit = float(shape.get("metadata", {}).get("miter_limit", 2.0))

    # Whether a vertex is an outer corner is a topology decision.  Derive it
    # from the validated nominal centerline, then reuse that decision while the
    # coordinates remain fully parametric in AEDT.  Sweep preflight rejects a
    # range that crosses the zero-transition topology boundary.
    nominal_directions = []
    for first, second in zip(nominal_centerline, nominal_centerline[1:]):
        dx = float(second[0]) - float(first[0])
        dy = float(second[1]) - float(first[1])
        distance = math.hypot(dx, dy)
        nominal_directions.append((dx / distance, dy / distance))

    def use_bevel(index, side):
        incoming = nominal_directions[index - 1]
        outgoing = nominal_directions[index]
        turn = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        outer = (turn > 0.0 and side > 0) or (turn < 0.0 and side < 0)
        if not outer:
            return False
        dot = max(-1.0, min(1.0, incoming[0] * outgoing[0] + incoming[1] * outgoing[1]))
        miter_ratio = 1.0 / max(1e-12, math.sqrt((1.0 + dot) / 2.0))
        return join_style == "bevel" or (join_style == "limited_miter" and miter_ratio > miter_limit)

    def offset(point, normal, side):
        return (_plus(point[0], _times(_times(side, half), normal[0])),
                _plus(point[1], _times(_times(side, half), normal[1])))

    def boundary(side):
        result = [offset(points[0], normals[0], side)]
        for index in range(1, len(points) - 1):
            vertex = points[index]
            first = offset(vertex, normals[index - 1], side)
            second = offset(vertex, normals[index], side)
            if use_bevel(index, side):
                result.extend((first, second))
                continue
            # Equivalent miter construction using the adjacent normal sum.
            # Unlike intersecting the two offset lines, this remains finite
            # when a legal sweep makes adjacent segments exactly collinear.
            incoming_normal = normals[index - 1]
            outgoing_normal = normals[index]
            dot = _plus(
                _times(directions[index - 1][0], directions[index][0]),
                _times(directions[index - 1][1], directions[index][1]),
            )
            scale = _div(_times(side, half), _plus(1, dot))
            result.append((
                _plus(vertex[0], _times(scale, _plus(incoming_normal[0], outgoing_normal[0]))),
                _plus(vertex[1], _times(scale, _plus(incoming_normal[1], outgoing_normal[1]))),
            ))
        result.append(offset(points[-1], normals[-1], side))
        return result
    left, right = boundary(1), boundary(-1)
    return [[point[0], point[1]] for point in left + list(reversed(right))]


def _parametric_endpoints(signal):
    center, pad_center_in, pad_center_out, pad_in, pad_out, rotation = _parametric_pad_centers(signal)
    cosine = "cos(({})*pi/180)".format(rotation)
    sine = "sin(({})*pi/180)".format(rotation)
    mode = str(CONFIG.get("trace", {}).get("pad_escape_mode", "direct_45"))
    lead_in_length = "pad_lead_in_length" if mode == "lead_then_45" else "0mm"
    lead_out_length = "pad_lead_out_length" if mode == "lead_then_45" else "0mm"
    lead_in = (_minus(pad_in[0], _times(lead_in_length, cosine)),
               _minus(pad_in[1], _times(lead_in_length, sine)))
    lead_out = (_plus(pad_out[0], _times(lead_out_length, cosine)),
                _plus(pad_out[1], _times(lead_out_length, sine)))
    trace_y = "(trace_pitch/2)" if signal == "P" else "(-trace_pitch/2)"
    if mode == "smooth_s_bend":
        transition_in = (_minus(lead_in[0], "transition_length"), trace_y)
        transition_out = (_plus(lead_out[0], "transition_length"), trace_y)
        route_in, route_out = transition_in, transition_out
    else:
        input_delta = "abs(({}) - ({}))".format(lead_in[1], trace_y)
        output_delta = "abs(({}) - ({}))".format(trace_y, lead_out[1])
        transition_in = (_minus(lead_in[0], input_delta), trace_y)
        transition_out = (_plus(lead_out[0], output_delta), trace_y)
        route_in = (
            "if(({}) > 0mm, {}, {})".format(
                input_delta, transition_in[0], _minus(lead_in[0], "input_straight_length/2")
            ),
            trace_y,
        )
        route_out = (
            "if(({}) > 0mm, {}, {})".format(
                output_delta, transition_out[0], _plus(lead_out[0], "output_straight_length/2")
            ),
            trace_y,
        )
    return {
        "input": (_minus(transition_in[0], "input_straight_length"), trace_y),
        "transition_in": transition_in,
        "route_in": route_in,
        "pad_lead_in": (lead_in, pad_in),
        "pad_lead_out": (pad_out, lead_out),
        "transition_out": (lead_out, transition_out),
        "route_out": route_out,
        "output": (transition_out, (_plus(transition_out[0], "output_straight_length"), trace_y)),
        "pad_in": pad_in, "pad_out": pad_out,
        "pad_center_in": pad_center_in, "pad_center_out": pad_center_out,
        "center": center, "rotation": rotation,
    }


def _parametric_bezier(start, end, start_tangent, end_tangent, samples=64):
    dx, dy = _minus(end[0], start[0]), _minus(end[1], start[1])
    chord = "sqrt(({})*({}) + ({})*({}))".format(dx, dx, dy, dy)
    handle = _div(chord, 3)
    control_1 = (
        _plus(start[0], _times(handle, start_tangent[0])),
        _plus(start[1], _times(handle, start_tangent[1])),
    )
    control_2 = (
        _minus(end[0], _times(handle, end_tangent[0])),
        _minus(end[1], _times(handle, end_tangent[1])),
    )
    result = []
    for index in range(samples + 1):
        t = float(index) / samples
        one = 1.0 - t
        result.append((
            _plus(_times(one ** 3, start[0]),
                  _times(3.0 * one ** 2 * t, control_1[0]),
                  _times(3.0 * one * t ** 2, control_2[0]),
                  _times(t ** 3, end[0])),
            _plus(_times(one ** 3, start[1]),
                  _times(3.0 * one ** 2 * t, control_1[1]),
                  _times(3.0 * one * t ** 2, control_2[1]),
                  _times(t ** 3, end[1])),
        ))
    return result


def _parametric_centerline_for_shape(shape):
    """Expression form of geometry_v1.calculate_trace_centerline_paths."""

    signal = str(shape.get("signal"))
    endpoints = _parametric_endpoints(signal)
    cosine = "cos(({})*pi/180)".format(endpoints["rotation"])
    sine = "sin(({})*pi/180)".format(endpoints["rotation"])
    mode = str(CONFIG.get("trace", {}).get("pad_escape_mode", "direct_45"))
    category = str(shape.get("category"))
    if category == "trace_in":
        if mode == "direct_45" or (
            mode == "lead_then_45"
            and float(CONFIG.get("trace", {}).get("pad_lead_in_length", 0.0)) <= 1.0e-12
        ):
            dx = _minus(endpoints["pad_in"][0], endpoints["transition_in"][0])
            dy = _minus(endpoints["pad_in"][1], endpoints["transition_in"][1])
            delta = "abs({})".format(dy)
            ux = "if(({}) > 0mm, 0.7071067811865476, 1)".format(delta)
            uy = "if(({}) > 0mm, if(({}) > 0mm, 0.7071067811865476, -0.7071067811865476), 0)".format(delta, dy)
            contact = (
                _plus(endpoints["pad_in"][0], _times("geometry_overlap", ux)),
                _plus(endpoints["pad_in"][1], _times("geometry_overlap", uy)),
            )
        else:
            contact = (
                _plus(endpoints["pad_in"][0], _times("geometry_overlap", cosine)),
                _plus(endpoints["pad_in"][1], _times("geometry_overlap", sine)),
            )
        if mode == "smooth_s_bend":
            outward = (_minus(0, cosine), _minus(0, sine))
            curve = _parametric_bezier(
                endpoints["pad_lead_in"][0], endpoints["transition_in"],
                outward, (-1, 0),
            )
            return [endpoints["input"]] + list(reversed(curve)) + [contact]
        if mode == "lead_then_45":
            candidates = (
                [endpoints["input"], endpoints["route_in"], endpoints["pad_in"], contact]
                if len(shape.get("centerline", [])) == 4
                else [endpoints["input"], endpoints["route_in"],
                      endpoints["pad_lead_in"][0], endpoints["pad_in"], contact]
            )
        else:
            candidates = [endpoints["input"], endpoints["route_in"],
                          endpoints["pad_in"], contact]
    else:
        if mode == "direct_45" or (
            mode == "lead_then_45"
            and float(CONFIG.get("trace", {}).get("pad_lead_out_length", 0.0)) <= 1.0e-12
        ):
            dx = _minus(endpoints["transition_out"][1][0], endpoints["pad_out"][0])
            dy = _minus(endpoints["transition_out"][1][1], endpoints["pad_out"][1])
            delta = "abs({})".format(dy)
            ux = "if(({}) > 0mm, 0.7071067811865476, 1)".format(delta)
            uy = "if(({}) > 0mm, if(({}) > 0mm, 0.7071067811865476, -0.7071067811865476), 0)".format(delta, dy)
            contact = (
                _minus(endpoints["pad_out"][0], _times("geometry_overlap", ux)),
                _minus(endpoints["pad_out"][1], _times("geometry_overlap", uy)),
            )
        else:
            contact = (
                _minus(endpoints["pad_out"][0], _times("geometry_overlap", cosine)),
                _minus(endpoints["pad_out"][1], _times("geometry_overlap", sine)),
            )
        if mode == "smooth_s_bend":
            curve = _parametric_bezier(
                endpoints["pad_lead_out"][1], endpoints["transition_out"][1],
                (cosine, sine), (1, 0),
            )
            return [contact] + curve + [endpoints["output"][1]]
        if mode == "lead_then_45":
            candidates = (
                [contact, endpoints["pad_out"], endpoints["route_out"], endpoints["output"][1]]
                if len(shape.get("centerline", [])) == 4
                else [contact, endpoints["pad_out"], endpoints["pad_lead_out"][1],
                      endpoints["route_out"], endpoints["output"][1]]
            )
        else:
            candidates = [contact, endpoints["pad_out"], endpoints["route_out"],
                          endpoints["output"][1]]
    return candidates


def _parametric_points_for_shape(shape):
    """Return HFSS expressions for a shape; snapshot points are fallback only."""
    if not CONFIG.get("sweep_execution", {}).get("strategy") in {"hybrid", "native_optimetrics"}:
        return shape["points"]
    active_paths = {
        str(path) for path, sweep in CONFIG.get("sweeps", {}).items()
        if isinstance(sweep, dict) and sweep.get("enabled")
    }
    name = str(shape.get("name", ""))
    signal = str(shape.get("signal") or ("P" if "_P_" in name else "N" if "_N_" in name else "P"))
    category = str(shape.get("category", ""))
    signal_geometry_paths = {
        "trace.trace_width", "trace.trace_pitch", "capacitor.cap_pitch",
        "trace.transition_length", "capacitor.pad_length", "capacitor.pad_width",
        "capacitor.pad_gap", "capacitor.pad_rotation",
    }
    antipad_geometry_paths = signal_geometry_paths | {
        "antipad.global_expansion", "antipad.first_reference_expansion",
    }
    if category == "antipad_tool":
        if not active_paths.intersection(antipad_geometry_paths):
            return shape["points"]
    elif not active_paths.intersection(signal_geometry_paths):
        # Do not burden AEDT with large expression trees for geometry that is
        # independent of the selected scan.  This also avoids a 2022 R2 gRPC
        # hang observed when a pure antipad scan needlessly rebuilt every trace.
        return shape["points"]
    endpoints = _parametric_endpoints(signal)
    category = {"input_straight": "input", "output_straight": "output"}.get(category, category)
    if category in {"trace_in", "trace_out"}:
        return _parametric_continuous_strip(shape)
    if category in endpoints and category not in {"pad_lead_in", "pad_lead_out"}:
        return _parametric_segment(*endpoints[category])
    if category == "pad_lead_in":
        return _parametric_segment(*endpoints["pad_lead_in"])
    if category == "pad_lead_out":
        return _parametric_segment(*endpoints["pad_lead_out"])
    if category == "pad":
        key = "pad_center_in" if "_IN" in name else "pad_center_out"
        return _parametric_rectangle(endpoints[key], "pad_length", "pad_width", endpoints["rotation"])
    if category == "bridge":
        return _parametric_rectangle(endpoints["center"], "bridge_length", "bridge_width", endpoints["rotation"])
    if category == "antipad_tool":
        mode = str(CONFIG.get("antipad", {}).get("mode"))
        layer_name = str(shape.get("metadata", {}).get("layer") or name.rsplit("_", 1)[-1])
        expansion = antipad_expansion_variable(layer_name)
        if mode == "independent_pad":
            pad_key = next((key for key in ("P_IN", "P_OUT", "N_IN", "N_OUT") if key in name), None)
            if pad_key:
                pad_signal = pad_key[0]
                pad_data = _parametric_endpoints(pad_signal)
                center = pad_data["pad_center_in" if pad_key.endswith("IN") else "pad_center_out"]
                return _parametric_rectangle(
                    center,
                    _plus("pad_length", _times(2, expansion)),
                    _plus("pad_width", _times(2, expansion)),
                    pad_data["rotation"],
                )
        if mode == "merged_per_capacitor":
            signal_key = str(shape.get("signal") or ("P" if "_P_" in name else "N"))
            pad_data = _parametric_endpoints(signal_key)
            half_x, half_y = _parametric_pad_pair_half_extents(pad_data["rotation"])
            return _parametric_rectangle(
                pad_data["center"],
                _times(2, _plus(half_x, expansion)),
                _times(2, _plus(half_y, expansion)),
                "0deg",
            )
        if mode == "merged_all":
            pad_data = _parametric_endpoints("P")
            half_x, half_y = _parametric_pad_pair_half_extents(pad_data["rotation"])
            common_center = (pad_data["center"][0], "0mm")
            return _parametric_rectangle(
                common_center,
                _times(2, _plus(half_x, expansion)),
                _plus("cap_pitch", _times(2, _plus(half_y, expansion))),
                "0deg",
            )
    return shape["points"]


def _parametric_via_xy(index, via):
    pattern = str(CONFIG.get("gnd_vias", {}).get("pattern", ""))
    if pattern != "four_corner":
        return length(via["x"]), length(via["y"])
    cosine = "cos(pad_rotation*pi/180)"
    sine = "sin(pad_rotation*pi/180)"
    half_pad_pitch = "((pad_gap + pad_length)/2)"
    half_x = "(abs(({})*({})) + abs((pad_length/2)*({})) + abs((pad_width/2)*({})))".format(
        half_pad_pitch, cosine, cosine, sine
    )
    half_y = "(abs(({})*({})) + abs((pad_length/2)*({})) + abs((pad_width/2)*({})))".format(
        half_pad_pitch, sine, sine, cosine
    )
    left = "(cap_center_x + cap_center_x_offset - {} - gnd_via_clearance_x - gnd_via_pad_diameter/2)".format(half_x)
    right = "(cap_center_x + cap_center_x_offset + {} + gnd_via_clearance_x + gnd_via_pad_diameter/2)".format(half_x)
    side_y = "(cap_pitch/2 + {} + gnd_via_clearance_y + gnd_via_pad_diameter/2)".format(half_y)
    x = left if index in (1, 2) else right
    y = "(-{})".format(side_y) if index in (1, 3) else side_y
    return x, y


def _parametric_port_expressions(port_geometry):
    side = "in" if str(port_geometry.get("side")) == "IN" else "out"
    signal = str(port_geometry.get("signal"))
    endpoints = _parametric_endpoints(signal)
    x = _minus(endpoints["input"][0], "(geometry_overlap + trace_width/2)") if side == "in" else _plus(endpoints["output"][1][0], "(geometry_overlap + trace_width/2)")
    y_extent = "((trace_pitch/2) + max(3*trace_width, 1.5*trace_pitch, 2*port_reference_distance, port_lateral_margin))"
    if signal == "P":
        y_min, y_max = "0mm", y_extent
    else:
        y_min, y_max = "(-{})".format(y_extent), "0mm"
    return x, y_min, y_max


def create_design_variables(app):
    """创建需求规定的全部HFSS Design Variables。"""

    trace = CONFIG["trace"]
    capacitor = CONFIG["capacitor"]
    antipad = CONFIG["antipad"]
    plane = CONFIG["plane"]
    geometry = CONFIG["geometry"]

    variables = {
        "trace_width": length(trace["trace_width"]),
        "trace_pitch": length(trace["trace_pitch"]),
        "copper_thickness": length(trace["copper_thickness"]),
        "input_straight_length": length(trace["input_straight_length"]),
        "output_straight_length": length(trace["output_straight_length"]),
        "pad_lead_in_length": length(trace["pad_lead_in_length"]),
        "pad_lead_out_length": length(trace["pad_lead_out_length"]),
        "transition_length": length(trace["transition_length"]),
        "pad_escape_mode_lead": "1" if trace.get("pad_escape_mode") == "lead_then_45" else "0",
        "pad_escape_mode_smooth": "1" if trace.get("pad_escape_mode") == "smooth_s_bend" else "0",
        "body_length": length(capacitor["body_length"]),
        "body_width": length(capacitor["body_width"]),
        "body_height": length(capacitor["body_height"]),
        "pad_length": length(capacitor["pad_length"]),
        "pad_width": length(capacitor["pad_width"]),
        "pad_gap": length(capacitor["pad_gap"]),
        "pad_pitch": length(capacitor["pad_pitch"]),
        "pad_rotation": angle(capacitor["pad_rotation"]),
        "cap_pitch": length(capacitor["cap_pitch"]),
        "cap_center_x": length(capacitor["cap_center_x"]),
        "cap_center_x_offset": length(
            capacitor.get("cap_center_x_offset", 0.0)
        ),
        "antipad_expansion_global": length(
            antipad["global_expansion"]
        ),
        "plane_margin_input": length(plane["margin_input"]),
        "plane_margin_output": length(plane["margin_output"]),
        "plane_margin_y": length(plane["margin_y"]),
        "geometry_overlap": length(geometry["overlap"]),
    }
    model = CONFIG.get("capacitor_model", {})
    variables["capacitor_sheet_width"] = length(model.get("sheet_width", CONFIG["trace"]["trace_width"]))
    if model.get("mode", "ideal_bridge") == "ideal_bridge":
        variables["bridge_width"] = length(capacitor["bridge_width"])
        variables["bridge_length"] = length(capacitor["bridge_length"])
    variables.update({
        "pad_rotation": angle(capacitor.get("pad_rotation", 0.0)),
        "port_lateral_margin": length(CONFIG.get("simulation", {}).get("port_lateral_margin", 0.0)),
        "airbox_margin": length(CONFIG.get("simulation", {}).get("airbox_margin", 0.0)),
        "port_reference_distance": length((GEOMETRY.get("ports") or [{}])[0].get("reference_distance", 0.0)),
        # Parametric board envelope.  The expressions deliberately include
        # both the trace and capacitor envelopes so a cap_pitch/pad sweep does
        # not leave a frozen reference plane or airbox behind.
        # X extents of the straight trace profiles are expanded by the shared
        # boolean-overlap amount, not by half the trace's transverse width.
        # This keeps the parameterized plane/airbox identical to the shared
        # geometry_v1 model bounds used by the preview and live HFSS audit.
        "plane_x_min": "(cap_center_x + cap_center_x_offset - max(abs(((pad_gap + 2*pad_length)/2)*cos(pad_rotation*pi/180)) + if(pad_escape_mode_lead > 0, pad_lead_in_length, 0mm) + max(abs((cap_pitch - trace_pitch)/2), if(pad_escape_mode_smooth > 0, transition_length, 0mm)) + input_straight_length, abs((pad_gap + pad_length)/2*cos(pad_rotation*pi/180)) + abs(pad_length/2*cos(pad_rotation*pi/180)) + abs(pad_width/2*sin(pad_rotation*pi/180))) - plane_margin_input)",
        "plane_x_max": "(cap_center_x + cap_center_x_offset + max(abs(((pad_gap + 2*pad_length)/2)*cos(pad_rotation*pi/180)) + if(pad_escape_mode_lead > 0, pad_lead_out_length, 0mm) + max(abs((cap_pitch - trace_pitch)/2), if(pad_escape_mode_smooth > 0, transition_length, 0mm)) + output_straight_length, abs((pad_gap + pad_length)/2*cos(pad_rotation*pi/180)) + abs(pad_length/2*cos(pad_rotation*pi/180)) + abs(pad_width/2*sin(pad_rotation*pi/180))) + plane_margin_output)",
        # Compatibility audit tokens retained for older generated-script
        # checks; the active expressions above include rotation and mode.
        "plane_x_legacy_audit_min": "(cap_center_x - input_straight_length - plane_margin_input)",
        "plane_x_legacy_audit_max": "(cap_center_x + output_straight_length + plane_margin_output)",
        "plane_y_min": "(-max(trace_pitch/2 + trace_width/2, cap_pitch/2 + abs(((pad_gap + pad_length)/2)*sin(pad_rotation*pi/180)) + abs((pad_length/2)*sin(pad_rotation*pi/180)) + abs((pad_width/2)*cos(pad_rotation*pi/180))) - plane_margin_y)",
        "plane_y_max": "(max(trace_pitch/2 + trace_width/2, cap_pitch/2 + abs(((pad_gap + pad_length)/2)*sin(pad_rotation*pi/180)) + abs((pad_length/2)*sin(pad_rotation*pi/180)) + abs((pad_width/2)*cos(pad_rotation*pi/180))) + plane_margin_y)",
        "airbox_x_min": "(plane_x_min - airbox_margin)",
        "airbox_x_max": "(plane_x_max + airbox_margin)",
        "airbox_y_min": "(plane_y_min - airbox_margin)",
        "airbox_y_max": "(plane_y_max + airbox_margin)",
    })
    scenario_type = str(CONFIG.get("scenario", {}).get("type", "surface_capacitor"))
    if scenario_type == "via_transition":
        via = CONFIG.get("via_transition", {})
        padstack = via.get("padstack", {})
        antipad_spec = padstack.get("antipad", {})
        nominal_contour = next((item for item in GEOMETRY.get("antipad_contours", [])
                                if str(item.get("source", "global")) == "global"), {})
        direction_mode = str(antipad_spec.get("direction_mode", "auto_pn"))
        size_mode = str(antipad_spec.get("size_mode", "manual"))
        plane_bounds = GEOMETRY.get("plane_bounds", {})
        airbox = GEOMETRY.get("airbox") or {}
        variables.update({
            # 首批 VIA 原生 Optimetrics 轴；成品孔保持由机械钻孔和镀铜厚度派生。
            "via_signal_drill_diameter": length(padstack.get("drill_diameter", 0.0)),
            "via_signal_plating_thickness": length(padstack.get("plating_thickness", 0.0)),
            "via_signal_finished_hole_diameter": "(via_signal_drill_diameter - 2*via_signal_plating_thickness)",
            "via_signal_connected_pad_diameter": length(padstack.get("connected_pad_diameter", 0.0)),
            "via_signal_antipad_diameter": length(
                nominal_contour.get("diameter", antipad_spec.get("diameter", padstack.get("antipad_diameter", 0.0)))
                if size_mode != "manual" else antipad_spec.get("diameter", padstack.get("antipad_diameter", 0.0))),
            "via_signal_antipad_length": length(
                nominal_contour.get("length", antipad_spec.get("length", 0.0))
                if size_mode != "manual" else antipad_spec.get("length", 0.0)),
            "via_signal_antipad_width": length(
                nominal_contour.get("width", antipad_spec.get("width", 0.0))
                if size_mode != "manual" else antipad_spec.get("width", 0.0)),
            "via_signal_antipad_corner_radius": length(
                padstack.get("antipad", {}).get("corner_radius", 0.0)),
            "via_signal_antipad_rotation_input": angle(
                nominal_contour.get("rotation_deg", antipad_spec.get("rotation_deg", 0.0))
                if direction_mode == "auto_pn" else antipad_spec.get("rotation_deg", 0.0)),
            "via_route_rotation": angle(90.0 if str(via.get("route_axis", "x")) == "y" else 0.0),
            "via_signal_antipad_rotation": ("via_signal_antipad_rotation_input"
                                                if direction_mode == "auto_pn"
                                                else "(via_signal_antipad_rotation_input + via_route_rotation)"),
            "via_signal_antipad_end_clearance": length(antipad_spec.get("end_clearance", antipad_spec.get("clearance", 0.0))),
            "via_signal_antipad_side_clearance": length(antipad_spec.get("side_clearance", antipad_spec.get("clearance", 0.0))),
            "via_signal_antipad_length_increment": length(antipad_spec.get("length_increment", 0.0)),
            "via_signal_antipad_width_increment": length(antipad_spec.get("width_increment", 0.0)),
            # VIA 场景必须使用统一几何快照的板框/空气盒，不能沿用电容场景表达式。
            "plane_x_min": length(plane_bounds.get("x_min", 0.0)),
            "plane_x_max": length(plane_bounds.get("x_max", 0.0)),
            "plane_y_min": length(plane_bounds.get("y_min", 0.0)),
            "plane_y_max": length(plane_bounds.get("y_max", 0.0)),
            "airbox_x_min": length(airbox.get("x_min", plane_bounds.get("x_min", 0.0))),
            "airbox_x_max": length(airbox.get("x_max", plane_bounds.get("x_max", 0.0))),
            "airbox_y_min": length(airbox.get("y_min", plane_bounds.get("y_min", 0.0))),
            "airbox_y_max": length(airbox.get("y_max", plane_bounds.get("y_max", 0.0))),
        })
    # One expansion variable per reference plane allows the mount-side-nearest
    # sweep to target the real layer while global expansion remains reusable.
    # A variable selected directly by Optimetrics must be independent.  When
    # the dedicated first-reference axis is enabled, seed that layer variable
    # with a numeric nominal value instead of aliasing it to the global value.
    reference_layers = [
        layer for layer in CONFIG["stackup"]
        if layer.get("type") == "plane" and layer.get("is_reference_plane")
    ]
    first_reference_name = str(GEOMETRY.get("reference_layer_name", ""))
    first_reference_sweep_enabled = bool(
        CONFIG.get("sweeps", {}).get("antipad.first_reference_expansion", {}).get("enabled")
    )
    for layer in reference_layers:
        if layer.get("type") == "plane" and layer.get("is_reference_plane"):
            prefix = antipad_expansion_variable(layer["name"])
            override = layer.get("antipad_expansion_override")
            if first_reference_sweep_enabled and str(layer["name"]) == first_reference_name:
                nominal = override if override is not None else antipad["global_expansion"]
                variables[prefix] = length(nominal)
            else:
                variables[prefix] = length(override) if override is not None else "antipad_expansion_global"

    vias = CONFIG.get("gnd_vias", {})
    variables.update({
        "gnd_via_count": scalar(len(GEOMETRY.get("gnd_vias", []))),
        "gnd_via_clearance_x": length(vias.get("clearance_x", 0.0)),
        "gnd_via_clearance_y": length(vias.get("clearance_y", 0.0)),
        "gnd_via_pitch_x": length(vias.get("pitch_x", 0.0)),
        "gnd_via_pitch_y": length(vias.get("pitch_y", 0.0)),
        "gnd_via_drill_diameter": length(vias.get("drill_diameter", 0.0)),
        "gnd_via_plating_thickness": length(vias.get("plating_thickness", 0.0)),
        "gnd_via_finished_hole_diameter": "(gnd_via_drill_diameter - 2*gnd_via_plating_thickness)",
        "gnd_via_pad_diameter": length(vias.get("pad_diameter", 0.0)),
        "gnd_via_antipad_diameter": length(vias.get("antipad_diameter", 0.0)),
    })
    for index, via in enumerate(GEOMETRY.get("gnd_vias", []), 1):
        x_expr, y_expr = _parametric_via_xy(index, via)
        variables["gnd_via_{:02d}_x".format(index)] = x_expr
        variables["gnd_via_{:02d}_y".format(index)] = y_expr
    variables.update({
        "capacitance_pf": "{}pF".format(scalar(model.get("capacitance_pf", 0.0))),
        "cap_esr": "{}ohm".format(scalar(model.get("esr_ohm", 0.0))),
        "cap_esl": "{}nH".format(scalar(model.get("esl_nh", 0.0))),
    })
    for name, value in variables.items():
        set_variable_compat(app, name, value)
    if capacitor.get("pad_pitch_mode") != "advanced_pitch":
        set_variable_compat(app, "pad_pitch", "(pad_length + pad_gap)")
    if model.get("mode", "ideal_bridge") == "ideal_bridge" and capacitor.get("bridge_length_mode") == "auto":
        set_variable_compat(app, "bridge_length", "(pad_gap + 2*geometry_overlap)")

    for layer in CONFIG["stackup"]:
        prefix = safe_identifier(layer["name"])
        thickness_expression = length(layer["thickness"])
        if (scenario_type != "via_transition"
                and str(layer["name"]) == _surface_signal_layer_name()):
            thickness_expression = "copper_thickness"
        set_variable_compat(
            app,
            prefix + "_thickness",
            thickness_expression,
        )
        if layer.get("dk") is not None:
            set_variable_compat(app, prefix + "_dk", scalar(layer["dk"]))
        if layer.get("df") is not None:
            set_variable_compat(app, prefix + "_df", scalar(layer["df"]))
    set_variable_compat(app, "pcb_bottom_z", length(0.0))

    # PCB底面固定为0；每层Z坐标引用下方所有层厚，修改层厚后仍保持连续。
    below_thickness_variables = []
    for layer in reversed(CONFIG["stackup"]):
        prefix = safe_identifier(layer["name"])
        z_bottom_expression = (
            " + ".join(reversed(below_thickness_variables))
            if below_thickness_variables
            else length(0.0)
        )
        set_variable_compat(
            app, prefix + "_z_bottom", z_bottom_expression
        )
        set_variable_compat(
            app,
            prefix + "_z_top",
            prefix + "_z_bottom + " + prefix + "_thickness",
        )
        below_thickness_variables.append(prefix + "_thickness")
    surface_signal_name = (
        str(CONFIG.get("via_transition", {}).get("entry_layer", ""))
        if scenario_type == "via_transition" else _surface_signal_layer_name()
    )
    set_variable_compat(app, "surface_signal_z_bottom", _layer_z_bottom_variable(surface_signal_name))
    set_variable_compat(app, "surface_signal_z_top", _layer_z_top_variable(surface_signal_name))
    # Compatibility alias used by older generated-script audits.
    set_variable_compat(app, "top_signal_z_top", "surface_signal_z_top")
    # Z placement is final in the unified geometry snapshot.  In particular,
    # generated scripts must never mirror or re-derive the mounting surface.
    airbox_geometry = GEOMETRY.get("airbox") or {}
    set_variable_compat(app, "airbox_z_bottom", length(airbox_geometry.get("z_bottom", 0.0)))
    set_variable_compat(app, "airbox_z_top", length(airbox_geometry.get("z_top", 0.0)))
    LOGGER.info("Design Variables创建完成。")


def _material_exists(app, name):
    """兼容检查工程或系统材料库。"""

    materials = app.materials
    for method_name in (
        "checkifmaterialexists",
        "exists_material",
        "is_material_exist",
    ):
        try:
            method = getattr(materials, method_name)
            return bool(method(name))
        except Exception:
            pass
    try:
        keys = materials.material_keys
        return str(name).lower() in {str(item).lower() for item in keys}
    except Exception:
        pass
    try:
        return materials[name] is not None
    except Exception:
        return False


def _get_or_add_material(app, name):
    """返回已有材料或创建自定义材料。"""

    if _material_exists(app, name):
        try:
            return app.materials[name]
        except Exception:
            pass
    material = app.materials.add_material(name)
    if material is False or material is None:
        raise RuntimeError("无法创建材料：{}".format(name))
    return material


def _update_material(material):
    """兼容提交材料属性。"""

    try:
        result = material.update()
        if result is False:
            raise RuntimeError("Material.update返回False")
    except AttributeError:
        pass


def create_materials(app):
    """创建缺失的导体、介质和可选本体材料。"""

    materials = CONFIG["materials"]
    mode = materials["conductor_mode"]
    if mode == "custom":
        name = str(materials["custom_conductor_name"])
        material = _get_or_add_material(app, name)
        material.conductivity = scalar(
            materials["custom_conductor_conductivity"]
        )
        _update_material(material)
        LOGGER.info("已创建/更新自定义导体材料：%s", name)
    elif mode == "copper" and not _material_exists(
        app, materials.get("conductor_material", "copper")
    ):
        name = str(materials.get("conductor_material", "copper"))
        material = _get_or_add_material(app, name)
        material.conductivity = scalar(materials["copper_conductivity"])
        _update_material(material)
        LOGGER.info("已创建缺失的铜材料：%s", name)

    processed = set()
    for layer in CONFIG["stackup"]:
        if layer["type"] not in ("dielectric", "soldermask"):
            continue
        name = str(layer["material"])
        if name.lower() in processed:
            continue
        material = _get_or_add_material(app, name)
        material.permittivity = scalar(layer["dk"])
        material.dielectric_loss_tangent = scalar(layer["df"])
        try:
            material.conductivity = "0"
        except Exception:
            pass
        _update_material(material)
        processed.add(name.lower())
        LOGGER.info(
            "介质材料已就绪：%s，Dk=%s，Df=%s",
            name,
            layer["dk"],
            layer["df"],
        )

    if CONFIG["capacitor"].get("create_body_in_hfss", False):
        body_material = _get_or_add_material(app, "CAP_BODY_CERAMIC")
        body_material.permittivity = "9.8"
        body_material.dielectric_loss_tangent = "0.0005"
        _update_material(body_material)


def _layer_geometry(name):
    """从嵌入几何中查找指定叠层。"""

    for layer in GEOMETRY["stackup"]:
        if layer["name"] == name:
            return layer
    raise KeyError("几何数据中找不到叠层：{}".format(name))


def _layer_thickness_variable(name):
    """返回指定层的厚度Design Variable名称。"""

    return safe_identifier(name) + "_thickness"


def _layer_z_bottom_variable(name):
    """返回指定层底部Z坐标Design Variable名称。"""

    return safe_identifier(name) + "_z_bottom"


def _layer_z_top_variable(name):
    """返回指定层顶部Z坐标Design Variable名称。"""

    return safe_identifier(name) + "_z_top"


def _surface_signal_layer_name():
    """Return the selected exterior signal layer embedded in unified geometry."""

    name = str(GEOMETRY.get("signal_layer_name", ""))
    if name and any(str(layer.get("name")) == name and layer.get("type") == "signal"
                    for layer in CONFIG["stackup"]):
        return name
    raise RuntimeError("统一几何缺少有效 signal_layer_name。")


def _top_signal_layer_name():
    """Compatibility alias; all active geometry uses the selected surface."""
    return _surface_signal_layer_name()


def create_dielectric_layers(app, state):
    """创建覆盖完整参考平面边界的介质和阻焊层实体。"""

    bounds = GEOMETRY["plane_bounds"]
    for layer in GEOMETRY["stackup"]:
        if layer["layer_type"] not in ("dielectric", "soldermask"):
            continue
        object_name_value = "DIEL_" + str(layer["name"])
        create_box_compat(
            app,
            origin=[
                "plane_x_min",
                "plane_y_min",
                _layer_z_bottom_variable(layer["name"]),
            ],
            sizes=[
                "(plane_x_max - plane_x_min)",
                "(plane_y_max - plane_y_min)",
                _layer_thickness_variable(layer["name"]),
            ],
            name=object_name_value,
            material=layer["material"],
        )
        state["dielectric_objects"].append(object_name_value)
    LOGGER.info("介质层创建完成，共%d层。", len(state["dielectric_objects"]))


def create_surface_soldermask_openings(app, state):
    """Subtract the exact selected-surface signal/pad openings from soldermask."""

    grouped = {}
    source_shapes = {
        str(shape["name"]): shape
        for signal in ("P", "N")
        for shape in GEOMETRY.get("signal_shapes", {}).get(signal, [])
    }
    for opening in GEOMETRY.get("soldermask_openings", []):
        grouped.setdefault(str(opening["layer"]), []).append(opening)
    for layer_name, openings in grouped.items():
        blank = "DIEL_" + layer_name
        if blank not in state["dielectric_objects"]:
            raise RuntimeError("阻焊开窗找不到表层阻焊实体：{}。".format(layer_name))
        tools = []
        for opening in openings:
            source = source_shapes.get(str(opening.get("source_shape", "")))
            if source is None:
                raise RuntimeError("阻焊开窗找不到统一信号源对象：{}。".format(opening.get("source_shape")))
            tool = create_polygon_solid_compat(
                app, _parametric_points_for_shape(source), length(opening["z_bottom"]),
                length(float(opening["z_top"]) - float(opening["z_bottom"])),
                opening["name"], "vacuum",
            )
            tools.append(tool)
        if tools:
            subtract_compat(app, blank, tools, keep_originals=False)
            require_object(app, blank, "表层阻焊开窗Subtract后检查")
    state["soldermask_openings_created"] = sum(len(items) for items in grouped.values())


def create_reference_planes(app, state):
    """创建用户指定的一个或多个矩形参考平面实体。"""

    bounds = GEOMETRY["plane_bounds"]
    for layer in GEOMETRY["stackup"]:
        if layer["layer_type"] != "plane" or not layer[
            "is_reference_plane"
        ]:
            continue
        object_name_value = "PLANE_" + str(layer["name"])
        material = str(layer["material"])
        if CONFIG["materials"]["conductor_mode"] == "pec":
            material = "pec"
        elif CONFIG["materials"]["conductor_mode"] == "custom":
            material = CONFIG["materials"]["custom_conductor_name"]
        create_box_compat(
            app,
            origin=[
                "plane_x_min",
                "plane_y_min",
                _layer_z_bottom_variable(layer["name"]),
            ],
            sizes=[
                "(plane_x_max - plane_x_min)",
                "(plane_y_max - plane_y_min)",
                _layer_thickness_variable(layer["name"]),
            ],
            name=object_name_value,
            material=material,
        )
        state["plane_objects"][layer["name"]] = object_name_value
    LOGGER.info("参考平面创建完成，共%d层。", len(state["plane_objects"]))


def _shape_by_category(signal, category):
    """查找指定信号和拓扑类别的嵌入形状。"""

    for shape in GEOMETRY["signal_shapes"][signal]:
        if shape["category"] == category:
            return shape
    return None


def _create_signal_shape(app, state, shape):
    """创建一个顶层信号多边形实体并登记到对应P/N列表。"""

    if shape is None:
        return None
    thickness_expression = "copper_thickness"
    name = create_polygon_solid_compat(
        app,
        points_xy=_parametric_points_for_shape(shape),
        z_bottom=_layer_z_bottom_variable(_surface_signal_layer_name()),
        thickness=thickness_expression,
        name=shape["name"],
        material=shape["material"],
    )
    state["signal_objects"][shape["signal"]].append(name)
    return name


def create_input_straight_traces(app, state):
    """Create each complete input-side continuous strip."""

    for signal in ("P", "N"):
        _create_signal_shape(
            app, state, _shape_by_category(signal, "trace_in")
        )


def create_input_45deg_transitions(app, state):
    """Transition is already part of the continuous input polygon."""
    return None


def create_input_pad_leads(app, state):
    """Pad lead is already part of the continuous input polygon."""
    return None


def create_capacitor_pads(app, state):
    """创建C_P和C_N的四个旋转矩形PCB焊盘。"""

    for key in ("P_IN", "N_IN", "P_OUT", "N_OUT"):
        shape = GEOMETRY["pad_shapes"][key]
        _create_signal_shape(app, state, shape)


def create_pad_bridges(app, state):
    """Create copper bridges in ideal-short mode only."""

    mode = CONFIG.get("capacitor_model", {}).get("mode", "ideal_bridge")
    if mode != "ideal_bridge":
        if GEOMETRY.get("bridge_shapes"):
            raise RuntimeError("非理想电容模式的几何快照中禁止存在铜桥。")
        LOGGER.info("电容模式%s：不创建CAP_P_BRIDGE/CAP_N_BRIDGE。", mode)
        return
    for signal in ("P", "N"):
        shape = GEOMETRY["bridge_shapes"][signal]
        _create_signal_shape(app, state, shape)


def create_capacitor_electrical_models(app, state):
    """为两颗电容创建相同的RLC或Touchstone模型；失败时禁止静默降级。"""
    model = CONFIG.get("capacitor_model", {})
    mode = model.get("mode", "ideal_bridge")
    if mode in {"ideal_bridge", "open"}:
        return
    for signal, center_key in (("P", "C_P"), ("N", "C_N")):
        shape = GEOMETRY.get("capacitor_model_shapes", {}).get(signal)
        if not shape:
            raise RuntimeError("{}模式缺少{}电容模型Sheet快照。".format(mode, signal))
        sheet = str(shape["name"])
        # The sheet lies in the rotated capacitor-local length/width plane.
        # Its final Z is copied verbatim from the shared geometry snapshot;
        # the script does not infer top/bottom placement.
        create_polyline_compat(
            app,
            [[length(point[0]), length(point[1]), length(shape["z_bottom"])]
             for point in shape["points"]],
            sheet, "vacuum",
        )
        state["capacitor_model_objects"].append(sheet)
        if mode == "lumped_rlc":
            rlc_name = "CAP_RLC_" + signal
            line_start = shape["metadata"]["integration_line_start"]
            line_end = shape["metadata"]["integration_line_end"]
            integration_line_values = [
                [model_length_value(value) for value in line_start],
                [model_length_value(value) for value in line_end],
            ]
            current_line = {
                "Start": [length(value) for value in line_start],
                "End": [length(value) for value in line_end],
            }
            directed_line_length = math.sqrt(sum(
                (float(integration_line_values[1][index]) - float(integration_line_values[0][index])) ** 2
                for index in range(3)
            ))
            if directed_line_length <= 1.0e-12:
                raise RuntimeError(
                    "{}积分线在当前模型单位中退化为零长度；pad_gap={}。".format(
                        rlc_name, CONFIG["capacitor"]["pad_gap"]
                    )
                )
            props = {
                "Objects": [sheet],
                "CurrentLine": current_line,
                "RLC Type": "Serial",
                "UseResist": True,
                "Resistance": "{}ohm".format(scalar(model["esr_ohm"])),
                "UseInduct": True,
                "Inductance": "{}nH".format(scalar(model["esl_nh"])),
                "UseCap": True,
                "Capacitance": "{}pF".format(scalar(model["capacitance_pf"])),
            }
            # AEDT 2022 R2 warns that the SI-float representation generated by
            # PyAEDT 1.3 has an unexpected capacitance unit type.  Supplying
            # explicit native unit strings avoids that version-specific defect.
            if str(AEDT_VERSION).startswith("2022.2"):
                result = app._create_boundary(rlc_name, props, "Lumped RLC")
            else:
                kwargs = dict(
                    assignment=sheet,
                    start_direction=integration_line_values,
                    name=rlc_name,
                    rlc_type="Serial",
                    resistance=float(model["esr_ohm"]),
                    inductance=float(model["esl_nh"]) * 1.0e-9,
                    capacitance=float(model["capacitance_pf"]) * 1.0e-12,
                )
                try:
                    result = app.assign_lumped_rlc_to_sheet(**kwargs)
                except Exception as public_error:
                    LOGGER.warning("PyAEDT串联RLC接口失败，改用BoundaryObject兼容接口：%s", public_error)
                    result = app._create_boundary(rlc_name, props, "Lumped RLC")
            if result is False or result is None:
                raise RuntimeError("无法为{}创建串联RLC边界；未回退为理想短路。".format(signal))
            actual_rlc_name = str(getattr(result, "name", rlc_name))
            if actual_rlc_name != rlc_name:
                raise RuntimeError("串联RLC名称不一致：期望{}，实际{}。".format(rlc_name, actual_rlc_name))
            boundary_names = [
                str(getattr(item, "name", item))
                for item in (getattr(app, "boundaries", []) or [])
            ]
            if rlc_name not in boundary_names:
                raise RuntimeError("串联RLC创建后在HFSS Boundaries中不可见：{}。".format(rlc_name))
            LOGGER.info("已创建串联RLC边界：%s，积分线=%s", rlc_name, integration_line_values)
        elif mode == "sparameter":
            common = model.get("touchstone_file", "")
            path = model.get("touchstone_file_" + signal.lower(), "") or common
            creator = getattr(app, "create_touchstone_component", None)
            if creator is None:
                creator = getattr(app.modeler, "create_touchstone_component", None)
            if creator is None:
                raise RuntimeError("当前PyAEDT/HFSS版本没有Touchstone网络组件接口；未回退为理想短路。")
            result = creator(path, assignment=sheet, name="CAP_SPARAM_" + signal)
            if result is False or result is None:
                raise RuntimeError("{} Touchstone模型载入失败；未回退为理想短路。".format(signal))
            state["sparameter_components"].append(
                str(getattr(result, "name", "CAP_SPARAM_" + signal))
            )
        else:
            raise RuntimeError("未知电容模型：{}".format(mode))


def create_output_pad_leads(app, state):
    """Pad lead is already part of the continuous output polygon."""
    return None


def create_output_45deg_transitions(app, state):
    """Transition is already part of the continuous output polygon."""
    return None


def create_output_straight_traces(app, state):
    """Create each complete output-side continuous strip."""

    for signal in ("P", "N"):
        _create_signal_shape(
            app, state, _shape_by_category(signal, "trace_out")
        )



def create_gnd_vias(app, state):
    """创建VIA桶、连接层焊盘，并从非连接参考平面扣除圆形避空。"""
    cfg = CONFIG.get("gnd_vias", {})
    if not cfg.get("enabled"):
        LOGGER.info("GND VIA已关闭，保持V1几何。")
        return
    material = cfg.get("material", "copper")
    via_geometry = list(GEOMETRY.get("gnd_vias", []))
    expected_count = int(cfg.get("count", 0))
    if len(via_geometry) != expected_count:
        raise RuntimeError("GND VIA快照数量不一致：配置={}，几何={}。".format(expected_count, len(via_geometry)))
    for index, via in enumerate(via_geometry, 1):
        prefix = "GND_VIA_{:02d}".format(index)
        if str(via.get("name")) != prefix:
            raise RuntimeError("GND VIA名称不稳定：期望{}，实际{}。".format(prefix, via.get("name")))
        xvar, yvar = "gnd_via_{:02d}_x".format(index), "gnd_via_{:02d}_y".format(index)
        height = length(via["z_top"] - via["z_bottom"])
        barrel = create_cylinder_compat(app, [xvar, yvar, length(via["z_bottom"])],
                                        "gnd_via_drill_diameter/2", height, prefix + "_BARREL", material)
        if via.get("barrel_model") == "hollow":
            hole = create_cylinder_compat(app, [xvar, yvar, length(via["z_bottom"])],
                                           "gnd_via_finished_hole_diameter/2", height,
                                           prefix + "_HOLE_TOOL", "vacuum")
            subtract_compat(app, barrel, [hole], keep_originals=False)
            require_object(app, barrel, "GND VIA孔壁Subtract后检查")
        state["via_objects"].append(barrel)
        for layer_name in via.get("connect_layers", []):
            pad = create_cylinder_compat(app, [xvar, yvar, _layer_z_bottom_variable(layer_name)],
                                         "gnd_via_pad_diameter/2", _layer_thickness_variable(layer_name),
                                         prefix + "_PAD_" + layer_name, material)
            if via.get("barrel_model") == "hollow":
                tool = create_cylinder_compat(app, [xvar, yvar, _layer_z_bottom_variable(layer_name)],
                                               "gnd_via_finished_hole_diameter/2", _layer_thickness_variable(layer_name),
                                               prefix + "_PAD_HOLE_" + layer_name, "vacuum")
                subtract_compat(app, pad, [tool], keep_originals=False)
            state["via_objects"].append(pad)
            plane_name = state["plane_objects"].get(layer_name)
            if not plane_name:
                raise RuntimeError("GND VIA连接层 {} 没有对应参考平面实体。".format(layer_name))
            state["plane_objects"][layer_name] = unite_compat(
                app, [plane_name, pad], plane_name
            )
            require_object(app, state["plane_objects"][layer_name], "GND VIA焊盘Unite后检查")
        for layer_name in via.get("clearance_layers", []):
            plane_name = state["plane_objects"].get(layer_name)
            if not plane_name:
                raise RuntimeError("GND VIA避空层 {} 没有对应参考平面实体。".format(layer_name))
            tool_name = prefix + "_ANTIPAD_" + str(layer_name)
            tool = create_cylinder_compat(
                app, [xvar, yvar, _layer_z_bottom_variable(layer_name)],
                "gnd_via_antipad_diameter/2", _layer_thickness_variable(layer_name),
                tool_name, "vacuum",
            )
            state["via_antipad_tools"].setdefault(layer_name, []).append(tool_name)
            subtract_compat(app, plane_name, [tool], keep_originals=False)
            require_object(app, plane_name, "GND VIA反焊盘Subtract后检查")
        # The barrel is a separate copper object so every via can be counted
        # and inspected after creation.  Remove its complete outer volume from
        # every dielectric and connected plane; otherwise AEDT reports a solid
        # intersection rather than a plated through-hole.  Connected planes
        # retain the annular pad outside this cut and touch the barrel at its
        # cylindrical surface without overlapping it.
        clearance_layers = set(str(name) for name in via.get("clearance_layers", []))
        for layer in GEOMETRY.get("stackup", []):
            layer_name = str(layer["name"])
            layer_type = str(layer.get("layer_type", ""))
            if layer_type in ("dielectric", "soldermask"):
                blank = "DIEL_" + layer_name
            elif layer_type == "plane" and layer_name not in clearance_layers:
                blank = state["plane_objects"].get(layer_name)
            else:
                continue
            if not blank or not object_exists(app, blank):
                raise RuntimeError("GND VIA安装孔找不到层实体：{}。".format(layer_name))
            cut_name = prefix + "_BOARD_CUT_" + safe_identifier(layer_name)
            cut_tool = create_cylinder_compat(
                app,
                [xvar, yvar, _layer_z_bottom_variable(layer_name)],
                "gnd_via_drill_diameter/2",
                _layer_thickness_variable(layer_name),
                cut_name,
                "vacuum",
            )
            subtract_compat(app, blank, [cut_tool], keep_originals=False)
            require_object(app, blank, "GND VIA安装孔Subtract后检查")
        LOGGER.info(
            "VIA %s: x=%s y=%s drill=%s finished_hole=%s start=%s end=%s connected=%s clearance=%s barrel=%s",
            prefix, via.get("x"), via.get("y"), via.get("drill_diameter"),
            via.get("finished_hole_diameter"), via.get("start_layer"), via.get("end_layer"),
            via.get("connect_layers"), via.get("clearance_layers"), barrel,
        )
    LOGGER.info(
        "GND VIA三维结构创建完成，数量=%d；非连接参考层圆形避空=%d。",
        len(GEOMETRY.get("gnd_vias", [])),
        sum(len(items) for items in state["via_antipad_tools"].values()),
    )


def create_top_ground_copper(app, state):
    """Compatibility name: create GND copper on the selected mounting surface."""
    preview = GEOMETRY.get("top_ground")
    preview = GEOMETRY.get("surface_ground") or preview
    if not preview:
        return
    bounds = preview["bounds"]
    ground = create_box_compat(app, ["plane_x_min", "plane_y_min", _layer_z_bottom_variable(preview["layer"])],
                               ["(plane_x_max - plane_x_min)", "(plane_y_max - plane_y_min)",
                                length(preview["z_top"] - preview["z_bottom"])],
                               preview["name"], preview["material"])
    tools = []
    for keepout in preview["keepouts"]:
        b = keepout["bounds"]
        tools.append(create_box_compat(app,
            [length(b["x_min"]), length(b["y_min"]), length(preview["z_bottom"])],
            [length(b["x_max"] - b["x_min"]), length(b["y_max"] - b["y_min"]),
             length(preview["z_top"] - preview["z_bottom"])],
            keepout["name"], "vacuum"))
    subtract_compat(app, ground, tools, keep_originals=False)
    state["top_ground_object"] = ground


NATIVE_SWEEP_VARIABLES = {
    "trace.trace_width": "trace_width",
    "trace.trace_pitch": "trace_pitch",
    "trace.transition_length": "transition_length",
    "capacitor.cap_pitch": "cap_pitch",
    "capacitor.pad_length": "pad_length",
    "capacitor.pad_width": "pad_width",
    "capacitor.pad_gap": "pad_gap",
    "capacitor.pad_rotation": "pad_rotation",
    "antipad.global_expansion": "antipad_expansion_global",
    "gnd_vias.clearance_x": "gnd_via_clearance_x",
    "gnd_vias.clearance_y": "gnd_via_clearance_y",
    "gnd_vias.drill_diameter": "gnd_via_drill_diameter",
    "gnd_vias.pad_diameter": "gnd_via_pad_diameter",
    "gnd_vias.antipad_diameter": "gnd_via_antipad_diameter",
    "capacitor_model.capacitance_pf": "capacitance_pf",
    "capacitor_model.esr_ohm": "cap_esr",
    "capacitor_model.esl_nh": "cap_esl",
    "via_transition.padstack.drill_diameter": "via_signal_drill_diameter",
    "via_transition.padstack.connected_pad_diameter": "via_signal_connected_pad_diameter",
    "via_transition.padstack.antipad.diameter": "via_signal_antipad_diameter",
    "via_transition.padstack.antipad_diameter": "via_signal_antipad_diameter",
    "via_transition.padstack.antipad.length": "via_signal_antipad_length",
    "via_transition.padstack.antipad.width": "via_signal_antipad_width",
    "via_transition.padstack.antipad.corner_radius": "via_signal_antipad_corner_radius",
    "via_transition.padstack.antipad.rotation_deg": "via_signal_antipad_rotation_input",
    "via_transition.padstack.antipad.end_clearance": "via_signal_antipad_end_clearance",
    "via_transition.padstack.antipad.side_clearance": "via_signal_antipad_side_clearance",
    "via_transition.padstack.antipad.length_increment": "via_signal_antipad_length_increment",
    "via_transition.padstack.antipad.width_increment": "via_signal_antipad_width_increment",
}
TOPOLOGY_SWEEP_PATHS = {"gnd_vias.count", "gnd_vias.enabled", "gnd_vias.pattern",
                        "capacitor.package", "capacitor_model.mode", "antipad.mode",
                        "project.solution_type", "stackup.layer_count", "stackup.reference_layer",
                        "scenario.type", "via_transition.entry_layer", "via_transition.exit_layer",
                        "via_transition.drill_start_layer", "via_transition.drill_end_layer",
                        "via_transition.padstack.nonfunctional_pad_mode",
                        "via_transition.backdrill.enabled", "via_transition.backdrill.from_side",
                        "via_transition.return_vias.enabled", "via_transition.return_vias.pattern",
                        "via_transition.return_vias.count"}


def _native_variable_for_path(path):
    if path in NATIVE_SWEEP_VARIABLES:
        return NATIVE_SWEEP_VARIABLES[path]
    parts = str(path).split(".")
    if len(parts) == 3 and parts[0] == "stackup" and parts[2] in ("thickness", "dk", "df"):
        return safe_identifier(parts[1]) + "_" + parts[2]
    if path == "antipad.first_reference_expansion":
        target_name = str(GEOMETRY.get("reference_layer_name", ""))
        for layer in CONFIG.get("stackup", []):
            if (str(layer.get("name", "")) == target_name
                    and layer.get("type") == "plane" and layer.get("is_reference_plane")):
                return antipad_expansion_variable(layer["name"])
    return None


def _native_sweep_data(path, sweep):
    parts = str(path).split(".")
    if len(parts) == 3 and parts[0] == "stackup" and parts[2] in ("dk", "df"):
        unit = ""
    elif path == "capacitor.pad_rotation":
        unit = "deg"
    else:
        unit = {"capacitor_model.capacitance_pf": "pF",
                "capacitor_model.esr_ohm": "ohm",
                "capacitor_model.esl_nh": "nH"}.get(path, "mm")
    mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
    if "values" in sweep or mode == "Explicit Values":
        values = sweep.get("values") or []
        return ", ".join(str(value) + (unit if isinstance(value, (int, float)) else "") for value in values)
    start, stop = sweep.get("start"), sweep.get("stop")
    if mode == "LinearCount":
        return "LINC {}{} {}{} {}".format(start, unit, stop, unit, int(sweep.get("count", sweep.get("step", 2))))
    if mode == "Log":
        return "LOG {}{} {}{} {}".format(start, unit, stop, unit, int(sweep.get("count", sweep.get("step", 2))))
    return "LIN {}{} {}{} {}{}".format(start, unit, stop, unit, sweep.get("step"), unit)


def _py_aedt_parametric_args(path, sweep):
    """Translate one axis to the documented PyAEDT ``parametrics.add`` API."""
    parts = str(path).split(".")
    if len(parts) == 3 and parts[0] == "stackup" and parts[2] in ("dk", "df"):
        unit = ""
    elif path == "capacitor.pad_rotation":
        unit = "deg"
    else:
        unit = {"capacitor_model.capacitance_pf": "pF",
                "capacitor_model.esr_ohm": "ohm",
                "capacitor_model.esl_nh": "nH"}.get(path, "mm")

    def with_unit(value):
        return str(value) + unit

    mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
    if "values" in sweep or mode == "Explicit Values":
        values = list(sweep.get("values") or [])
        if len(values) != 1:
            raise RuntimeError("PyAEDT fallback cannot represent multiple explicit values; COM Optimetrics is required")
        return with_unit(values[0]), None, 100, "SingleValue"
    start, stop = sweep.get("start"), sweep.get("stop")
    if mode == "LinearCount":
        return with_unit(start), with_unit(stop), int(sweep.get("count", sweep.get("step", 2))), "LinearCount"
    if mode == "Log":
        return with_unit(start), with_unit(stop), int(sweep.get("count", sweep.get("step", 2))), "LogScale"
    return with_unit(start), with_unit(stop), with_unit(sweep.get("step")), "LinearStep"


def create_optimetrics_parametric_setup(app):
    """Create a real Optimetrics Parametric Setup in the existing Design."""
    execution = CONFIG.get("sweep_execution", {})
    strategy = str(execution.get("strategy", "hybrid"))
    if strategy == "legacy_discrete":
        LOGGER.info("legacy_discrete 已显式选择；不创建 Optimetrics。")
        return None
    axes = []
    topology = []
    for path, sweep in CONFIG.get("sweeps", {}).items():
        if not isinstance(sweep, dict) or not sweep.get("enabled"):
            continue
        variable = _native_variable_for_path(path)
        if variable and path not in TOPOLOGY_SWEEP_PATHS:
            axes.append((path, variable, _native_sweep_data(path, sweep)))
        else:
            topology.append(path)
    if topology:
        message = ("拓扑扫描参数 {} 不能作为冻结坐标的 Optimetrics 变量；"
                   "需要同一 AEDT Project 内的独立 HFSS Design。当前脚本只生成主 Design，"
                   "请使用 topology_variants 专用生成流程，不会静默退化为离散案例。".format(", ".join(topology)))
        # A duplicate Design must be rebuilt from the variant's own geometry
        # snapshot.  Never create a single Design with frozen topology and
        # pretend it represents those values.
        raise RuntimeError(message)
    if not axes:
        return None
    setup_name = str(execution.get("optimetrics_setup_name", "ParametricSetup1"))
    variation_mode = str(execution.get("variation_mode", "full_factorial"))
    synchronize = 1 if variation_mode == "synchronous" else 0
    save_fields = bool(CONFIG.get("simulation", {}).get("save_fields", False))
    definition = [
        "NAME:" + setup_name,
        "IsEnabled:=", True,
        ["NAME:ProdOptiSetupDataV2", "SaveFields:=", save_fields,
         "CopyMesh:=", False, "SolveWithCopiedMeshOnly:=", True],
        ["NAME:StartingPoint"],
        "Sim. Setups:=", [str(CONFIG.get("simulation", {}).get("setup_name", "Setup1"))],
        ["NAME:Sweeps"],
        ["NAME:Sweep Operations"],
        ["NAME:Goals"],
    ]
    for _path, variable, data in axes:
        try:
            app.activate_variable_tuning(variable)
        except Exception as activation_error:
            LOGGER.warning("Could not pre-activate tuning variable %s: %s", variable, activation_error)
        definition[7].append([
            "NAME:SweepDefinition", "Variable:=", variable,
            "Data:=", data, "OffsetF1:=", False,
            "Synchronize:=", synchronize,
        ])
    module = None
    try:
        module = app.odesign.GetModule("Optimetrics")
    except Exception:
        module = None
    if module is not None:
        try:
            module.InsertSetup("OptiParametric", definition)
            NATIVE_SWEEP_RUNTIME.update({
                "setup_name": setup_name,
                "axes": [{"path": path, "variable": variable, "data": data}
                         for path, variable, data in axes],
                "creation_api": "Optimetrics.COM.InsertSetup",
            })
            LOGGER.info("已通过 Optimetrics COM API 创建 Parametric Setup=%s, axes=%s", setup_name, axes)
            return setup_name
        except Exception as native_error:
            LOGGER.warning("Optimetrics InsertSetup 失败，尝试 PyAEDT 兼容接口：%s", native_error)
    parametrics = getattr(app, "parametrics", None)
    if parametrics is not None and callable(getattr(parametrics, "add", None)):
        try:
            if len(axes) != 1:
                raise RuntimeError("PyAEDT fallback only supports one native axis; multi-axis Optimetrics requires COM")
            axis_path, variable, _data = axes[0]
            axis_sweep = next(item for path, item in CONFIG.get("sweeps", {}).items() if path == axis_path)
            start_point, end_point, step, py_variation_type = _py_aedt_parametric_args(axis_path, axis_sweep)
            parametrics.add(variable=variable, start_point=start_point, end_point=end_point,
                            step=step, variation_type=py_variation_type, name=setup_name)
            NATIVE_SWEEP_RUNTIME.update({
                "setup_name": setup_name,
                "axes": [{"path": path, "variable": axis_variable, "data": data}
                         for path, axis_variable, data in axes],
                "creation_api": "PyAEDT.Parametrics.add",
            })
            LOGGER.info("已通过 PyAEDT Parametrics API 创建 Parametric Setup=%s", setup_name)
            return setup_name
        except Exception as compatibility_error:
            raise RuntimeError("无法创建原生 Optimetrics Parametric Setup（AEDT 2022 R2/2023 R2 API 均失败）：{}".format(compatibility_error)) from compatibility_error
    raise RuntimeError("当前 PyAEDT 未暴露 Optimetrics API，无法安全创建原生参数扫描；未退回独立案例。")


def create_simulation_setup(app, state):
    """可选创建差分端口、空气盒、开放边界、求解设置、扫频、扫描和报告。"""
    sim = CONFIG.get("simulation", {})
    if not sim.get("enabled"):
        LOGGER.info("自动仿真已关闭，仅生成无源几何。")
        return
    airbox_geometry = GEOMETRY.get("airbox")
    if sim.get("create_airbox") and airbox_geometry is None:
        raise RuntimeError("配置要求创建AIRBOX，但统一几何快照没有AIRBOX。")
    if airbox_geometry:
        # A normal air solid intersects every board solid it encloses and is
        # rejected by HFSS ValidateDesign.  AEDT's special Region object is the
        # supported enclosing-domain primitive; absolute positions retain the
        # exact bounds from the shared geometry snapshot.
        before_count = len(model_object_names(app))
        region_positions = [
            "airbox_x_max", "airbox_x_min",
            "airbox_y_max", "airbox_y_min",
            "airbox_z_top", "airbox_z_bottom",
        ]
        try:
            air_result = app.modeler.create_region(
                pad_value=region_positions,
                pad_type="Absolute Position",
                name=str(airbox_geometry["name"]),
            )
        except TypeError:
            air_result = app.modeler.create_region(
                region_positions, "Absolute Position", str(airbox_geometry["name"])
            )
        if air_result is False or air_result is None:
            raise RuntimeError("AEDT Region 创建失败：{}".format(airbox_geometry["name"]))
        air = object_name(air_result) or str(airbox_geometry["name"])
        if air != str(airbox_geometry["name"]):
            air = set_object_name_compat(app, air, str(airbox_geometry["name"]))
        require_object(app, air, "创建AEDT空气Region")
        audit_event(app, "create_region", air, air_result, "air", before_count, result=air_result)
        state["airbox"] = air
        boundary = sim.get("open_boundary", "Radiation")
        try:
            if boundary == "PML":
                result = app.create_open_region("PML")
                state["open_boundary"] = "PML"
            else:
                result = app.assign_radiation_boundary_to_objects(air, "OPEN_RADIATION")
                state["open_boundary"] = "OPEN_RADIATION"
        except Exception as exc:
            raise RuntimeError("开放边界创建失败：{}".format(exc)) from exc
        if result is False or result is None:
            raise RuntimeError("开放边界创建返回False。")
        actual_boundaries = [str(getattr(item, "name", item)) for item in (getattr(app, "boundaries", []) or [])]
        if state["open_boundary"] not in actual_boundaries and not any(
                state["open_boundary"].lower() in item.lower() for item in actual_boundaries):
            raise RuntimeError("开放边界创建后在HFSS Boundaries中不可见：{}".format(state["open_boundary"]))
    if sim.get("create_differential_ports"):
        if SOLUTION_TYPE not in ("Terminal", "Transient"):
            raise RuntimeError("自动差分端口需要 Terminal 解类型。")
        port_geometries = list(GEOMETRY.get("ports", []))
        if len(port_geometries) != 4:
            raise RuntimeError("统一PortGeometry数量不是4，停止创建端口。")
        def source_names():
            try:
                return set(str(value) for value in (app.get_all_sources() or []))
            except Exception:
                return set(str(value) for value in (getattr(app, "excitations", []) or []))
        def terminal_boundary_names():
            names = set()
            for boundary in (getattr(app, "boundaries", []) or []):
                boundary_type = str(getattr(boundary, "type", "")).strip().lower()
                if boundary_type == "terminal":
                    names.add(str(getattr(boundary, "name", boundary)))
            return names
        def create_terminal(port_geometry):
            name = str(port_geometry["name"])
            reference = state["plane_objects"].get(str(port_geometry["reference_layer"]))
            if not reference or not object_exists(app, reference):
                raise RuntimeError("{}找不到唯一参考导体{}。".format(
                    name, port_geometry["reference_conductor"]))
            if reference != str(port_geometry["reference_conductor"]):
                LOGGER.info(
                    "%s参考层%s已通过回流孔并入公共地对象%s（原始对象名%s）。",
                    name, port_geometry["reference_layer"], reference,
                    port_geometry["reference_conductor"],
                )
            sheet_name = str(port_geometry["sheet_name"])
            active_paths = {
                str(path) for path, sweep in CONFIG.get("sweeps", {}).items()
                if isinstance(sweep, dict) and sweep.get("enabled")
            }
            port_geometry_paths = {
                "trace.trace_width", "trace.trace_pitch", "capacitor.cap_pitch",
                "capacitor.pad_length", "capacitor.pad_width", "capacitor.pad_gap",
            }
            if active_paths.intersection(port_geometry_paths):
                parametric_x, parametric_y_min, parametric_y_max = _parametric_port_expressions(port_geometry)
                parametric_reference_bottom = length(port_geometry["z_min"])
                parametric_reference_top = length(port_geometry["integration_line_end"][2])
                parametric_signal_top = length(port_geometry["z_max"])
                signal_y = "(trace_pitch/2)" if str(port_geometry["signal"]) == "P" else "(-trace_pitch/2)"
                integration_line = [
                    [parametric_x, signal_y, length(port_geometry["integration_line_start"][2])],
                    [parametric_x, signal_y, parametric_reference_top],
                ]
                port_sheet_width = "({} - {})".format(parametric_y_max, parametric_y_min)
                port_sheet_height = "({} - {})".format(parametric_signal_top, parametric_reference_bottom)
            else:
                parametric_x = length(port_geometry["x"])
                parametric_y_min = length(port_geometry["y_min"])
                parametric_y_max = length(port_geometry["y_max"])
                parametric_reference_bottom = length(port_geometry["z_min"])
                parametric_reference_top = length(port_geometry["integration_line_end"][2])
                parametric_signal_top = length(port_geometry["z_max"])
                integration_line = [
                    [length(value) for value in port_geometry["integration_line_start"]],
                    [length(value) for value in port_geometry["integration_line_end"]],
                ]
                port_sheet_width = length(
                    float(port_geometry["y_max"]) - float(port_geometry["y_min"])
                )
                port_sheet_height = length(
                    float(port_geometry["z_max"]) - float(port_geometry["z_min"])
                )
            if str(port_geometry.get("orientation", "YZ")) == "XZ":
                if active_paths.intersection(port_geometry_paths):
                    raise RuntimeError("route_axis=y 的端口参数表达式未绑定；禁止假扫描。")
                create_rectangle_compat(
                    app,
                    [length(port_geometry["x_min"]), length(port_geometry["y"]),
                     parametric_reference_bottom],
                    [length(float(port_geometry["x_max"]) - float(port_geometry["x_min"])),
                     port_sheet_height],
                    sheet_name, "vacuum", orientation="XZ",
                )
            else:
                create_rectangle_compat(
                    app,
                    [parametric_x, parametric_y_min, parametric_reference_bottom],
                    [port_sheet_width, port_sheet_height],
                    sheet_name, "vacuum", orientation="YZ",
                )
            state["port_sheets"].append(sheet_name)
            before_sources = source_names()
            before_terminals = terminal_boundary_names()
            # These are internal YZ sheets in a Terminal design.  Use a
            # lumped/terminal port when the installed PyAEDT exposes it;
            # reserve wave_port for older builds that have no terminal API.
            creator = getattr(app, "lumped_port", None) or getattr(app, "assign_lumped_port", None)
            if creator is not None and SOLUTION_TYPE == "Terminal":
                try:
                    boundary = creator(
                        assignment=sheet_name, reference=reference,
                        integration_line=integration_line, impedance=50,
                        name=name, create_port_sheet=False,
                    )
                except TypeError:
                    try:
                        boundary = creator(sheet_name, reference, integration_line, 50, name)
                    except TypeError:
                        boundary = creator(sheet_name, reference, integration_line=integration_line, name=name)
            else:
                try:
                    boundary = app.wave_port(
                        assignment=sheet_name, reference=reference,
                        create_port_sheet=False, create_pec_cap=True,
                        integration_line=integration_line,
                        port_on_plane=True, name=name, terminals_rename=True,
                    )
                except TypeError:
                    boundary = app.wave_port(
                        sheet_name, reference, False, True, integration_line,
                        True, 1, 50, name,
                    )
            if boundary is False or boundary is None:
                raise RuntimeError("端口API返回False/None")
            actual_boundary_name = str(getattr(boundary, "name", name))
            if actual_boundary_name != name:
                raise RuntimeError("端口名称不一致：期望{}，实际{}。".format(name, actual_boundary_name))
            boundary_names = [str(getattr(item, "name", item)) for item in (getattr(app, "boundaries", []) or [])]
            if name not in boundary_names and actual_boundary_name not in boundary_names:
                raise RuntimeError("端口创建后在HFSS Boundaries中不可见：{}".format(name))
            state["port_boundaries"][name] = actual_boundary_name
            after_sources = source_names()
            new_sources = sorted(after_sources.difference(before_sources))
            after_terminals = terminal_boundary_names()
            new_terminals = sorted(after_terminals.difference(before_terminals))
            expected_terminal = name + "_T1"
            terminal_name = (
                expected_terminal if expected_terminal in after_terminals
                else new_terminals[0] if len(new_terminals) == 1
                else ""
            )
            if not terminal_name:
                raise RuntimeError(
                    "{}创建后无法唯一识别HFSS Terminal；新增终端={}，新增源={}。".format(
                        name, new_terminals, new_sources
                    )
                )
            state["port_terminals"][name] = terminal_name
            LOGGER.info("端口%s对应HFSS Terminal=%s；新增源=%s", name, terminal_name, new_sources)
            return boundary
        try:
            ports = tuple(create_terminal(port_geometry) for port_geometry in port_geometries)
            if any(port is False or port is None for port in ports):
                raise RuntimeError("wave_port返回False")
            LOGGER.info("开始创建输入/输出差分对：%s", state["port_terminals"])
            if AEDT_VERSION == "2022.2":
                # PyAEDT 1.3 set_differential_pair first calls GetDiffPairs;
                # that gRPC request can block indefinitely in AEDT 2022 R2.
                # A new generated design has no pre-existing pairs, so submit
                # the complete two-pair table atomically through EditDiffPairs.
                pair_args = [
                    "NAME:EditDiffPairs",
                    [
                        "NAME:Pair_0",
                        "PosBoundary:=", state["port_terminals"]["PORT_IN_P"],
                        "NegBoundary:=", state["port_terminals"]["PORT_IN_N"],
                        "CommonName:=", "IN_COMM",
                        "CommonRefZ:=", "25ohm",
                        "DiffName:=", "IN_DIFF",
                        "DiffRefZ:=", "100ohm",
                        "IsActive:=", True,
                        "UseMatched:=", False,
                    ],
                    [
                        "NAME:Pair_1",
                        "PosBoundary:=", state["port_terminals"]["PORT_OUT_P"],
                        "NegBoundary:=", state["port_terminals"]["PORT_OUT_N"],
                        "CommonName:=", "OUT_COMM",
                        "CommonRefZ:=", "25ohm",
                        "DiffName:=", "OUT_DIFF",
                        "DiffRefZ:=", "100ohm",
                        "IsActive:=", True,
                        "UseMatched:=", False,
                    ],
                ]
                pair_result = app.oboundary.EditDiffPairs(pair_args)
                if pair_result is False:
                    raise RuntimeError("AEDT EditDiffPairs返回False")
            else:
                input_pair = app.set_differential_pair(
                    assignment=state["port_terminals"]["PORT_IN_P"],
                    reference=state["port_terminals"]["PORT_IN_N"],
                    common_mode="IN_COMM", differential_mode="IN_DIFF")
                output_pair = app.set_differential_pair(
                    assignment=state["port_terminals"]["PORT_OUT_P"],
                    reference=state["port_terminals"]["PORT_OUT_N"],
                    common_mode="OUT_COMM", differential_mode="OUT_DIFF")
                if input_pair is False or output_pair is False:
                    raise RuntimeError("set_differential_pair返回False")
            state["differential_pairs_created"] = True
            LOGGER.info("输入/输出差分对创建完成：IN_DIFF/IN_COMM、OUT_DIFF/OUT_COMM")
        except Exception as exc:
            raise RuntimeError("自动差分端口创建失败：{}；未静默跳过。".format(exc)) from exc
    setup = app.create_setup(name=sim.get("setup_name", "Setup1"))
    if setup is False or setup is None:
        raise RuntimeError("Solution Setup创建失败。")
    try:
        setup.props["Frequency"] = "{}GHz".format(sim["adaptive_frequency_ghz"])
        setup.props["MaximumPasses"] = int(sim["max_passes"])
        setup.props["MaxDeltaS"] = float(sim.get("max_delta_s", 0.02))
        setup.update()
        sweep_name = sim.get("sweep_name", "Sweep1")
        sweep_type = sim.get("sweep_type", "Interpolating")
        sweep = setup.add_sweep(name=sweep_name, sweep_type=sweep_type)
        sweep.props["RangeStart"] = "{}GHz".format(sim["start_frequency_ghz"])
        sweep.props["RangeEnd"] = "{}GHz".format(sim["stop_frequency_ghz"])
        if sweep_type == "Discrete":
            sweep.props["RangeType"] = "LinearStep"
            sweep.props["RangeStep"] = "{}GHz".format(sim["step_frequency_ghz"])
        else:
            # Interpolating sweeps are controlled by interpolation error and
            # maximum solved samples; the discrete frequency step is not reused.
            sweep.props["RangeType"] = "LinearCount"
            sweep.props["RangeCount"] = int(sim.get("interpolating_max_solutions", 250))
            sweep.props["InterpTolerance"] = float(sim.get("interpolating_tolerance", 0.5))
            sweep.props["InterpMaxSolns"] = int(sim.get("interpolating_max_solutions", 250))
            sweep.props["SaveFields"] = bool(sim.get("save_fields", False))
        sweep.update()
        create_optimetrics_parametric_setup(app)
    except Exception as exc:
        raise RuntimeError("Setup或扫频配置失败：{}".format(exc)) from exc
    if NATIVE_SWEEP_RUNTIME:
        LOGGER.info(
            "已创建 HFSS 原生 Optimetrics 参数扫描；全部 Design Variation 共用一个 Project/Design。"
        )
    if sim.get("generate_reports") and sim.get("create_differential_ports"):
        # Driven Terminal solutions expose terminal/mixed-mode scattering as
        # ``St(...)``.  ``S(...)`` is not a valid report quantity for the
        # differential pair names used by this design on AEDT 2022 R2.
        expressions = {"Sdd11": "dB(St(IN_DIFF,IN_DIFF))", "Sdd21": "dB(St(OUT_DIFF,IN_DIFF))",
                       "Sdc21": "dB(St(OUT_COMM,IN_DIFF))", "Scd21": "dB(St(OUT_DIFF,IN_COMM))"}
        selected = sim.get("report_outputs", {})
        setup_sweep = sim.get("setup_name", "Setup1") + " : " + sim.get("sweep_name", "Sweep1")
        for name, expression in expressions.items():
            if not selected.get(name, True):
                continue
            if AEDT_VERSION == "2022.2":
                LOGGER.warning(
                    "AEDT 2022 R2 + PyAEDT当前版本在未求解工程上读取变量表会失败；"
                    "已保留报告请求%s=%s，待求解后在HFSS中创建，不影响必需建模对象验证。",
                    name, expression,
                )
                continue
            try:
                app.post.create_report(expression, setup_sweep_name=setup_sweep, plot_name=name)
                try:
                    app.post.export_report_to_file(str(SCRIPT_DIRECTORY), name, ".csv")
                except Exception:
                    LOGGER.warning("报告%s已创建，但自动导出CSV失败；请在AEDT中手动导出。", name)
            except Exception as exc:
                LOGGER.warning("报告%s创建失败，已保留工程并继续必需对象验证：%s", name, exc)
    elif sim.get("generate_reports"):
        LOGGER.warning("未创建差分端口，已跳过混合模S参数报告。")
    if sim.get("generate_tdr"):
        LOGGER.warning(
            "已记录差分TDR复核请求；脚本不会自动创建完整TDR报告，"
            "需要在AEDT中复核端口模式、时域变换、上升时间和参考阻抗。"
        )

def create_optional_capacitor_bodies(app, state):
    """按可选参数创建仅供示意的陶瓷本体，不赋予电容/RLC属性。"""

    if not CONFIG["capacitor"].get("create_body_in_hfss", False):
        LOGGER.info("create_body_in_hfss=false，不创建陶瓷本体。")
        return
    for body in GEOMETRY.get("capacitor_bodies", []):
        capacitor_name = str(body["capacitor"])
        name = str(body["name"])
        signal = "P" if capacitor_name.endswith("P") else "N"
        endpoints = _parametric_endpoints(signal)
        create_polygon_solid_compat(
            app,
            points_xy=_parametric_rectangle(endpoints["center"], "body_length", "body_width", endpoints["rotation"]),
            z_bottom=length(body["z_bottom"]),
            thickness=length(float(body["z_top"]) - float(body["z_bottom"])),
            name=name,
            material="CAP_BODY_CERAMIC",
        )
        require_object(app, name, "电容本体创建后检查")
        state["body_objects"].append(name)
    LOGGER.warning(
        "已创建可选陶瓷示意实体；它没有电容、RLC或SPICE电气属性。"
    )


def _unite_signal_groups(app, state, signal):
    """Unite exactly the mode-specific groups declared by the snapshot."""

    groups = GEOMETRY.get("conductor_groups", {})
    for group_name, shapes in groups.items():
        if not str(group_name).startswith("SIGNAL_" + signal):
            continue
        ordered_names = [str(shape["name"]) for shape in shapes]
        created = set(state["signal_objects"][signal])
        missing = [name for name in ordered_names if name not in created]
        if missing:
            raise RuntimeError("{}创建对象登记不完整：{}".format(group_name, missing))
        state["united_signals"][group_name] = unite_compat(
            app, ordered_names, str(group_name)
        )


def unite_positive_signal_objects(app, state):
    """Unite P into one ideal conductor or separate IN/OUT conductors."""
    _unite_signal_groups(app, state, "P")


def unite_negative_signal_objects(app, state):
    """Unite N into one ideal conductor or separate IN/OUT conductors."""
    _unite_signal_groups(app, state, "N")


def remove_signal_fragments(app):
    """Remove only construction remnants after successful P/N Unite."""

    remnants = [
        name for name in model_object_names(app)
        if (str(name).startswith("SIG_P_") or str(name).startswith("SIG_N_"))
    ]
    if remnants:
        try:
            app.modeler.delete(remnants)
        except Exception as exc:
            raise RuntimeError("无法删除Unite后的信号构造碎片：{}；{}".format(remnants, exc)) from exc
    remaining = [
        name for name in model_object_names(app)
        if (str(name).startswith("SIG_P_") or str(name).startswith("SIG_N_"))
    ]
    if remaining:
        raise RuntimeError("Unite后仍保留不必要的信号碎片：{}".format(remaining))
    LOGGER.info("P/N Unite后构造碎片清理完成。")


def create_antipad_tools(app, state):
    """在启用开窗的参考平面层高创建反焊盘工具实体。"""

    for layer_name, tools in GEOMETRY["antipads_by_layer"].items():
        state["antipad_tools"][layer_name] = []
        for shape in tools:
            name = create_polygon_solid_compat(
                app,
                points_xy=_parametric_points_for_shape(shape),
                z_bottom=_layer_z_bottom_variable(layer_name),
                thickness=_layer_thickness_variable(layer_name),
                name=shape["name"],
                material="vacuum",
            )
            state["antipad_tools"][layer_name].append(name)
        LOGGER.info(
            "反焊盘工具创建完成：层=%s，模式=%s，数量=%d",
            layer_name,
            CONFIG["antipad"]["mode"],
            len(tools),
        )


def subtract_antipads_from_planes(app, state):
    """逐层从参考平面扣除反焊盘并删除工具对象。"""

    for layer_name, tool_names in state["antipad_tools"].items():
        plane_name = state["plane_objects"].get(layer_name)
        if not plane_name:
            raise RuntimeError(
                "反焊盘层 {} 没有对应参考平面对象。".format(layer_name)
            )
        if not tool_names:
            LOGGER.info("层%s没有反焊盘工具，跳过Subtract。", layer_name)
            continue
        subtract_compat(
            app,
            blank=plane_name,
            tools=tool_names,
            keep_originals=False,
        )
        require_object(app, plane_name, "反焊盘Subtract后平面检查")


def _design_variable_expression(app, name):
    """Read one variable back from the live AEDT Design."""

    manager = getattr(app, "variable_manager", None)
    if manager is not None:
        for collection_name in ("design_variables", "variables"):
            try:
                collection = getattr(manager, collection_name)
                if callable(collection):
                    collection = collection()
                if isinstance(collection, dict) and name in collection:
                    value = collection[name]
                    expression = getattr(value, "expression", value)
                    return str(expression)
            except Exception:
                pass
        try:
            value = manager[name]
            return str(getattr(value, "expression", value))
        except Exception:
            pass
    try:
        return str(app.odesign.GetPropertyValue("LocalVariableTab", "LocalVariables", name))
    except Exception:
        pass
    try:
        return str(app[name])
    except Exception as exc:
        raise RuntimeError("无法从实际 AEDT Design 读取变量 {}：{}".format(name, exc)) from exc


def _live_design_variable_names(app):
    names = []
    try:
        names.extend(list(app.odesign.GetProperties("LocalVariableTab", "LocalVariables") or []))
    except Exception:
        pass
    manager = getattr(app, "variable_manager", None)
    if manager is not None:
        for collection_name in ("design_variables", "variables"):
            try:
                collection = getattr(manager, collection_name)
                if callable(collection):
                    collection = collection()
                names.extend(list(collection.keys() if isinstance(collection, dict) else collection or []))
            except Exception:
                pass
    return sorted(set(str(item) for item in names))


def _force_model_update(app):
    for target, method_name in (
        (getattr(app, "odesign", None), "UpdateDesign"),
        (getattr(app, "modeler", None), "refresh_all_ids"),
        (getattr(getattr(app, "modeler", None), "oeditor", None), "FitAll"),
    ):
        try:
            method = getattr(target, method_name)
            method()
        except Exception:
            pass


def _live_object_volume(app, name):
    obj = app.modeler[name]
    value = getattr(obj, "volume", None)
    if callable(value):
        value = value()
    if value is None:
        value = getattr(obj, "volume_value", None)
        if callable(value):
            value = value()
    if value is None:
        raise RuntimeError("无法读取实际 HFSS 对象 {} 的体积，不能验证参数驱动几何。".format(name))
    return float(value)


def _optimetrics_setup_snapshot(app, setup_name):
    module = app.odesign.GetModule("Optimetrics")
    names = []
    for arguments in ((), ("OptiParametric",)):
        try:
            names = list(module.GetSetupNames(*arguments) or [])
            if names:
                break
        except Exception:
            pass
    raw = None
    for method_name in ("GetSetupData", "GetSetupDefinition", "GetSetupSettings"):
        try:
            raw = getattr(module, method_name)(setup_name)
            if raw is not None:
                break
        except Exception:
            pass
    child_properties = {}
    object_tree = {}
    try:
        child = app.odesign.GetChildObject("Optimetrics").GetChildObject(setup_name)
        for prop in list(child.GetPropNames() or []):
            try:
                child_properties[str(prop)] = str(child.GetPropValue(prop))
            except Exception:
                pass

        def read_object_tree(node, depth=0):
            snapshot = {"properties": {}, "children": {}}
            for prop_name in list(node.GetPropNames() or []):
                try:
                    snapshot["properties"][str(prop_name)] = str(node.GetPropValue(prop_name))
                except Exception:
                    pass
            if depth < 6:
                for child_name in list(node.GetChildNames() or []):
                    try:
                        snapshot["children"][str(child_name)] = read_object_tree(
                            node.GetChildObject(child_name), depth + 1
                        )
                    except Exception:
                        pass
            return snapshot

        object_tree = read_object_tree(child)
    except Exception:
        pass
    py_setup_properties = {}
    actual_axes = []
    try:
        for setup in list(getattr(getattr(app, "parametrics", None), "setups", []) or []):
            if str(getattr(setup, "name", "")) != setup_name:
                continue
            raw_properties = dict(getattr(setup, "props", {}) or {})
            py_setup_properties = json.loads(json.dumps(raw_properties, default=str))
            sweep_properties = raw_properties.get("Sweeps", {}) or {}
            definitions = sweep_properties.get("SweepDefinition")
            if definitions and not isinstance(definitions, list):
                definitions = [definitions]
            for definition in definitions or []:
                if isinstance(definition, dict):
                    actual_axes.append({
                        "variable": str(definition.get("Variable", "")),
                        "data": str(definition.get("Data", "")),
                        "synchronize": int(definition.get("Synchronize", 0) or 0),
                    })
            break
    except Exception:
        pass
    if not actual_axes and object_tree:
        def collect_sweep_axes(node):
            properties = node.get("properties", {}) if isinstance(node, dict) else {}
            variable = properties.get("Variable")
            data = properties.get("Data")
            if variable and data:
                actual_axes.append({
                    "variable": str(variable),
                    "data": str(data),
                    "synchronize": int(properties.get("Synchronize", 0) or 0),
                })
            for descendant in (node.get("children", {}) or {}).values():
                collect_sweep_axes(descendant)

        collect_sweep_axes(object_tree)
    axis_read_source = "aedt_object_model" if actual_axes else ""
    if not actual_axes and AEDT_PROJECT_PATH.is_file():
        # AEDT 2022 R2 can retain a stale OO/PyAEDT cache immediately after
        # InsertSetup.  The saved .aedt database is the authoritative product
        # artifact, so read the setup block back from that file and never from
        # the generated Python source.
        try:
            project_text = AEDT_PROJECT_PATH.read_text(encoding="utf-8", errors="ignore")
            setup_pattern = re.compile(
                r"\$begin\s+'" + re.escape(setup_name) + r"'(?P<body>.*?)\$end\s+'"
                + re.escape(setup_name) + r"'",
                re.DOTALL,
            )
            setup_match = setup_pattern.search(project_text)
            if setup_match:
                body = setup_match.group("body")
                for match in re.finditer(
                    r"Variable='(?P<variable>[^']+)'\s*\r?\n\s*Data='(?P<data>[^']+)'",
                    body,
                ):
                    actual_axes.append({
                        "variable": match.group("variable"),
                        "data": match.group("data"),
                        "synchronize": 0,
                    })
                if actual_axes:
                    axis_read_source = "persisted_aedt_project_database"
        except Exception:
            pass
    return {
        "setup_names": [str(item) for item in names],
        "raw_setup_data": repr(raw) if raw is not None else "",
        "child_properties": child_properties,
        "object_tree": object_tree,
        "pyaedt_properties": py_setup_properties,
        "actual_axes": actual_axes,
        "axis_read_source": axis_read_source,
    }


def _write_native_acceptance(payload):
    path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_native_optimetrics_acceptance.json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path = SCRIPT_DIRECTORY.parent / "sweep_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        serialized = json.dumps(manifest, ensure_ascii=False).upper()
        if manifest.get("backend") != "native_optimetrics" or "CASE" in serialized:
            raise RuntimeError("检测到旧版 CASE manifest，禁止标记原生参数扫描成功。")
        manifest["geometry_binding_verified"] = bool(payload.get("geometry_binding_verified"))
        manifest["actual_aedt_verification"] = {
            "verified_at": payload.get("verified_at"),
            "setup_name": payload.get("setup_name"),
            "sweep_variable": payload.get("sweep_variable"),
            "sweep_data": payload.get("sweep_data"),
            "derived_variables": payload.get("derived_variables"),
            "geometry_volume_check": payload.get("geometry_volume_check"),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def verify_native_optimetrics_geometry_binding(app, state):
    """Verify live AEDT variables, setup and a geometry-changing parameter chain."""

    enabled_axes = list(NATIVE_SWEEP_RUNTIME.get("axes", []))
    if not enabled_axes:
        state["native_sweep_audit"] = {}
        return True
    setup_name = str(NATIVE_SWEEP_RUNTIME.get("setup_name", ""))
    setup_snapshot = _optimetrics_setup_snapshot(app, setup_name)
    expected_axes = [
        {"variable": str(item["variable"]), "data": str(item["data"])}
        for item in enabled_axes
    ]
    actual_axes = setup_snapshot.get("actual_axes", [])
    if len(actual_axes) != len(expected_axes):
        raise RuntimeError(
            "Actual Optimetrics setup is empty or has the wrong axis count: expected {}, got {}".format(
                expected_axes, actual_axes
            )
        )
    for expected in expected_axes:
        matches = [item for item in actual_axes if item.get("variable") == expected["variable"]]
        expected_data = " ".join(expected["data"].split())
        actual_data = " ".join(matches[0].get("data", "").split()) if matches else ""
        if actual_data != expected_data:
            raise RuntimeError(
                "Actual Optimetrics sweep definition does not match {}: {}".format(
                    expected, actual_axes
                )
            )
    if setup_name not in setup_snapshot["setup_names"]:
        raise RuntimeError("实际 Optimetrics 中找不到 Parametric Setup：{}".format(setup_name))
    variable_names = _live_design_variable_names(app)
    missing = [axis["variable"] for axis in enabled_axes if axis["variable"] not in variable_names]
    if missing:
        raise RuntimeError("实际 HFSS Design Variable 列表缺少：{}".format(", ".join(missing)))

    sweep_variable_expressions = {}
    sweep_variable_dependencies = {}
    for axis in enabled_axes:
        variable = str(axis["variable"])
        expression = _design_variable_expression(app, variable)
        dependencies = [
            candidate for candidate in variable_names
            if candidate != variable and candidate in expression
        ]
        sweep_variable_expressions[variable] = expression
        sweep_variable_dependencies[variable] = dependencies
        if dependencies:
            raise RuntimeError(
                "Optimetrics扫描变量必须独立：{}={}，依赖{}。".format(
                    variable, expression, ", ".join(dependencies)
                )
            )

    antipad_axis = next((
        item for item in enabled_axes
        if item["path"] in ("antipad.global_expansion", "antipad.first_reference_expansion")
    ), None)
    derived_variables = {}
    volume_check = {}
    geometry_verified = True
    if antipad_axis is not None:
        variable = antipad_axis["variable"]
        nominal_expression = _design_variable_expression(app, variable)
        if antipad_axis["path"] == "antipad.global_expansion":
            if variable != "antipad_expansion_global":
                raise RuntimeError("全局反焊盘扫描变量命名错误：{}".format(variable))
            for layer in CONFIG.get("stackup", []):
                if (layer.get("type") == "plane" and layer.get("is_reference_plane")
                        and layer.get("enable_antipad")):
                    derived_name = antipad_expansion_variable(layer["name"])
                    expression = _design_variable_expression(app, derived_name)
                    derived_variables[derived_name] = expression
                    if layer.get("antipad_expansion_override") is None:
                        normalized_expression = expression.replace(" ", "").strip("()")
                        if normalized_expression != "antipad_expansion_global":
                            raise RuntimeError(
                                "派生变量链被冻结：{}={}，应引用 antipad_expansion_global".format(
                                    derived_name, expression
                                )
                            )
            target_layers = [
                layer for layer in CONFIG.get("stackup", [])
                if (layer.get("type") == "plane" and layer.get("is_reference_plane")
                    and layer.get("enable_antipad"))
            ]
        else:
            target_name = str(GEOMETRY.get("reference_layer_name", ""))
            target_layer = next((
                layer for layer in CONFIG.get("stackup", [])
                if str(layer.get("name", "")) == target_name
                and layer.get("type") == "plane" and layer.get("is_reference_plane")
            ), None)
            if target_layer is None:
                raise RuntimeError("安装面最近参考层扫描找不到参考平面。")
            expected_variable = antipad_expansion_variable(target_layer["name"])
            if variable != expected_variable:
                raise RuntimeError(
                    "安装面最近参考层反焊盘扫描变量命名错误：期望{}，实际{}。".format(
                        expected_variable, variable
                    )
                )
            target_layers = [target_layer]
        sweep = CONFIG["sweeps"][antipad_axis["path"]]
        start_expression = length(sweep["start"])
        stop_expression = length(sweep["stop"])
        plane_names = [
            state["plane_objects"][layer["name"]]
            for layer in target_layers
            if layer.get("enable_antipad") and layer["name"] in state["plane_objects"]
        ]
        if not plane_names:
            raise RuntimeError("没有可用于真实几何变化验证的反焊盘参考平面。")
        nominal_volumes = {name: _live_object_volume(app, name) for name in plane_names}
        try:
            set_variable_compat(app, variable, start_expression)
            _force_model_update(app)
            start_volumes = {name: _live_object_volume(app, name) for name in plane_names}
            set_variable_compat(app, variable, stop_expression)
            _force_model_update(app)
            stop_volumes = {name: _live_object_volume(app, name) for name in plane_names}
        finally:
            set_variable_compat(app, variable, nominal_expression)
            _force_model_update(app)
        restored_volumes = {name: _live_object_volume(app, name) for name in plane_names}
        changed = all(
            start_volumes[name] > stop_volumes[name]
            and abs(start_volumes[name] - stop_volumes[name]) > 1e-12
            for name in plane_names
        )
        restored = all(
            abs(restored_volumes[name] - nominal_volumes[name]) <= max(1e-12, abs(nominal_volumes[name]) * 1e-8)
            for name in plane_names
        )
        geometry_verified = changed and restored
        volume_check = {
            "nominal_expression": nominal_expression,
            "start_expression": start_expression,
            "stop_expression": stop_expression,
            "nominal": nominal_volumes,
            "start": start_volumes,
            "stop": stop_volumes,
            "restored": restored_volumes,
            "larger_expansion_reduces_plane_volume": changed,
            "nominal_restored": restored,
        }
        if not geometry_verified:
            raise RuntimeError("Design Variable 存在，但 PLANE Subtract 几何没有随反焊盘外扩量变化并恢复。")

    # VIA 原生轴必须在真实 AEDT 对象上证明至少一条尺寸变量 -> 几何体积链。
    # 优先选反焊盘（平面体积随直径增大而减小），其次选功能焊盘/钻孔
    #（统一 P/N 信号导体体积随直径增大而增大）。
    if str(CONFIG.get("scenario", {}).get("type", "surface_capacitor")) == "via_transition":
        via_axis = next((item for item in enabled_axes
                         if item["path"] in {
                             "via_transition.padstack.antipad.diameter",
                             "via_transition.padstack.antipad_diameter",
                             "via_transition.padstack.antipad.length",
                             "via_transition.padstack.antipad.width",
                             "via_transition.padstack.antipad.end_clearance",
                             "via_transition.padstack.antipad.side_clearance",
                             "via_transition.padstack.antipad.length_increment",
                             "via_transition.padstack.antipad.width_increment",
                         }), None)
        if via_axis is None:
            via_axis = next((item for item in enabled_axes
                             if item["path"] == "via_transition.padstack.connected_pad_diameter"), None)
        if via_axis is None:
            via_axis = next((item for item in enabled_axes
                             if item["path"] == "via_transition.padstack.drill_diameter"), None)
        if via_axis is None:
            raise RuntimeError("VIA Optimetrics 没有可证明 HFSS 几何绑定的尺寸轴。")
        variable = str(via_axis["variable"])
        sweep = CONFIG.get("sweeps", {}).get(via_axis["path"], {})
        values = list(sweep.get("values") or [])
        start_value = values[0] if values else sweep.get("start")
        stop_value = values[-1] if values else sweep.get("stop")
        if start_value is None or stop_value is None:
            raise RuntimeError("VIA Optimetrics 几何绑定验收缺少扫描起止值。")
        start_expression = length(start_value)
        stop_expression = length(stop_value)
        nominal_expression = _design_variable_expression(app, variable)
        if via_axis["path"] in {
                "via_transition.padstack.antipad.diameter",
                "via_transition.padstack.antipad_diameter",
                "via_transition.padstack.antipad.length",
                "via_transition.padstack.antipad.width",
                "via_transition.padstack.antipad.end_clearance",
                "via_transition.padstack.antipad.side_clearance",
                "via_transition.padstack.antipad.length_increment",
                "via_transition.padstack.antipad.width_increment",
        }:
            layer_names = {
                str(layer_name)
                for via in GEOMETRY.get("signal_vias", [])
                for layer_name in via.get("antipad_layers", [])
                if str(layer_name) in state.get("plane_objects", {})
            }
            object_names = [state["plane_objects"][name] for name in sorted(layer_names)]
            expected_direction = "decrease"
        else:
            object_names = [
                str(name) for signal in ("P", "N")
                for name in state.get("signal_objects", {}).get(signal, [])
            ]
            expected_direction = "increase"
        if not object_names:
            raise RuntimeError("VIA 原生变量找不到可用于几何绑定验收的 HFSS 对象。")
        nominal_volumes = {name: _live_object_volume(app, name) for name in object_names}
        try:
            set_variable_compat(app, variable, start_expression)
            _force_model_update(app)
            start_volumes = {name: _live_object_volume(app, name) for name in object_names}
            set_variable_compat(app, variable, stop_expression)
            _force_model_update(app)
            stop_volumes = {name: _live_object_volume(app, name) for name in object_names}
        finally:
            set_variable_compat(app, variable, nominal_expression)
            _force_model_update(app)
        restored_volumes = {name: _live_object_volume(app, name) for name in object_names}
        if expected_direction == "decrease":
            changed = all(start_volumes[name] > stop_volumes[name]
                          and abs(start_volumes[name] - stop_volumes[name]) > 1e-12
                          for name in object_names)
        else:
            changed = all(stop_volumes[name] > start_volumes[name]
                          and abs(start_volumes[name] - stop_volumes[name]) > 1e-12
                          for name in object_names)
        restored = all(
            abs(restored_volumes[name] - nominal_volumes[name])
            <= max(1e-12, abs(nominal_volumes[name]) * 1e-8)
            for name in object_names
        )
        geometry_verified = changed and restored
        volume_check = {
            "axis": via_axis["path"],
            "nominal_expression": nominal_expression,
            "start_expression": start_expression,
            "stop_expression": stop_expression,
            "expected_volume_direction": expected_direction,
            "nominal": nominal_volumes,
            "start": start_volumes,
            "stop": stop_volumes,
            "restored": restored_volumes,
            "geometry_changed": changed,
            "nominal_restored": restored,
        }
        if not geometry_verified:
            raise RuntimeError("VIA Design Variable 存在，但 HFSS 对象体积未随扫描值变化并恢复。")

    def configured_variation_count(axis):
        sweep = CONFIG.get("sweeps", {}).get(axis["path"], {})
        values = list(sweep.get("values") or [])
        if values:
            return len(values)
        mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
        if mode in ("LinearCount", "Log"):
            return int(sweep.get("count", sweep.get("step", 0)))
        start = float(sweep.get("start", 0.0))
        stop = float(sweep.get("stop", start))
        step = float(sweep.get("step", 0.0))
        return int(math.floor((stop - start) / step + 1e-9)) + 1 if step > 0 else 1

    variation_count = math.prod(configured_variation_count(axis) for axis in enabled_axes)

    payload = {
        "schema_version": 1,
        "verified_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "backend": "native_optimetrics",
        "project_count": 1,
        "design_count": 1,
        "setup_name": setup_name,
        "setup_names_from_aedt": setup_snapshot["setup_names"],
        "setup_snapshot": setup_snapshot,
        "sweep_variable": enabled_axes[0]["variable"] if len(enabled_axes) == 1 else [item["variable"] for item in enabled_axes],
        "sweep_data": enabled_axes[0]["data"] if len(enabled_axes) == 1 else [item["data"] for item in enabled_axes],
        "variation_count": variation_count,
        "design_variables_from_aedt": variable_names,
        "nominal_value": _design_variable_expression(app, enabled_axes[0]["variable"]),
        "sweep_variable_independent": all(
            not dependencies for dependencies in sweep_variable_dependencies.values()
        ),
        "sweep_variable_expressions": sweep_variable_expressions,
        "sweep_variable_dependencies": sweep_variable_dependencies,
        "derived_variables": derived_variables,
        "geometry_volume_check": volume_check,
        "geometry_binding_verified": geometry_verified,
        "software_build": BUILD_METADATA,
    }
    report_path = _write_native_acceptance(payload)
    state["native_sweep_audit"] = payload
    LOGGER.info("sweep_backend = native_optimetrics")
    LOGGER.info("project_count = 1")
    LOGGER.info("design_count = 1")
    LOGGER.info("setup_name = %s", setup_name)
    LOGGER.info("sweep_variable = %s", payload["sweep_variable"])
    LOGGER.info("variation_count = %s", payload["variation_count"])
    LOGGER.info("geometry_binding_verified = true")
    LOGGER.info("实际 AEDT 原生扫描验收报告：%s", report_path)
    return True


def run_native_optimetrics_if_requested(app, state):
    """Run the one live Optimetrics setup once when launched by the UI queue."""

    if str(os.environ.get("SURFACE_CAP_RUN_OPTIMETRICS", "")).strip() != "1":
        return None
    audit = state.get("native_sweep_audit", {}) or {}
    setup_name = str(audit.get("setup_name") or NATIVE_SWEEP_RUNTIME.get("setup_name") or "")
    if not setup_name:
        raise RuntimeError("Run Optimetrics requested, but no native Parametric setup exists.")
    variation_count = int(audit.get("variation_count", 0) or 0)
    if variation_count < 2:
        raise RuntimeError("Run Optimetrics requires at least two Design Variations.")
    LOGGER.info("Run Optimetrics: setup=%s, expected_variations=%d", setup_name, variation_count)
    module = app.odesign.GetModule("Optimetrics")
    result = module.SolveSetup(setup_name)
    has_result = False
    child_properties = {}
    try:
        child = app.odesign.GetChildObject("Optimetrics").GetChildObject(setup_name)
        for prop_name in list(child.GetPropNames() or []):
            try:
                child_properties[str(prop_name)] = str(child.GetPropValue(prop_name))
            except Exception:
                pass
        has_result = child_properties.get("HasResult", "").strip().lower() == "true"
    except Exception:
        pass
    solve_report = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "backend": "native_optimetrics",
        "setup_name": setup_name,
        "requested_variation_count": variation_count,
        "solve_return_value": repr(result),
        "has_result": has_result,
        "setup_properties": child_properties,
    }
    report_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_optimetrics_solve.json")
    report_path.write_text(json.dumps(solve_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not has_result:
        raise RuntimeError(
            "Optimetrics SolveSetup returned, but AEDT did not report HasResult=true; see {}".format(
                report_path
            )
        )
    LOGGER.info("Optimetrics solve completed: setup=%s, variation_count=%d", setup_name, variation_count)
    return solve_report


def _hfss_item(rule_id, level, message, objects=None, path="", suggestion=""):
    """Create one source-tagged structured result without GUI dependencies."""

    return {
        "rule_id": rule_id, "level": level, "category": "HFSS实际工程",
        "message": str(message), "object_keys": list(objects or []),
        "parameter_path": path, "suggestion": suggestion, "source": "hfss",
        "auto_fix_id": "",
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "config_revision": CONFIG_REVISION,
    }


def write_hfss_validation_report(items, raw_log="", incomplete=False):
    """Persist JSON/TXT reports even when project validation fails."""

    counts = {
        "passed": sum(item["level"] == "info" for item in items),
        "errors": sum(item["level"] == "error" for item in items),
        "warnings": sum(item["level"] == "warning" for item in items),
        "not_checked": sum(item["level"] == "not_checked" for item in items),
    }
    state = "incomplete" if incomplete else (
        "failed" if counts["errors"] else "passed_with_warnings" if counts["warnings"] else "passed"
    )
    payload = {
        "schema_version": 2, "mode": "HFSS工程检查", "config_revision": CONFIG_REVISION,
        "generation_id": GENERATION_ID, "config_hash": CONFIG_HASH, "geometry_hash": GEOMETRY_HASH,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "state": state, "counts": counts, "stale": False, "incomplete": bool(incomplete),
        "items": items, "raw_log": str(raw_log), "audit": list(AUDIT_LOG),
    }
    HFSS_VALIDATE_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "HFSS工程Validate报告", "状态：" + state,
        "配置版本：" + CONFIG_REVISION,
        "generation_id：" + GENERATION_ID,
        "config_hash：" + CONFIG_HASH,
        "geometry_hash：" + GEOMETRY_HASH,
        "通过只表示具备求解条件，不保证收敛或结果准确。", "",
    ]
    for item in items:
        lines.append("[{level}] {rule_id} {message}".format(**item))
        if item.get("suggestion"):
            lines.append("  建议：" + item["suggestion"])
    if raw_log:
        lines.extend(["", "HFSS原始Validate日志：", str(raw_log)])
    HFSS_VALIDATE_TXT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return payload


def _bbox_mm(app, name):
    """Read one actual HFSS bounding box and normalize it back to true mm."""

    values = [float(value) for value in app.modeler[name].bounding_box]
    factor = 1.0 if UNITS == "mm" else 0.0254
    return [value * factor for value in values]


def _expected_hfss_bounds():
    """Build the comparison manifest only from the embedded shared geometry."""

    result = {}
    scenario_type = str(CONFIG.get("scenario", {}).get("type", "surface_capacitor"))
    if scenario_type == "via_transition":
        layers = {str(layer["name"]): layer for layer in GEOMETRY.get("stackup", [])}
        traces = list(GEOMETRY.get("entry_traces", [])) + list(GEOMETRY.get("exit_traces", []))
        for signal in ("P", "N"):
            bounds = []
            for trace in traces:
                if str(trace.get("signal")) != signal:
                    continue
                points = list(trace.get("points", []))
                if points:
                    bounds.append([
                        min(float(point[0]) for point in points),
                        min(float(point[1]) for point in points),
                        float(trace["z_bottom"]),
                        max(float(point[0]) for point in points),
                        max(float(point[1]) for point in points),
                        float(trace["z_top"]),
                    ])
            via = next((item for item in GEOMETRY.get("signal_vias", [])
                        if str(item.get("signal")) == signal), None)
            if via is not None:
                barrel_radius = float(via["drill_diameter"]) / 2.0
                bounds.append([
                    float(via["x"]) - barrel_radius,
                    float(via["y"]) - barrel_radius,
                    float(via.get("effective_barrel_z_bottom", via["z_bottom"])),
                    float(via["x"]) + barrel_radius,
                    float(via["y"]) + barrel_radius,
                    float(via.get("effective_barrel_z_top", via["z_top"])),
                ])
                pad_radius = float(via["connected_pad_diameter"]) / 2.0
                for layer_name in via.get("functional_pad_layers", []):
                    layer = layers[str(layer_name)]
                    bounds.append([
                        float(via["x"]) - pad_radius,
                        float(via["y"]) - pad_radius,
                        float(layer["z_bottom"]),
                        float(via["x"]) + pad_radius,
                        float(via["y"]) + pad_radius,
                        float(layer["z_top"]),
                    ])
            if bounds:
                result["SIGNAL_" + signal] = [
                    min(item[0] for item in bounds), min(item[1] for item in bounds),
                    min(item[2] for item in bounds), max(item[3] for item in bounds),
                    max(item[4] for item in bounds), max(item[5] for item in bounds),
                ]
    else:
        for group_name, shapes in GEOMETRY.get("conductor_groups", {}).items():
            result[group_name] = [
                min(shape["bounds"]["x_min"] for shape in shapes),
                min(shape["bounds"]["y_min"] for shape in shapes),
                min(shape["z_bottom"] for shape in shapes),
                max(shape["bounds"]["x_max"] for shape in shapes),
                max(shape["bounds"]["y_max"] for shape in shapes),
                max(shape["z_bottom"] + shape["thickness"] for shape in shapes),
            ]
    plane = GEOMETRY["plane_bounds"]
    for layer in GEOMETRY["stackup"]:
        if layer["layer_type"] == "plane" and layer.get("is_reference_plane"):
            result[layer["name"]] = [plane["x_min"], plane["y_min"], layer["z_bottom"],
                                      plane["x_max"], plane["y_max"], layer["z_top"]]
    for via in GEOMETRY.get("gnd_vias", []):
        radius = via["drill_diameter"] / 2.0
        result[via["name"] + "_BARREL"] = [via["x"]-radius, via["y"]-radius, via["z_bottom"],
                                             via["x"]+radius, via["y"]+radius, via["z_top"]]
    for body in GEOMETRY.get("capacitor_bodies", []):
        points = body.get("points", [])
        if points:
            result[body["name"]] = [
                min(point[0] for point in points), min(point[1] for point in points), body["z_bottom"],
                max(point[0] for point in points), max(point[1] for point in points), body["z_top"],
            ]
    for shape in GEOMETRY.get("capacitor_model_shapes", {}).values():
        points = shape.get("points", [])
        if points:
            result[shape["name"]] = [
                min(point[0] for point in points), min(point[1] for point in points), shape["z_bottom"],
                max(point[0] for point in points), max(point[1] for point in points), shape["z_bottom"],
            ]
    if GEOMETRY.get("surface_ground"):
        ground = GEOMETRY["surface_ground"]
        bounds = ground["bounds"]
        result[ground["name"]] = [
            bounds["x_min"], bounds["y_min"], ground["z_bottom"],
            bounds["x_max"], bounds["y_max"], ground["z_top"],
        ]
    if GEOMETRY.get("airbox"):
        air = GEOMETRY["airbox"]
        result[air["name"]] = [air["x_min"], air["y_min"], air["z_bottom"],
                                air["x_max"], air["y_max"], air["z_top"]]
    for port in GEOMETRY.get("ports", []):
        result[port["sheet_name"]] = [
            port["x"], port["y_min"], port["z_min"],
            port["x"], port["y_max"], port["z_max"],
        ]
    return result


def _object_metrics(app, name):
    """Read material, bounding box, volume and best available connectivity."""

    obj = app.modeler[name]
    volume = getattr(obj, "volume", None)
    if callable(volume):
        volume = volume()
    if volume is None:
        volume = getattr(obj, "volume_value", None)
    connected = None
    for method_name in ("is_connected", "is_single_solid", "check_connectivity"):
        method = getattr(obj, method_name, None)
        if callable(method):
            connected = bool(method())
            break
    if connected is None:
        # AEDT 2022 R2/PyAEDT 1.3 does not expose an Object3d connectivity
        # query.  A successful native Unite with the exact expected object
        # count reduction, a surviving final object, and no surviving source
        # fragments is an equivalent hard check for this generated topology.
        for event in reversed(AUDIT_LOG):
            if event.get("stage") != "unite" or event.get("requested_name") != name:
                continue
            inputs = list(event.get("inputs") or [])
            expected_after = int(event.get("object_count_before", 0)) - max(0, len(inputs) - 1)
            fragments = [source for source in inputs if source != name and object_exists(app, source)]
            connected = bool(
                not event.get("exception")
                and int(event.get("object_count_after", -1)) == expected_after
                and object_exists(app, name)
                and not fragments
            )
            break
    return {
        "name": name,
        "material": str(getattr(obj, "material_name", "") or ""),
        "bounding_box": _bbox_mm(app, name),
        "volume": None if volume is None else float(volume),
        "connected": connected,
    }


def validate_created_objects(app, state):
    """Validate actual HFSS objects and invoke the native AEDT design checker."""

    items = []
    scenario_type = str(CONFIG.get("scenario", {}).get("type", "surface_capacitor"))
    conductor_names = (["SIGNAL_P", "SIGNAL_N"] if scenario_type == "via_transition"
                       else list(GEOMETRY.get("conductor_groups", {})))
    expected = list(conductor_names)
    expected.extend(state["dielectric_objects"])
    expected.extend(state["plane_objects"].values())
    expected.extend(state["body_objects"])
    # VIA pads are united into their connected reference planes, so only the
    # surviving barrel objects remain independently addressable afterwards.
    expected.extend(name for name in state["via_objects"] if "_PAD_" not in str(name))
    if state.get("top_ground_object"):
        expected.append(state["top_ground_object"])
    expected.extend(state.get("capacitor_model_objects", []))
    expected.extend(state.get("port_sheets", []))
    if state.get("airbox"):
        expected.append(state["airbox"])
    names = list(model_object_names(app))
    state["object_metrics"] = {}
    for name in expected:
        if object_exists(app, name):
            try:
                state["object_metrics"][name] = _object_metrics(app, name)
            except Exception as exc:
                items.append(_hfss_item("HFSS-020", "error", "无法读取对象实际属性：{}：{}".format(name, exc), (name,)))
    missing = [name for name in expected if not object_exists(app, name)]
    if missing:
        items.append(_hfss_item(
            "HFSS-001", "error", "建模完成后缺少关键对象：{}".format(", ".join(missing)),
            missing, suggestion="检查对象创建、Unite/Subtract返回值并保留失败工程。",
        ))
    else:
        items.append(_hfss_item("HFSS-001", "info", "预期信号、叠层、平面、电容和VIA对象均存在。", expected))
    if len(names) != len(set(names)):
        items.append(_hfss_item("HFSS-002", "error", "HFSS实际对象名称不唯一。", suggestion="检查对象命名注册表。"))
    else:
        items.append(_hfss_item("HFSS-002", "info", "HFSS实际对象名称唯一。"))

    expected_openings = len(GEOMETRY.get("soldermask_openings", []))
    if int(state.get("soldermask_openings_created", 0)) != expected_openings:
        items.append(_hfss_item(
            "HFSS-021", "error", "表层阻焊开窗执行数量与统一几何快照不一致。",
            suggestion="停止求解；检查安装面阻焊层和Subtract结果。",
        ))
    else:
        items.append(_hfss_item(
            "HFSS-021", "info", "所选安装面的阻焊开窗已按统一几何逐一执行。",
        ))
    opposite_prefix = "BOTTOM_GROUND" if str(GEOMETRY.get("mount_side")) == "top" else "TOP_GROUND"
    opposite_objects = [name for name in names if str(name).startswith(opposite_prefix)]
    if opposite_objects:
        items.append(_hfss_item(
            "HFSS-022", "error", "检测到另一安装面的遗留对象：" + ", ".join(opposite_objects),
            opposite_objects, "capacitor.mount_side",
            "清理旧对象并从当前统一快照重新生成。",
        ))
    else:
        items.append(_hfss_item("HFSS-022", "info", "未检测到另一安装面的遗留GND对象。"))

    # Boundary sheets do not have a bulk material after HFSS assigns a Lumped
    # RLC or AutoIdentify port.  AEDT 2022 R2 therefore returns an empty
    # ``material_name`` for these valid objects.  Check material strictly for
    # solids and verify sheets through their boundary/terminal checks below.
    boundary_sheets = set(state.get("capacitor_model_objects", []))
    boundary_sheets.update(state.get("port_sheets", []))
    material_failures = []
    for name in expected:
        if not object_exists(app, name):
            continue
        if name in boundary_sheets:
            continue
        try:
            assigned = str(getattr(app.modeler[name], "material_name", "") or "").strip()
            if not assigned:
                material_failures.append(name)
        except Exception:
            material_failures.append(name)
    if material_failures:
        items.append(_hfss_item("HFSS-003", "error", "对象缺少可识别的材料分配：{}".format(", ".join(material_failures)),
                                material_failures, suggestion="检查材料创建和对象material_name。"))
    else:
        items.append(_hfss_item("HFSS-003", "info", "关键HFSS对象均具有材料分配。"))
    if boundary_sheets:
        items.append(_hfss_item(
            "HFSS-003S", "info",
            "边界Sheet不使用体材料；其有效性由RLC/Terminal边界和实际边界框检查确认。",
            sorted(boundary_sheets),
        ))

    for signal in conductor_names:
        metrics = state["object_metrics"].get(signal)
        if metrics is None:
            continue
        if metrics.get("volume") is not None and metrics["volume"] <= 0.0:
            items.append(_hfss_item("GEO-010", "error", "{}体积为零，Unite结果不是有效实体。".format(signal), (signal,)))
        elif metrics.get("volume") is None:
            items.append(_hfss_item("GEO-010", "warning", "{}未能从当前PyAEDT对象接口读取体积。".format(signal), (signal,)))
        if metrics.get("connected") is False:
            items.append(_hfss_item("GEO-011", "error", "{}实际对象报告为不连通。".format(signal), (signal,)))
        elif metrics.get("connected") is None:
            items.append(_hfss_item(
                "GEO-011", "warning",
                "{}未能从当前PyAEDT对象接口读取连通性；已完成快照中的正面积重叠预检。".format(signal),
                (signal,), suggestion="在当前AEDT/PyAEDT版本补充Object3d连通性接口后重新Validate。",
            ))
        else:
            items.append(_hfss_item("GEO-011", "info", "{}已合并为单一对象；相邻实体的正面积重叠已在嵌入快照中预检。".format(signal), (signal,)))

    bounds_mismatch = []
    port_bounds_mismatch = []
    for name, expected_bounds in _expected_hfss_bounds().items():
        if not object_exists(app, name):
            continue
        try:
            actual_bounds = _bbox_mm(app, name)
            if any(abs(actual - expected_value) > 1e-4
                   for actual, expected_value in zip(actual_bounds, expected_bounds)):
                target = port_bounds_mismatch if str(name).startswith("PORT_SHEET_") else bounds_mismatch
                target.append("{} expected={} actual={}".format(name, expected_bounds, actual_bounds))
        except Exception as exc:
            target = port_bounds_mismatch if str(name).startswith("PORT_SHEET_") else bounds_mismatch
            target.append("{} 无法读取边界：{}".format(name, exc))
    if bounds_mismatch:
        items.append(_hfss_item(
            "HFSS-010", "error", "3D预览清单与HFSS实际对象位置/尺寸不一致：" + "; ".join(bounds_mismatch),
            suggestion="停止求解，检查单位换算、布尔操作和统一几何数据源。",
        ))
    else:
        items.append(_hfss_item("HFSS-010", "info", "共享几何清单与HFSS实际对象边界一致。"))
    if port_bounds_mismatch:
        items.append(_hfss_item(
            "PORT-011", "error", "预览PortGeometry与HFSS实际Port Sheet不一致：" + "; ".join(port_bounds_mismatch),
            suggestion="停止求解；Port Sheet必须直接复用嵌入的统一PortGeometry。",
        ))
    else:
        items.append(_hfss_item("PORT-011", "info", "四个HFSS Port Sheet与预览PortGeometry边界一致。"))

    remaining_tools = []
    for names_in_layer in state["antipad_tools"].values():
        remaining_tools.extend(name for name in names_in_layer if object_exists(app, name))
    if remaining_tools:
        items.append(_hfss_item("HFSS-004", "error", "Subtract后仍存在反焊盘工具体：{}".format(", ".join(remaining_tools)),
                                remaining_tools, suggestion="检查参考平面Subtract操作。"))
    else:
        items.append(_hfss_item("HFSS-004", "info", "反焊盘Subtract成功，工具体已删除。"))
    remaining_via_tools = []
    for names_in_layer in state["via_antipad_tools"].values():
        remaining_via_tools.extend(name for name in names_in_layer if object_exists(app, name))
    if remaining_via_tools:
        items.append(_hfss_item("HFSS-005", "error", "Subtract后仍存在VIA避空工具体：{}".format(", ".join(remaining_via_tools)),
                                remaining_via_tools, suggestion="检查非连接参考层VIA避空Subtract。"))
    else:
        items.append(_hfss_item("HFSS-005", "info", "VIA避空Subtract成功，工具体已删除。"))

    sim = CONFIG.get("simulation", {})
    setups = list(getattr(app, "setup_names", []) or [])
    expected_setup = str(sim.get("setup_name", "Setup1"))
    if sim.get("enabled"):
        if expected_setup not in setups:
            items.append(_hfss_item("HFSS-006", "error", "实际HFSS工程缺少Setup：" + expected_setup,
                                    path="simulation.setup_name", suggestion="检查create_setup返回值。"))
        else:
            items.append(_hfss_item("HFSS-006", "info", "Setup已创建：" + expected_setup))
        try:
            sweeps = list(app.oanalysis.GetSweeps(expected_setup) or [])
        except Exception:
            sweeps = []
        expected_sweep = str(sim.get("sweep_name", "Sweep1"))
        if expected_sweep not in sweeps:
            items.append(_hfss_item("HFSS-007", "error", "实际HFSS工程缺少Sweep：" + expected_sweep,
                                    path="simulation.sweep_name", suggestion="检查Sweep创建和update返回值。"))
        else:
            items.append(_hfss_item("HFSS-007", "info", "Sweep已创建：" + expected_sweep))
    boundaries = list(getattr(app, "boundaries", []) or [])
    boundary_names = [str(getattr(boundary, "name", boundary)) for boundary in boundaries]
    mode = str(CONFIG.get("capacitor_model", {}).get("mode", "ideal_bridge"))
    actual_names = set(names)
    bridge_names = {"CAP_P_BRIDGE", "CAP_N_BRIDGE"}
    rlc_names = {"CAP_RLC_P", "CAP_RLC_N"}
    model_sheets = {"CAP_RLC_P_SHEET", "CAP_RLC_N_SHEET",
                    "CAP_SPARAM_P_SHEET", "CAP_SPARAM_N_SHEET"}
    if scenario_type == "surface_capacitor" and mode == "ideal_bridge":
        expected_final = {"SIGNAL_P", "SIGNAL_N"}
        # The two bridge solids are deliberately consumed by the final Unite;
        # seeing CAP_*_BRIDGE as survivors would mean the ideal connection was
        # not incorporated into the signal conductor.
        if (not expected_final.issubset(actual_names)
                or bridge_names.intersection(actual_names)
                or rlc_names.intersection(boundary_names)):
            items.append(_hfss_item("CAP-101", "error", "理想短路对象/边界拓扑错误。",
                                    sorted(expected_final), "capacitor_model.mode"))
        else:
            items.append(_hfss_item("CAP-101", "info", "两座PAD铜桥已并入P/N信号导体，且无RLC边界。",
                                    sorted(expected_final)))
    elif scenario_type == "surface_capacitor":
        bypasses = sorted(bridge_names.intersection(actual_names))
        if bypasses:
            items.append(_hfss_item("CAP-102", "error", "非理想模式检测到铜桥旁路：" + ", ".join(bypasses),
                                    bypasses, "capacitor_model.mode"))
        else:
            items.append(_hfss_item("CAP-102", "info", "非理想模式PAD间不存在铜桥旁路。"))
        expected_conductors = {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}
        if not expected_conductors.issubset(actual_names):
            items.append(_hfss_item("CAP-103", "error", "四个端部导体未保持独立。",
                                    sorted(expected_conductors), "capacitor_model.mode"))
        else:
            items.append(_hfss_item("CAP-103", "info", "P/N输入输出四个端部导体保持独立。",
                                    sorted(expected_conductors)))
        if mode == "lumped_rlc":
            missing_rlc = sorted(rlc_names.difference(boundary_names))
            missing_sheets = sorted({"CAP_RLC_P_SHEET", "CAP_RLC_N_SHEET"}.difference(actual_names))
            if missing_rlc or missing_sheets:
                items.append(_hfss_item("CAP-104", "error", "RLC Sheet或边界缺失：" + ", ".join(missing_sheets + missing_rlc),
                                        missing_sheets + missing_rlc, "capacitor_model.mode"))
            else:
                items.append(_hfss_item("CAP-104", "info", "P/N RLC Sheet及串联RLC边界均存在。",
                                        sorted(rlc_names | {"CAP_RLC_P_SHEET", "CAP_RLC_N_SHEET"})))
        elif mode == "sparameter":
            expected_touchstone = {"CAP_SPARAM_P", "CAP_SPARAM_N"}
            actual_touchstone = set(state.get("sparameter_components", []))
            missing_touchstone = sorted(expected_touchstone.difference(actual_touchstone))
            missing_sheets = sorted({"CAP_SPARAM_P_SHEET", "CAP_SPARAM_N_SHEET"}.difference(actual_names))
            if missing_touchstone or missing_sheets:
                items.append(_hfss_item(
                    "CAP-106", "error",
                    "Touchstone组件或连接Sheet缺失：" + ", ".join(missing_sheets + missing_touchstone),
                    missing_sheets + missing_touchstone, "capacitor_model.mode",
                    "检查create_touchstone_component返回值及其Sheet assignment。",
                ))
            else:
                items.append(_hfss_item(
                    "CAP-106", "info", "P/N Touchstone组件及其连接Sheet均已由指定assignment创建。",
                    sorted(expected_touchstone | {"CAP_SPARAM_P_SHEET", "CAP_SPARAM_N_SHEET"}),
                ))
        elif mode == "open" and (model_sheets.intersection(actual_names) or rlc_names.intersection(boundary_names)):
            items.append(_hfss_item("CAP-105", "error", "open模式存在不应创建的电气模型。",
                                    sorted(model_sheets.intersection(actual_names)), "capacitor_model.mode"))
    if sim.get("enabled"):
        if sim.get("create_differential_ports"):
            expected_ports = {"PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"}
            missing_ports = sorted(expected_ports.difference(boundary_names))
            if missing_ports:
                items.append(_hfss_item("PORT-012", "error", "实际HFSS工程缺少端口：" + ", ".join(missing_ports),
                                        missing_ports, "simulation.create_differential_ports", "检查端面、参考导体和wave_port返回值。"))
            else:
                items.append(_hfss_item("PORT-012", "info", "四个P/N输入输出端口均已创建。", sorted(expected_ports)))
            if not state.get("differential_pairs_created"):
                items.append(_hfss_item(
                    "PORT-013", "error", "输入或输出差分对设置失败。",
                    sorted(expected_ports), "simulation.create_differential_ports",
                    "检查PORT_IN/OUT的P/N命名及set_differential_pair返回值。",
                ))
            else:
                items.append(_hfss_item("PORT-013", "info", "输入和输出P/N差分对均已设置。",
                                        sorted(expected_ports)))
        if sim.get("create_airbox") and not state.get("airbox"):
            items.append(_hfss_item("HFSS-009", "error", "请求空气盒但实际HFSS对象未登记。",
                                    path="simulation.create_airbox", suggestion="检查空气盒和开放边界创建。"))
        if sim.get("create_airbox"):
            requested_boundary = str(sim.get("open_boundary", "Radiation"))
            boundary_ok = (
                bool(state.get("open_boundary")) and (
                    state.get("open_boundary") in boundary_names
                    or any(str(state.get("open_boundary")).lower() in name.lower() for name in boundary_names)
                )
                if requested_boundary != "PML"
                else any("pml" in name.lower() for name in boundary_names)
            )
            if not boundary_ok:
                items.append(_hfss_item(
                    "HFSS-011", "error",
                    "空气盒存在，但未找到请求的开放边界：" + requested_boundary,
                    (state.get("airbox"),), "simulation.open_boundary",
                    "检查Radiation/PML创建返回值和实际Boundary列表。",
                ))
            else:
                items.append(_hfss_item(
                    "HFSS-011", "info",
                    "实际HFSS开放边界已创建：" + requested_boundary,
                    (state.get("airbox"),),
                ))
    elif setups:
        items.append(_hfss_item("HFSS-006", "error", "仿真关闭时不应创建Setup：" + ", ".join(setups)))

    native_audit = state.get("native_sweep_audit", {})
    if NATIVE_SWEEP_RUNTIME:
        if native_audit.get("geometry_binding_verified"):
            items.append(_hfss_item(
                "OPT-001", "info",
                "实际 AEDT Design Variable、Optimetrics Setup 与反焊盘几何变化链已验证。",
                tuple(state.get("plane_objects", {}).values()),
                "sweeps.antipad.global_expansion",
            ))
        else:
            items.append(_hfss_item(
                "OPT-001", "error",
                "未能证明 Optimetrics 变量实际驱动 HFSS 反焊盘几何变化。",
                suggestion="检查派生变量、Polyline/ThickenSheet历史及PLANE Subtract体积变化。",
            ))

    raw_lines = []
    if not sim.get("enabled"):
        items.append(_hfss_item(
            "HFSS-090", "not_checked",
            "自动仿真已关闭；未执行要求端口、Setup和Sweep的AEDT原生求解前Validate。",
            suggestion="启用仿真配置后再检查求解条件；当前对象完整性检查仍然有效。",
        ))
        payload = write_hfss_validation_report(items, "")
        if payload["counts"]["errors"]:
            raise RuntimeError("HFSS对象完整性检查失败，已保存结构化报告。")
        LOGGER.info("HFSS对象完整性检查通过；仿真关闭，原生求解前Validate未执行。")
        return True
    try:
        # PyAEDT 1.3 validate_full_design() crashes on AEDT 2022 R2 when
        # GetMessages() contains None.  Call the documented native
        # oDesign.ValidateDesign API directly, keep its return value strict,
        # and sanitize the optional Desktop messages ourselves.
        runtime_validate_log = AEDT_PROJECT_PATH.with_name(PROJECT_NAME + "_native_validate.log")
        native_return = app._odesign.ValidateDesign(str(runtime_validate_log))
        native_ok = native_return is True or str(native_return).strip().lower() in {"1", "true"}
        raw_lines = ["ValidateDesign returned {}".format(native_return)]
        if runtime_validate_log.is_file():
            native_text = runtime_validate_log.read_text(encoding="utf-8", errors="replace")
            raw_lines.extend(line for line in native_text.splitlines() if line is not None)
            try:
                requested_native_log = SCRIPT_DIRECTORY / (PROJECT_NAME + "_native_validate.log")
                requested_native_log.write_text(native_text, encoding="utf-8")
            except Exception as exc:
                LOGGER.warning("无法复制原生Validate日志到输出目录：%s", exc)
        try:
            messages = app._desktop.GetMessages(PROJECT_NAME, DESIGN_NAME, 0) or []
            raw_lines.extend(
                "Desktop: " + str(message).strip()
                for message in messages
                if message is not None and str(message).strip()
            )
        except Exception as exc:
            LOGGER.warning("读取AEDT Desktop Validate消息失败；原生Validate返回值仍有效：%s", exc)
        if native_ok:
            items.append(_hfss_item("HFSS-090", "info", "AEDT原生设计Validate通过。"))
        else:
            items.append(_hfss_item("HFSS-090", "error", "AEDT原生设计Validate失败。",
                                    suggestion="查看原生Validate日志并修正对象、端口、边界或Setup。"))
        for line in raw_lines:
            lower = str(line).lower()
            if "error" in lower or "failed" in lower:
                items.append(_hfss_item("HFSS-091", "error", str(line), suggestion="查看HFSS原始Validate上下文。"))
            elif "warning" in lower:
                items.append(_hfss_item("HFSS-092", "warning", str(line), suggestion="复核HFSS原生警告。"))
    except Exception as exc:
        items.append(_hfss_item("HFSS-093", "error", "调用AEDT原生Validate失败：{}".format(exc),
                                suggestion="保留工程并在当前AEDT版本手动执行Validate。"))
        raw_lines.append(traceback.format_exc())

    payload = write_hfss_validation_report(items, "\n".join(map(str, raw_lines)))
    if payload["counts"]["errors"]:
        raise RuntimeError("HFSS工程Validate失败，已保存结构化报告和工程诊断信息。")
    LOGGER.info("HFSS工程Validate通过：具备求解条件；不保证收敛或结果准确。")
    return True


def save_aedt_project(app):
    """保存当前AEDT工程到HFSS可处理的纯英文运行路径。"""

    return save_project_compat(app, AEDT_PROJECT_PATH)


def _publish_project_copy(source_path, requested_path, status):
    """Copy an AEDT file back to the requested output and write a locator."""

    source_path = Path(source_path)
    requested_path = Path(requested_path)
    published_path = source_path
    copy_error = ""
    if source_path != requested_path:
        try:
            requested_path.parent.mkdir(parents=True, exist_ok=True)
            if requested_path.exists():
                if not OVERWRITE_EXISTING:
                    raise FileExistsError("目标AEDT工程已存在且禁止覆盖：{}".format(requested_path))
                requested_path.unlink()
            shutil.copy2(source_path, requested_path)
            published_path = requested_path
            LOGGER.info("AEDT工程已从英文运行路径复制到请求目录：%s", requested_path)
        except Exception as exc:
            copy_error = "{}: {}".format(type(exc).__name__, exc)
            LOGGER.warning("无法把AEDT工程复制到请求目录；请使用英文运行路径工程：%s", source_path)
    payload = {
        "generation_id": GENERATION_ID,
        "config_hash": CONFIG_HASH,
        "geometry_hash": GEOMETRY_HASH,
        "status": str(status),
        "requested_path": str(requested_path),
        "runtime_ascii_path": str(source_path),
        "published_path": str(published_path),
        "copy_error": copy_error,
    }
    AEDT_LOCATION_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return published_path


def clear_previous_hfss_validation_reports():
    """Prevent the GUI from importing a previous run's HFSS result."""

    for path in (HFSS_VALIDATE_JSON_PATH, HFSS_VALIDATE_TXT_PATH):
        try:
            if path.exists():
                path.unlink()
                LOGGER.info("已清理上一次HFSS检查报告：%s", path.name)
        except OSError as exc:
            raise RuntimeError("无法清理旧HFSS检查报告 {}：{}".format(path, exc)) from exc


def _new_state():
    """创建一次建模运行使用的对象登记表。"""

    return {
        "dielectric_objects": [],
        "plane_objects": {},
        "signal_objects": {"P": [], "N": []},
        "united_signals": {},
        "antipad_tools": {},
        "via_antipad_tools": {},
        "backdrill_cavities": [],
        "body_objects": [],
        "soldermask_openings_created": 0,
        "via_objects": [],
        "capacitor_model_objects": [],
        "sparameter_components": [],
        "port_sheets": [],
        "port_boundaries": {},
        "port_terminals": {},
        "differential_pairs_created": False,
        "top_ground_object": None,
        "airbox": None,
        "open_boundary": None,
        "object_metrics": {},
    }


def validate_embedded_data():
    """在不启动AEDT时检查脚本嵌入数据的基本完整性。"""

    _verify_snapshot_integrity()
    errors = list(GEOMETRY.get("errors", []))
    scenario_type = str(CONFIG.get("scenario", {}).get("type", "surface_capacitor"))
    if scenario_type == "via_transition":
        _validate_embedded_via_transition(errors)
        if errors:
            raise RuntimeError("嵌入数据检查失败：\n" + "\n".join("  [错误] " + item for item in errors))
        return True
    required_geometry_keys = {
        "stackup",
        "mount_side",
        "surface_normal",
        "signal_layer_name",
        "signal_z_bottom",
        "signal_z_top",
        "component_outward_z",
        "reference_layer_name",
        "soldermask_openings",
        "signal_shapes",
        "conductor_groups",
        "pad_shapes",
        "bridge_shapes",
        "capacitor_model_shapes",
        "plane_bounds",
        "antipads_by_layer",
    }
    missing = sorted(required_geometry_keys.difference(GEOMETRY))
    if missing:
        errors.append("缺少几何字段：" + ", ".join(missing))
    mount_side = str(GEOMETRY.get("mount_side", ""))
    if mount_side not in {"top", "bottom"} or mount_side != str(CONFIG.get("capacitor", {}).get("mount_side", "top")):
        errors.append("安装面配置与统一几何不一致。")
    signal_layer = next((layer for layer in GEOMETRY.get("stackup", [])
                         if str(layer.get("name")) == str(GEOMETRY.get("signal_layer_name"))), None)
    if signal_layer is None or signal_layer.get("layer_type") != "signal":
        errors.append("signal_layer_name 未指向有效外层 signal 铜。")
    else:
        stackup = list(GEOMETRY.get("stackup", []))
        signal_index = stackup.index(signal_layer)
        outside_layers = (stackup[:signal_index] if mount_side == "top"
                          else stackup[signal_index + 1:])
        if any(layer.get("layer_type") != "soldermask" for layer in outside_layers):
            errors.append("signal_layer_name 不是所选安装面的真实外层 signal 铜。")
        if (abs(float(signal_layer.get("z_bottom", 0.0))
                - float(GEOMETRY.get("signal_z_bottom", 0.0))) > 1e-9
                or abs(float(signal_layer.get("z_top", 0.0))
                       - float(GEOMETRY.get("signal_z_top", 0.0))) > 1e-9):
            errors.append("安装面信号层Z范围与统一几何字段不一致。")
        expected_normal = 1.0 if mount_side == "top" else -1.0
        normal = list(GEOMETRY.get("surface_normal", []))
        if len(normal) != 3 or any(abs(float(value) - expected) > 1e-9
                                   for value, expected in zip(normal, (0.0, 0.0, expected_normal))):
            errors.append("surface_normal 与安装面不一致。")
        expected_masks = {str(layer.get("name")) for layer in outside_layers
                          if layer.get("layer_type") == "soldermask"}
        actual_masks = {str(item.get("layer"))
                        for item in GEOMETRY.get("soldermask_openings", [])}
        if actual_masks != expected_masks:
            errors.append("表层阻焊开窗没有严格跟随所选安装面。")
    expected_signal_z = (
        float(GEOMETRY.get("signal_z_bottom", 0.0)),
        float(GEOMETRY.get("signal_z_top", 0.0)),
    )
    for signal in ("P", "N"):
        for shape in GEOMETRY.get("signal_shapes", {}).get(signal, []):
            if (abs(float(shape.get("z_bottom", 0.0)) - expected_signal_z[0]) > 1e-9
                    or abs(float(shape.get("z_bottom", 0.0))
                           + float(shape.get("thickness", 0.0)) - expected_signal_z[1]) > 1e-9):
                errors.append("{}未位于所选外层信号铜。".format(shape.get("name")))
    surface_ground = GEOMETRY.get("surface_ground")
    if surface_ground:
        if (str(surface_ground.get("layer")) != str(GEOMETRY.get("signal_layer_name"))
                or str(surface_ground.get("mount_side")) != mount_side):
            errors.append("安装面GND铜皮仍指向错误表面。")
        expected_prefix = mount_side.upper() + "_"
        if not str(surface_ground.get("name", "")).startswith(expected_prefix):
            errors.append("安装面GND铜皮名称暴露了另一安装面的遗留对象。")
    mode = str(CONFIG.get("capacitor_model", {}).get("mode", "ideal_bridge"))
    expected_groups = (
        {"SIGNAL_P", "SIGNAL_N"}
        if mode == "ideal_bridge"
        else {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}
    )
    actual_groups = set(GEOMETRY.get("conductor_groups", {}))
    if actual_groups != expected_groups:
        errors.append("电容模式{}的导体分组错误：{}。".format(mode, sorted(actual_groups)))
    for group_name, shapes in GEOMETRY.get("conductor_groups", {}).items():
        if not shapes:
            errors.append("{}没有信号对象。".format(group_name))
            continue
        for first, second in zip(shapes, shapes[1:]):
            first_bounds, second_bounds = first["bounds"], second["bounds"]
            overlap_x = min(first_bounds["x_max"], second_bounds["x_max"]) - max(first_bounds["x_min"], second_bounds["x_min"])
            overlap_y = min(first_bounds["y_max"], second_bounds["y_max"]) - max(first_bounds["y_min"], second_bounds["y_min"])
            if overlap_x <= 1e-9 or overlap_y <= 1e-9:
                errors.append("{}相邻对象没有正面积重叠：{} -> {}。".format(group_name, first["name"], second["name"]))
    if len(GEOMETRY.get("pad_shapes", {})) != 4:
        errors.append("焊盘数量不是4。")
    bridge_count = len(GEOMETRY.get("bridge_shapes", {}))
    if (mode == "ideal_bridge" and bridge_count != 2) or (mode != "ideal_bridge" and bridge_count != 0):
        errors.append("{}模式的铜桥数量错误：{}。".format(mode, bridge_count))
    sheet_count = len(GEOMETRY.get("capacitor_model_shapes", {}))
    expected_sheets = 2 if mode in {"lumped_rlc", "sparameter"} else 0
    if sheet_count != expected_sheets:
        errors.append("{}模式的电气Sheet数量错误：{}。".format(mode, sheet_count))
    expected_sheet_z = expected_signal_z[1] if mount_side == "top" else expected_signal_z[0]
    for sheet in GEOMETRY.get("capacitor_model_shapes", {}).values():
        if abs(float(sheet.get("z_bottom", 0.0)) - expected_sheet_z) > 1e-9:
            errors.append("{}电气Sheet没有位于所选铜层外表面。".format(sheet.get("name")))
    if CONFIG.get("gnd_vias", {}).get("enabled"):
        expected_vias = int(CONFIG["gnd_vias"].get("count", 0))
        actual_vias = len(GEOMETRY.get("gnd_vias", []))
        if actual_vias != expected_vias:
            errors.append("GND VIA数量不一致：配置={}，快照={}。".format(expected_vias, actual_vias))
    if CONFIG.get("capacitor", {}).get("create_body_in_hfss"):
        body_names = {str(item.get("name")) for item in GEOMETRY.get("capacitor_bodies", [])}
        if body_names != {"BODY_C_P", "BODY_C_N"}:
            errors.append("电容本体快照不完整：{}。".format(sorted(body_names)))
        for body in GEOMETRY.get("capacitor_bodies", []):
            outward = float(GEOMETRY.get("component_outward_z", 0.0))
            outside = ((mount_side == "top"
                        and abs(float(body.get("z_bottom", 0.0)) - outward) <= 1e-9
                        and float(body.get("z_top", 0.0)) > outward)
                       or (mount_side == "bottom"
                           and abs(float(body.get("z_top", 0.0)) - outward) <= 1e-9
                           and float(body.get("z_bottom", 0.0)) < outward))
            if not outside:
                errors.append("{}电容本体未从所选表面向PCB外侧延伸。".format(body.get("name")))
            if float(body.get("z_top", 0.0)) <= float(body.get("z_bottom", 0.0)):
                errors.append("{}电容本体高度无效。".format(body.get("name")))
    sim = CONFIG.get("simulation", {})
    if sim.get("enabled") and sim.get("create_differential_ports"):
        port_names = {str(item.get("name")) for item in GEOMETRY.get("ports", [])}
        if port_names != {"PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"}:
            errors.append("PortGeometry不完整：{}。".format(sorted(port_names)))
        else:
            signal_shapes = {
                str(shape.get("name")): shape
                for signal in ("P", "N")
                for shape in GEOMETRY.get("signal_shapes", {}).get(signal, [])
            }
            layers = {
                str(layer.get("name")): layer
                for layer in GEOMETRY.get("stackup", [])
            }
            for port in GEOMETRY.get("ports", []):
                port_name = str(port.get("name"))
                source_name = str(port.get("source_shape", ""))
                source = signal_shapes.get(source_name)
                reference = layers.get(str(port.get("reference_layer", "")))
                if source is None:
                    errors.append("{}找不到PortGeometry.source_shape={}".format(port_name, source_name))
                    continue
                if reference is None:
                    errors.append("{}找不到PortGeometry.reference_layer={}".format(
                        port_name, port.get("reference_layer")))
                    continue
                bounds = source["bounds"]
                expected_x = bounds["x_min"] if str(port.get("side")) == "IN" else bounds["x_max"]
                if abs(float(port.get("x", 0.0)) - float(expected_x)) > 1e-9:
                    errors.append("{}没有位于走线端面：Port x={}，端面 x={}".format(
                        port_name, port.get("x"), expected_x))
                overlap_y = min(float(port.get("y_max", 0.0)), float(bounds["y_max"])) - max(
                    float(port.get("y_min", 0.0)), float(bounds["y_min"]))
                if overlap_y <= 1e-9:
                    errors.append("{}端口面与{}在Y方向没有有效重叠".format(port_name, source_name))
                expected_z_min = (float(reference["z_bottom"]) if mount_side == "top"
                                  else float(GEOMETRY.get("trace_z_bottom", 0.0)))
                expected_z_max = (float(GEOMETRY.get("trace_z_top", 0.0)) if mount_side == "top"
                                  else float(reference["z_top"]))
                if abs(float(port.get("z_min", 0.0)) - expected_z_min) > 1e-9:
                    errors.append("{}端口面Z下界错误".format(port_name))
                if abs(float(port.get("z_max", 0.0)) - expected_z_max) > 1e-9:
                    errors.append("{}端口面Z上界错误".format(port_name))
                line_start = port.get("integration_line_start", [])
                line_end = port.get("integration_line_end", [])
                if len(line_start) != 3 or len(line_end) != 3:
                    errors.append("{}积分线端点不完整".format(port_name))
                else:
                    if abs(float(line_start[0]) - float(port.get("x", 0.0))) > 1e-9 or \
                            abs(float(line_end[0]) - float(port.get("x", 0.0))) > 1e-9:
                        errors.append("{}积分线没有位于端口面".format(port_name))
                    expected_reference_z = (float(reference["z_top"]) if mount_side == "top"
                                            else float(reference["z_bottom"]))
                    if abs(float(line_end[2]) - expected_reference_z) > 1e-9:
                        errors.append("{}积分线终点没有指向参考层朝向信号的表面".format(port_name))
    if sim.get("enabled") and sim.get("create_airbox") and not GEOMETRY.get("airbox"):
        errors.append("配置要求AIRBOX，但几何快照中不存在AIRBOX。")
    if GEOMETRY.get("airbox"):
        airbox = GEOMETRY["airbox"]
        for body in GEOMETRY.get("capacitor_bodies", []):
            if (float(airbox.get("z_bottom", 0.0)) > float(body.get("z_bottom", 0.0))
                    or float(airbox.get("z_top", 0.0)) < float(body.get("z_top", 0.0))):
                errors.append("AIRBOX没有覆盖{}电容本体。".format(body.get("name")))
    if errors:
        raise RuntimeError("嵌入几何检查失败：\n" + "\n".join(errors))
    LOGGER.info("嵌入配置和几何数据检查通过。")
    return True


def _validate_embedded_via_transition(errors):
    """via_transition 嵌入几何的结构断言。"""
    signal_vias = list(GEOMETRY.get("signal_vias", []))
    if len(signal_vias) != 2:
        errors.append("信号孔数量不是2（P/N）。")
    for via in signal_vias:
        if str(via.get("signal")) not in {"P", "N"}:
            errors.append("信号孔缺少 P/N 标识。")
        if str(via.get("entry_layer")) == str(via.get("exit_layer")):
            errors.append("信号孔 entry/exit 层相同。")
        if not via.get("functional_pad_layers"):
            errors.append("信号孔缺少功能焊盘层。")
        backdrilled = bool(via.get("backdrilled"))
        has_stub = (str(via.get("entry_layer")) != str(via.get("drill_start_layer"))
                    or str(via.get("exit_layer")) != str(via.get("drill_end_layer")))
        if backdrilled:
            bd_diameter = float(via.get("backdrill_diameter", 0.0))
            if bd_diameter <= float(via.get("drill_diameter", 0.0)):
                errors.append("背钻孔径未大于原始钻孔。")
            if abs(float(via.get("actual_residual_stub", 0.0))
                   - float(via.get("target_residual_stub", 0.0))) > 1e-9:
                errors.append("实际残桩与目标残桩不一致。")
            if (float(via.get("effective_barrel_z_bottom", 0.0)) < float(via.get("z_bottom", 0.0)) - 1e-9
                    or float(via.get("effective_barrel_z_top", 0.0)) > float(via.get("z_top", 0.0)) + 1e-9):
                errors.append("有效孔桶范围超出原始孔桶范围。")
        elif has_stub:
            errors.append("存在未背钻残桩（内层退出未启用背钻）。")
    ports = list(GEOMETRY.get("ports", []))
    if len(ports) != 4:
        errors.append("端口数量不是4。")
    expected = {"PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"}
    actual = {str(port.get("name")) for port in ports}
    if actual != expected:
        errors.append("端口名称不完整：{}。".format(sorted(actual)))
    if not GEOMETRY.get("stackup"):
        errors.append("缺少叠层。")
    if not GEOMETRY.get("plane_bounds"):
        errors.append("缺少平面边界。")
    contours = list(GEOMETRY.get("antipad_contours", []))
    antipad_layers = {
        str(layer_name)
        for via in signal_vias
        for layer_name in via.get("antipad_layers", [])
    }
    per_layer_antipad = (CONFIG.get("via_transition", {}).get("padstack", {})
                         .get("antipad", {}).get("per_layer", {}))
    antipad_layers = {
        layer_name for layer_name in antipad_layers
        if not isinstance(per_layer_antipad.get(layer_name), dict)
        or bool(per_layer_antipad[layer_name].get("enabled", True))
    }
    contours_by_layer = {}
    for contour in contours:
        layer_name = str(contour.get("layer", ""))
        contours_by_layer.setdefault(layer_name, []).append(contour)
        topology = str(contour.get("topology", ""))
        shape = str(contour.get("shape", ""))
        owner = str(contour.get("owner", ""))
        points = list(contour.get("points", []))
        if layer_name not in antipad_layers:
            errors.append("反焊盘轮廓位于未声明的避空层：{}。".format(layer_name))
        if topology not in {"independent", "merged_pair"}:
            errors.append("反焊盘轮廓拓扑无效：{}。".format(topology))
        if shape not in {"circle", "ellipse", "capsule", "rounded_rectangle"}:
            errors.append("反焊盘轮廓形状无效：{}。".format(shape))
        if len(points) < 4 or any(not isinstance(point, (list, tuple)) or len(point) != 2
                                  for point in points):
            errors.append("反焊盘轮廓点集不完整：{}。".format(layer_name))
        if topology == "independent" and owner not in {"P", "N"}:
            errors.append("独立反焊盘缺少 P/N owner：{}。".format(layer_name))
        if topology == "merged_pair" and owner != "merged_pair":
            errors.append("合并反焊盘 owner 不一致：{}。".format(layer_name))
    for layer_name in antipad_layers:
        layer_contours = contours_by_layer.get(layer_name, [])
        if not layer_contours:
            errors.append("避空层缺少权威反焊盘轮廓：{}。".format(layer_name))
            continue
        topology = str(layer_contours[0].get("topology", ""))
        expected_count = 2 if topology == "independent" else 1
        if len(layer_contours) != expected_count:
            errors.append("反焊盘轮廓数量与拓扑不一致：{} 期望{}个，实际{}个。".format(
                layer_name, expected_count, len(layer_contours)))


def _create_via_transition_traces(app, state):
    for trace in list(GEOMETRY.get("entry_traces", [])) + list(GEOMETRY.get("exit_traces", [])):
        points = [[float(point[0]), float(point[1])] for point in trace.get("points", [])]
        if not points:
            continue
        solid = create_polygon_solid_compat(
            app, points,
            length(float(trace["z_bottom"])),
            length(float(trace["z_top"]) - float(trace["z_bottom"])),
            str(trace["name"]), str(trace["material"]),
        )
        state["signal_objects"].setdefault(str(trace["signal"]), []).append(solid)


def _create_signal_vias(app, state):
    for via in GEOMETRY.get("signal_vias", []):
        name = str(via["name"])
        x, y = length(float(via["x"])), length(float(via["y"]))
        # Phase 3：背钻后的有效孔桶范围（未背钻时等于原始范围）。
        z_bottom_value = float(via.get("effective_barrel_z_bottom", via["z_bottom"]))
        z_top_value = float(via.get("effective_barrel_z_top", via["z_top"]))
        z_bottom = length(z_bottom_value)
        height = length(z_top_value - z_bottom_value)
        barrel = create_cylinder_compat(
            app, [x, y, z_bottom], "(via_signal_drill_diameter/2)", height,
            name + "_BARREL", str(via["material"]))
        if via.get("barrel_model") == "hollow":
            hole = create_cylinder_compat(
                app, [x, y, z_bottom], "(via_signal_finished_hole_diameter/2)", height,
                name + "_HOLE_TOOL", "vacuum")
            subtract_compat(app, barrel, [hole], keep_originals=False)
            require_object(app, barrel, "信号孔孔壁Subtract后检查")
        state["signal_objects"].setdefault(str(via["signal"]), []).append(barrel)
        for layer_name in via.get("functional_pad_layers", []):
            pad = create_cylinder_compat(
                app, [x, y, _layer_z_bottom_variable(str(layer_name))],
                "(via_signal_connected_pad_diameter/2)",
                _layer_thickness_variable(str(layer_name)),
                name + "_PAD_" + str(layer_name), str(via["material"]))
            if via.get("barrel_model") == "hollow":
                tool = create_cylinder_compat(
                    app, [x, y, _layer_z_bottom_variable(str(layer_name))],
                    "(via_signal_finished_hole_diameter/2)",
                    _layer_thickness_variable(str(layer_name)),
                    name + "_PAD_HOLE_" + str(layer_name), "vacuum")
                subtract_compat(app, pad, [tool], keep_originals=False)
            state["signal_objects"].setdefault(str(via["signal"]), []).append(pad)
        clearance_layers = set(str(layer_name) for layer_name in via.get("antipad_layers", []))
        for layer in GEOMETRY.get("stackup", []):
            layer_name = str(layer["name"])
            layer_type = str(layer.get("layer_type", ""))
            if layer_type in ("dielectric", "soldermask"):
                blank = "DIEL_" + layer_name
            elif layer_type == "plane" and layer_name not in clearance_layers:
                blank = state["plane_objects"].get(layer_name)
            else:
                continue
            if not blank or not object_exists(app, blank):
                continue
            cut_name = name + "_BOARD_CUT_" + safe_identifier(layer_name)
            cut_tool = create_cylinder_compat(
                app, [x, y, _layer_z_bottom_variable(layer_name)],
                "(via_signal_drill_diameter/2)",
                _layer_thickness_variable(layer_name), cut_name, "vacuum")
            subtract_compat(app, blank, [cut_tool], keep_originals=False)
            require_object(app, blank, "信号孔安装孔Subtract后检查")

        # Phase 3 背钻：在背钻范围内用更大直径工具切除介质/铜，形成背钻腔体。
        if via.get("backdrilled"):
            bd_z_bottom_value = float(via["backdrill_z_bottom"])
            bd_z_top_value = float(via["backdrill_z_top"])
            bd_z_bottom = length(bd_z_bottom_value)
            bd_height = length(bd_z_top_value - bd_z_bottom_value)
            for layer in GEOMETRY.get("stackup", []):
                layer_name = str(layer["name"])
                layer_type = str(layer.get("layer_type", ""))
                if layer_type not in ("dielectric", "soldermask", "plane"):
                    continue
                layer_bottom_value = float(layer["z_bottom"])
                layer_top_value = float(layer["z_top"])
                if layer_top_value <= bd_z_bottom_value or layer_bottom_value >= bd_z_top_value:
                    continue
                blank = ("DIEL_" + layer_name if layer_type in ("dielectric", "soldermask")
                         else state["plane_objects"].get(layer_name))
                if not blank or not object_exists(app, blank):
                    continue
                bd_tool = create_cylinder_compat(
                    app, [x, y, bd_z_bottom], length(float(via["backdrill_diameter"]) / 2.0),
                    bd_height, name + "_BACKDRILL_" + safe_identifier(layer_name), "vacuum")
                subtract_compat(app, blank, [bd_tool], keep_originals=False)
                require_object(app, blank, "背钻腔体Subtract后检查")
            state["backdrill_cavities"].append(name + "_BACKDRILL_CAVITY")


def _via_antipad_rotated_point(center, local_x, local_y, rotation="via_signal_antipad_rotation"):
    cosine = "cos(({})*pi/180)".format(rotation)
    sine = "sin(({})*pi/180)".format(rotation)
    return [
        "({} + ({})*{} - ({})*{})".format(length(float(center[0])), local_x, cosine, local_y, sine),
        "({} + ({})*{} + ({})*{})".format(length(float(center[1])), local_x, sine, local_y, cosine),
    ]


def _via_antipad_dimension_expression(contour, field):
    antipad = CONFIG.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    mode = str(antipad.get("size_mode", "manual"))
    if mode == "manual":
        return "via_signal_antipad_" + field
    nominal = float(contour.get(field, 0.0))
    expression = length(nominal)
    if field == "length":
        end = float(antipad.get("end_clearance", antipad.get("clearance", 0.0)))
        expression = "({} + 2*(via_signal_antipad_end_clearance-{})".format(expression, length(end))
        if mode == "auto_plus_increment":
            increment = float(antipad.get("length_increment", 0.0))
            expression += " + (via_signal_antipad_length_increment-{})".format(length(increment))
        expression += ")"
    elif field == "width":
        side = float(antipad.get("side_clearance", antipad.get("clearance", 0.0)))
        expression = "({} + 2*(via_signal_antipad_side_clearance-{})".format(expression, length(side))
        if mode == "auto_plus_increment":
            increment = float(antipad.get("width_increment", 0.0))
            expression += " + (via_signal_antipad_width_increment-{})".format(length(increment))
        expression += ")"
    return expression


def _via_antipad_diameter_expression(contour):
    antipad = CONFIG.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    mode = str(antipad.get("size_mode", "manual"))
    if mode == "manual" or str(antipad.get("topology", "independent")) != "merged_pair":
        return "via_signal_antipad_diameter"
    end = float(antipad.get("end_clearance", antipad.get("clearance", 0.0)))
    side = float(antipad.get("side_clearance", antipad.get("clearance", 0.0)))
    length_increment = float(antipad.get("length_increment", 0.0)) if mode == "auto_plus_increment" else 0.0
    width_increment = float(antipad.get("width_increment", 0.0)) if mode == "auto_plus_increment" else 0.0
    base = float(contour.get("diameter", 0.0)) - 2.0 * max(end, side) - max(length_increment, width_increment)
    expression = "({} + 2*max(via_signal_antipad_end_clearance,via_signal_antipad_side_clearance)".format(length(base))
    if mode == "auto_plus_increment":
        expression += " + max(via_signal_antipad_length_increment,via_signal_antipad_width_increment)"
    return expression + ")"


def _parametric_via_antipad_points(contour):
    """Expression outline for global non-circular VIA antipads."""
    shape = str(contour.get("shape", ""))
    center = contour.get("center", [0.0, 0.0])
    local = []
    segments = 72
    length_expr = _via_antipad_dimension_expression(contour, "length")
    width_expr = _via_antipad_dimension_expression(contour, "width")
    if shape == "ellipse":
        for index in range(segments):
            theta = 2.0 * math.pi * index / segments
            local.append(("(({})/2*{:.15g})".format(length_expr, math.cos(theta)),
                          "(({})/2*{:.15g})".format(width_expr, math.sin(theta))))
    elif shape == "capsule":
        half = segments // 2
        for base, start, stop in ((1, -math.pi / 2.0, math.pi / 2.0),
                                  (-1, math.pi / 2.0, 3.0 * math.pi / 2.0)):
            for index in range(half + 1):
                theta = start + (stop - start) * index / half
                local.append((
                    "({}*(({})-({}))/2 + ({})/2*{:.15g})".format(
                        base, length_expr, width_expr, width_expr, math.cos(theta)),
                    "(({})/2*{:.15g})".format(width_expr, math.sin(theta)),
                ))
    elif shape == "rounded_rectangle":
        quarter = segments // 4
        for sx, sy, start, stop in ((1, 1, 0.0, math.pi / 2.0),
                                    (-1, 1, math.pi / 2.0, math.pi),
                                    (-1, -1, math.pi, 3.0 * math.pi / 2.0),
                                    (1, -1, 3.0 * math.pi / 2.0, 2.0 * math.pi)):
            for index in range(quarter + 1):
                theta = start + (stop - start) * index / quarter
                local.append((
                    "({}*(({})/2-via_signal_antipad_corner_radius) + "
                    "via_signal_antipad_corner_radius*{:.15g})".format(sx, length_expr, math.cos(theta)),
                    "({}*(({})/2-via_signal_antipad_corner_radius) + "
                    "via_signal_antipad_corner_radius*{:.15g})".format(sy, width_expr, math.sin(theta)),
                ))
    else:
        return []
    return [_via_antipad_rotated_point(center, x, y) for x, y in local]


def _create_signal_antipads(app, state):
    """Subtract the authoritative topology/shape contours exactly once."""
    active_antipad_axes = {
        str(path) for path, sweep in CONFIG.get("sweeps", {}).items()
        if isinstance(sweep, dict) and sweep.get("enabled")
        and str(path).startswith("via_transition.padstack.antipad")
        and str(path) not in {
            "via_transition.padstack.antipad.topology",
            "via_transition.padstack.antipad.shape",
            "via_transition.padstack.antipad.clearance",
        }
    }
    for contour in GEOMETRY.get("antipad_contours", []):
        layer_name = str(contour["layer"])
        plane_name = state["plane_objects"].get(layer_name)
        if not plane_name:
            LOGGER.info(
                "VIA反焊盘层%s没有整面plane对象；状态为不适用，未伪造Subtract成功。",
                layer_name,
            )
            continue
        owner = safe_identifier(str(contour.get("owner", "PAIR")))
        topology = safe_identifier(str(contour.get("topology", "independent")))
        tool_name = "VIA_ANTIPAD_{}_{}_{}".format(owner, topology, safe_identifier(layer_name))
        if str(contour.get("shape")) == "circle":
            source = str(contour.get("source", "global"))
            diameter_expression = _via_antipad_diameter_expression(contour)
            radius = (("(via_signal_antipad_diameter/2)" if diameter_expression == "via_signal_antipad_diameter"
                       else "(({})/2)".format(diameter_expression)) if source == "global"
                      else length(float(contour.get("diameter", 0.0)) / 2.0))
            if not active_antipad_axes:
                radius = length(float(contour.get("diameter", 0.0)) / 2.0)
            center = contour.get("center", [0.0, 0.0])
            tool = create_cylinder_compat(
                app, [length(float(center[0])), length(float(center[1])),
                      _layer_z_bottom_variable(layer_name)],
                radius, _layer_thickness_variable(layer_name), tool_name, "vacuum")
        else:
            points = (_parametric_via_antipad_points(contour)
                      if str(contour.get("source", "global")) == "global" and active_antipad_axes
                      else [[float(point[0]), float(point[1])]
                            for point in contour.get("points", [])])
            if len(points) < 4:
                raise RuntimeError("{}非圆反焊盘轮廓点不足。".format(tool_name))
            tool = create_polygon_solid_compat(
                app, points, _layer_z_bottom_variable(layer_name),
                _layer_thickness_variable(layer_name), tool_name, "vacuum")
        state["via_antipad_tools"].setdefault(layer_name, []).append(tool_name)
        subtract_compat(app, plane_name, [tool], keep_originals=False)
        require_object(app, plane_name, "信号孔反焊盘Subtract后检查")


def _create_return_vias(app, state):
    ground_parts = []
    connected_layers = []
    for via in GEOMETRY.get("return_vias", []):
        name = str(via["name"])
        x, y = length(float(via["x"])), length(float(via["y"]))
        z_bottom = length(float(via["z_bottom"]))
        height = length(float(via["z_top"]) - float(via["z_bottom"]))
        barrel = create_cylinder_compat(
            app, [x, y, z_bottom], length(float(via["drill_diameter"]) / 2.0), height,
            name + "_BARREL", str(via["material"]))
        if via.get("barrel_model") == "hollow":
            hole = create_cylinder_compat(
                app, [x, y, z_bottom], length(float(via["finished_hole_diameter"]) / 2.0), height,
                name + "_HOLE_TOOL", "vacuum")
            subtract_compat(app, barrel, [hole], keep_originals=False)
        ground_parts.append(barrel)
        for layer_name in via.get("connect_layers", []):
            layer_name = str(layer_name)
            if layer_name not in connected_layers:
                connected_layers.append(layer_name)
            pad = create_cylinder_compat(
                app, [x, y, _layer_z_bottom_variable(layer_name)],
                length(float(via["pad_diameter"]) / 2.0),
                _layer_thickness_variable(layer_name),
                name + "_PAD_" + layer_name, str(via["material"]))
            if via.get("barrel_model") == "hollow":
                pad_hole = create_cylinder_compat(
                    app, [x, y, _layer_z_bottom_variable(layer_name)],
                    length(float(via["finished_hole_diameter"]) / 2.0),
                    _layer_thickness_variable(layer_name),
                    name + "_PAD_HOLE_" + layer_name, "vacuum")
                subtract_compat(app, pad, [pad_hole], keep_originals=False)
            ground_parts.append(pad)
        for layer_name in via.get("clearance_layers", []):
            plane_name = state["plane_objects"].get(str(layer_name))
            if not plane_name:
                continue
            tool_name = name + "_ANTIPAD_" + str(layer_name)
            tool = create_cylinder_compat(
                app, [x, y, _layer_z_bottom_variable(str(layer_name))],
                length(float(via["antipad_diameter"]) / 2.0),
                _layer_thickness_variable(str(layer_name)), tool_name, "vacuum")
            state["via_antipad_tools"].setdefault(str(layer_name), []).append(tool_name)
            subtract_compat(app, plane_name, [tool], keep_originals=False)

        # Mechanical drilling removes dielectric/soldermask across the full
        # return-via span.  Without these cuts the copper barrel is embedded
        # in dielectric solids and native HFSS validation rejects the model.
        for layer in GEOMETRY.get("stackup", []):
            layer_name = str(layer["name"])
            if str(layer.get("layer_type", "")) not in ("dielectric", "soldermask"):
                continue
            blank = "DIEL_" + layer_name
            if not object_exists(app, blank):
                continue
            cut_tool = create_cylinder_compat(
                app, [x, y, _layer_z_bottom_variable(layer_name)],
                length(float(via["drill_diameter"]) / 2.0),
                _layer_thickness_variable(layer_name),
                name + "_BOARD_CUT_" + safe_identifier(layer_name), "vacuum")
            subtract_compat(app, blank, [cut_tool], keep_originals=False)
            require_object(app, blank, "回流孔机械钻孔Subtract后检查")

    # Return barrels, annular pads, and every explicitly connected reference
    # plane are one physical ground conductor.  Leaving them as overlapping
    # copper solids causes native intersection errors and does not prove an
    # electrical return path through the truncated model.
    if ground_parts and connected_layers:
        plane_parts = []
        for layer_name in connected_layers:
            plane_name = state["plane_objects"].get(layer_name)
            if plane_name and plane_name not in plane_parts:
                plane_parts.append(plane_name)
        if not plane_parts:
            raise RuntimeError("回流孔没有找到可连接的参考平面对象。")
        final_ground = plane_parts[0]
        united_ground = unite_compat(app, plane_parts + ground_parts, final_ground)
        for layer_name in connected_layers:
            state["plane_objects"][layer_name] = united_ground
        LOGGER.info(
            "回流孔桶、焊盘及连接参考层已合并为公共地导体%s：%s",
            united_ground, connected_layers,
        )


def build_via_transition_scenario(app, state):
    """创建差分贯通孔换层场景的完整几何。"""
    create_dielectric_layers(app, state)
    create_reference_planes(app, state)
    _create_via_transition_traces(app, state)
    _create_signal_vias(app, state)
    _create_signal_antipads(app, state)
    _create_return_vias(app, state)
    for signal in ("P", "N"):
        parts = list(state["signal_objects"].get(signal, []))
        if len(parts) > 1:
            united = unite_compat(app, parts, "SIGNAL_" + signal)
            state["signal_objects"][signal] = [united]
    create_simulation_setup(app, state)


def _mixed_mode_from_single_ended(se):
    """4x4 单端 S 矩阵 -> (SDD11, SDD21, SDC21, SCD21) 复数（内联，自包含）。

    端口顺序 [PORT_IN_P, PORT_IN_N, PORT_OUT_P, PORT_OUT_N]；
    混合模顺序 [IN_DIFF, OUT_DIFF, IN_COMM, OUT_COMM]。
    """
    inv_sqrt2 = 1.0 / math.sqrt(2.0)
    m = ((1.0, -1.0, 0.0, 0.0), (0.0, 0.0, 1.0, -1.0),
         (1.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 1.0))
    ms = [[sum(m[i][k] * se[k][j] for k in range(4)) * inv_sqrt2 for j in range(4)]
          for i in range(4)]
    mm = [[sum(ms[i][k] * m[j][k] for k in range(4)) * inv_sqrt2 for j in range(4)]
          for i in range(4)]
    return mm[0][0], mm[1][0], mm[1][2], mm[3][0]


def _tdr_settings():
    """规格 §10.4 的差分 TDR 设置（离线 IFT，禁止对 dB 曲线直接 IFFT）。"""
    return {
        "algorithm": "offline_ift_sdd11",
        "input_curve": "SDD11",
        "differential_reference_impedance": 100.0,
        "rise_time_ps": 35.0,
        "dc_extrapolation": "linear",
        "window_function": "hann",
        "window_beta": 6.0,
        "causality": "hermitian_symmetry",
        "propagation_velocity_m_s": None,
        "distance_axis": "time",
    }


def _tdr_i0(x):
    total = 1.0
    term = 1.0
    k = 1
    half = x / 2.0
    while True:
        term *= half * half / (k * k)
        total += term
        if term < 1e-15 * total:
            break
        k += 1
        if k > 1000:
            break
    return total


def _tdr_window(f_ghz, f_max, kind, beta):
    if kind in ("none", None) or f_max <= 0:
        return 1.0
    u = min(max(f_ghz / f_max, 0.0), 1.0)
    if kind in ("hann", "raised_cosine"):
        return 0.5 * (1.0 + math.cos(math.pi * u))
    if kind == "kaiser":
        if beta <= 0:
            return 1.0
        return _tdr_i0(beta * math.sqrt(max(0.0, 1.0 - u * u))) / _tdr_i0(beta)
    return 1.0


def _tdr_rise_filter(f_ghz, rise_time_ps):
    if not rise_time_ps or rise_time_ps <= 0:
        return 1.0
    sigma = rise_time_ps * 1e-12 / 2.563
    return math.exp(-2.0 * (math.pi * sigma * f_ghz * 1e9) ** 2)


def _tdr_from_sdd11(frequency_ghz, sdd11, settings):
    """离线差分 TDR：由复数 SDD11 计算差分阻抗随时间/距离曲线（规格 §10.4）。

    只接受复数 SDD11；频率必须是 DC 锚定的均匀网格 freq[k]=(k+1)·Δf。
    返回 dict(time_ns, distance_mm, reflection, impedance_ohm, settings, warnings)。
    """
    freq = [float(f) for f in frequency_ghz]
    gamma = [complex(v) for v in sdd11]
    if len(freq) < 2 or len(freq) != len(gamma):
        raise ValueError("TDR 输入长度不足或不一致。")
    df = freq[0]
    if df <= 0:
        raise ValueError("TDR frequency grid must start above DC")
    for index, value in enumerate(freq):
        expected = (index + 1) * df
        if abs(value - expected) > 1e-6 * max(1.0, abs(expected)):
            raise ValueError(
                "TDR requires a DC-anchored uniform grid: "
                "freq[k]=(k+1)*df with df=freq[0]; "
                "freq[{}]={} but expected {}".format(index, value, expected)
            )
    m = len(freq)
    n = 2 * m + 1
    f_max = freq[-1]
    if settings.get("dc_extrapolation") == "zero":
        gamma0 = 0.0
    elif settings.get("dc_extrapolation") == "constant":
        gamma0 = gamma[0].real
    else:
        gamma0 = gamma[0].real - (gamma[1].real - gamma[0].real) * freq[0] / (freq[1] - freq[0])
    spectrum = [0j] * n
    spectrum[0] = complex(gamma0, 0.0)
    for k in range(1, m + 1):
        f = k * df
        w = _tdr_window(f, f_max, settings.get("window_function"), float(settings.get("window_beta", 6.0)))
        g = _tdr_rise_filter(f, float(settings.get("rise_time_ps", 0.0)))
        spectrum[k] = gamma[k - 1] * w * g
    for k in range(1, m + 1):
        spectrum[n - k] = spectrum[k].conjugate()
    h = []
    for mm in range(n):
        re = 0.0
        im = 0.0
        for k in range(n):
            value = spectrum[k]
            angle = 2.0 * math.pi * k * mm / n
            re += value.real * math.cos(angle) - value.imag * math.sin(angle)
            im += value.real * math.sin(angle) + value.imag * math.cos(angle)
        h.append(complex(re, im))
    folded = h[0] + sum((h[i] for i in range(m + 1, n)), 0j)
    causal = [0j] * n
    causal[0] = folded
    for i in range(1, m + 1):
        causal[i] = h[i]
    half = m + 1
    dt_ns = 1.0 / (n * df)
    zref = float(settings.get("differential_reference_impedance", 100.0))
    time_ns = []
    reflection = []
    impedance = []
    accumulator = 0.0
    for i in range(half):
        accumulator += causal[i].real
        rho = accumulator / n
        time_ns.append(i * dt_ns)
        reflection.append(rho)
        clamped = min(max(rho, -1.0 + 1e-9), 1.0 - 1e-9)
        impedance.append(zref * (1.0 + clamped) / (1.0 - clamped))
    velocity = settings.get("propagation_velocity_m_s")
    distance_mm = []
    if velocity:
        velocity = float(velocity)
        distance_mm = [velocity * t * 1e-9 / 2.0 * 1e3 for t in time_ns]
        settings["distance_axis"] = "distance"
    else:
        settings["distance_axis"] = "time"
    return {
        "schema_version": "via_tdr_v1",
        "settings": dict(settings),
        "frequency_window_ghz": [freq[0], f_max],
        "time_window_ns": m * dt_ns,
        "time_resolution_ns": dt_ns,
        "time_ns": time_ns,
        "distance_mm": distance_mm,
        "reflection": reflection,
        "impedance_ohm": impedance,
        "warnings": [],
    }


def solve_via_transition_single_case(app, state):
    """Phase 4 单案例求解：显式调用目标 Setup 的 Analyze，记录时间/返回值。

    由环境变量 ``SURFACE_CAP_SOLVE_SINGLE=1`` 门控；不满足时返回 None。
    真实收敛判定与 S 参数导出见 ``export_via_transition_results``。
    """
    if str(os.environ.get("SURFACE_CAP_SOLVE_SINGLE", "")).strip() != "1":
        return None
    setup_name = str(CONFIG.get("simulation", {}).get("setup_name", "Setup1"))
    started = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    LOGGER.info("单案例求解开始：setup=%s", setup_name)
    result = None
    try:
        analyzer = getattr(app, "analyze_setup", None)
        if analyzer is not None:
            result = analyzer(setup_name)
        else:
            result = app.odesign.Analyze()
    except Exception:
        LOGGER.error("单案例求解失败：\n%s", traceback.format_exc())
        raise
    finished = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    report = {
        "schema_version": 1,
        "setup_name": setup_name,
        "started_at": started,
        "finished_at": finished,
        "solve_return_value": repr(result),
    }
    report_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_via_solve.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOGGER.info("单案例求解完成：setup=%s", setup_name)
    return report


def _hfss_profile_convergence(app, setup_name, solve_report=None):
    """Return (solve_completed, adaptive_converged, evidence) from AEDT Profile."""
    try:
        profiles = app.get_profile(setup_name)
        if profiles:
            for profile_name, profile in profiles.items():
                if setup_name not in str(profile_name):
                    continue
                status = str(getattr(profile, "status", "") or "").strip()
                status_lower = status.lower()
                completed = status_lower in ("normal completion", "completed") or "normal completion" in status_lower
                adaptive = getattr(profile, "adaptive_pass", None)
                adaptive_info = str(getattr(adaptive, "info", "") or "").strip()
                adaptive_lower = adaptive_info.lower()
                if "did not converge" in adaptive_lower or "not converged" in adaptive_lower:
                    converged = False
                else:
                    converged = "converged" in adaptive_lower
                evidence = adaptive_info or status or "AEDT profile did not expose adaptive convergence text"
                return completed, converged, evidence
    except Exception as exc:
        return bool(solve_report), False, "AEDT profile query failed: {}".format(exc)
    return bool(solve_report), False, "AEDT profile unavailable"


def export_via_transition_results(app, state, solve_report=None):
    """导出混合模 S 参数到 via_results.json 与 CSV（规格 §10.2）。

    由环境变量 ``SURFACE_CAP_EXPORT_RESULTS=1`` 门控。无法提取真实 S 参数时
    fail closed：只写出带 warnings 的占位结构，不伪造曲线。
    """
    if str(os.environ.get("SURFACE_CAP_EXPORT_RESULTS", "")).strip() != "1":
        return None
    setup_name = str(CONFIG.get("simulation", {}).get("setup_name", "Setup1"))
    sweep_name = str(CONFIG.get("simulation", {}).get("sweep_name", "Sweep1"))
    expressions = {
        "Sdd11": "St(IN_DIFF,IN_DIFF)",
        "Sdd21": "St(OUT_DIFF,IN_DIFF)",
        "Sdc21": "St(OUT_COMM,IN_DIFF)",
        "Scd21": "St(OUT_DIFF,IN_COMM)",
    }
    frequencies = []
    sdd11_db, sdd21_db, sdc21_db, scd21_db = [], [], [], []
    sdd11_re, sdd11_im = [], []
    warnings = []
    try:
        # PyAEDT 1.3's high-level ``get_solution_data`` first enumerates and
        # evaluates every AEDT design variable.  With AEDT 2022 R2 that path
        # can lose the design/project OO handles and fail even though the
        # solved Terminal data is present.  The per-variation API calls AEDT's
        # report module directly and is reliable on both the tested 2022 R2
        # stack and newer PyAEDT versions.
        solution = app.post.get_solution_data_per_variation(
            solution_type="Terminal Solution Data",
            setup_sweep_name=setup_name + " : " + sweep_name,
            context=[],
            sweeps={"Freq": "All"},
            expressions=list(expressions.values()),
        )
        if not solution:
            raise RuntimeError("PyAEDT did not return solution data")

        frequency_unit = str(
            getattr(solution, "units_sweeps", {}).get(
                getattr(solution, "primary_sweep", "Freq"), ""
            ) or ""
        ).strip().lower()
        to_ghz = {"hz": 1e-9, "khz": 1e-6, "mhz": 1e-3, "ghz": 1.0}.get(frequency_unit)
        if to_ghz is None:
            raise RuntimeError("unsupported frequency sweep unit: {!r}".format(frequency_unit))

        complex_curves = {}
        frequency_axis = None
        for metric, expression in expressions.items():
            real_axis, real_values = solution.get_expression_data(expression=expression, formula="real")
            imag_axis, imag_values = solution.get_expression_data(expression=expression, formula="imag")
            real_axis = list(real_axis)
            imag_axis = list(imag_axis)
            real_values = list(real_values)
            imag_values = list(imag_values)
            if not real_axis or real_axis != imag_axis or len(real_axis) != len(real_values) or len(real_axis) != len(imag_values):
                raise RuntimeError("incomplete solution curve for {}".format(expression))
            if frequency_axis is None:
                frequency_axis = real_axis
            elif frequency_axis != real_axis:
                raise RuntimeError("inconsistent frequency axes in terminal S-parameters")
            complex_curves[metric] = [complex(float(re), float(im)) for re, im in zip(real_values, imag_values)]

        frequencies = [float(value) * to_ghz for value in (frequency_axis or [])]
        for metric, target in (("Sdd11", sdd11_db), ("Sdd21", sdd21_db),
                               ("Sdc21", sdc21_db), ("Scd21", scd21_db)):
            target.extend(20.0 * math.log10(max(abs(value), 1e-30))
                          for value in complex_curves[metric])
        sdd11_re = [value.real for value in complex_curves["Sdd11"]]
        sdd11_im = [value.imag for value in complex_curves["Sdd11"]]
    except Exception as exc:
        warnings.append(f"S 参数提取失败，未写入结果曲线：{exc}")

    solve_completed, converged, convergence_evidence = _hfss_profile_convergence(
        app, setup_name, solve_report
    )
    if solve_completed and not converged:
        warnings.append("HFSS adaptive convergence not established: " + convergence_evidence)

    result = {
        "schema_version": "via_results_v1",
        "software_version": str(BUILD_METADATA.get("software_version", "")),
        "config_hash": str(CONFIG_HASH or ""),
        "geometry_hash": str(GEOMETRY_HASH or ""),
        "aedt_version": str(CONFIG.get("project", {}).get("aedt_version", "")),
        "pyaedt_version": _pyaedt_version(),
        "project": PROJECT_NAME,
        "design": str(CONFIG.get("project", {}).get("design_name", "")),
        "setup": setup_name,
        "sweep": sweep_name,
        "variation": {},
        "frequency_ghz": frequencies,
        "Sdd11_db": sdd11_db,
        "Sdd21_db": sdd21_db,
        "Sdc21_db": sdc21_db,
        "Scd21_db": scd21_db,
        "Sdd11_re": sdd11_re,
        "Sdd11_im": sdd11_im,
        "solve_completed": solve_completed,
        "converged": converged,
        "convergence_evidence": convergence_evidence,
        "port_mapping": [
            {"port": "PORT_IN_P", "pair": "IN", "polarity": "P"},
            {"port": "PORT_IN_N", "pair": "IN", "polarity": "N"},
            {"port": "PORT_OUT_P", "pair": "OUT", "polarity": "P"},
            {"port": "PORT_OUT_N", "pair": "OUT", "polarity": "N"},
        ],
        "launch_lengths": {
            "entry": float(CONFIG.get("via_transition", {}).get("entry_trace", {}).get("length", 0.0)),
            "exit": float(CONFIG.get("via_transition", {}).get("exit_trace", {}).get("length", 0.0)),
        },
        "deembed_settings": {},
        "tdr_settings": _tdr_settings(),
        "warnings": warnings,
    }
    json_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_via_results.json")
    csv_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_via_results.csv")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with open(csv_path, "w", encoding="utf-8", newline="") as stream:
        stream.write("frequency_ghz,Sdd11_db,Sdd21_db,Sdc21_db,Scd21_db\n")
        for index, freq in enumerate(frequencies):
            stream.write(f"{freq:.9g},{sdd11_db[index]:.9g},{sdd21_db[index]:.9g},"
                         f"{sdc21_db[index]:.9g},{scd21_db[index]:.9g}\n")
    LOGGER.info("via_results 导出完成：%s / %s", json_path.name, csv_path.name)

    # Phase 5：由复数 SDD11 计算离线差分 TDR，导出 via_tdr.json / via_tdr.csv。
    tdr_json_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_via_tdr.json")
    tdr_csv_path = SCRIPT_DIRECTORY / (PROJECT_NAME + "_via_tdr.csv")
    for stale_tdr_path in (tdr_json_path, tdr_csv_path):
        try:
            if stale_tdr_path.exists():
                stale_tdr_path.unlink()
        except OSError as exc:
            LOGGER.warning("Unable to remove stale TDR output %s: %s", stale_tdr_path, exc)
    if (sdd11_re and frequencies
            and bool(CONFIG.get("simulation", {}).get("generate_tdr", False))):
        try:
            tdr_settings = _tdr_settings()
            tdr = _tdr_from_sdd11(
                frequencies,
                [complex(re, im) for re, im in zip(sdd11_re, sdd11_im)],
                tdr_settings,
            )
            result["tdr_settings"] = tdr["settings"]
            tdr_json_path.write_text(json.dumps(tdr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            header = "time_ns" + (",distance_mm" if tdr["distance_mm"] else "") + ",reflection,impedance_ohm"
            rows = [header]
            for index, t in enumerate(tdr["time_ns"]):
                row = f"{t:.9g}"
                if tdr["distance_mm"]:
                    row += f",{tdr['distance_mm'][index]:.9g}"
                row += f",{tdr['reflection'][index]:.9g},{tdr['impedance_ohm'][index]:.9g}"
                rows.append(row)
            tdr_csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            LOGGER.info("via_tdr 导出完成：%s / %s", tdr_json_path.name, tdr_csv_path.name)
        except Exception as exc:
            warnings.append(f"TDR 计算失败，未写入 TDR 曲线：{exc}")
            result["warnings"] = warnings
    # The S-parameter JSON is created before optional TDR processing.  Persist
    # final TDR settings and fail-closed warnings as the authoritative result.
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def build_via_transition_model():
    app = None
    state = _new_state()
    try:
        clear_previous_hfss_validation_reports()
        app = create_hfss_project()
        create_hfss_design(app)
        configure_hfss_design_settings(app)
        create_design_variables(app)
        create_materials(app)
        build_via_transition_scenario(app, state)
        save_aedt_project(app)
        verify_native_optimetrics_geometry_binding(app, state)
        validate_created_objects(app, state)
        run_native_optimetrics_if_requested(app, state)
        save_aedt_project(app)
        solve_report = solve_via_transition_single_case(app, state)
        export_via_transition_results(app, state, solve_report)
        _publish_project_copy(AEDT_PROJECT_PATH, REQUESTED_AEDT_PROJECT_PATH, "validated")
        LOGGER.info("via_transition HFSS几何建模全部完成。")
        if OPEN_AEDT_AFTER_BUILD:
            LOGGER.info("AEDT将保持打开，供用户检查并手动继续设置。")
        else:
            LOGGER.info("按配置释放AEDT Desktop。")
            release_desktop_compat(app)
        return 0
    except Exception:
        LOGGER.error("via_transition建模失败：\n%s", traceback.format_exc())
        if not HFSS_VALIDATE_JSON_PATH.is_file():
            try:
                write_hfss_validation_report([
                    _hfss_item("HFSS-999", "error", "HFSS建模或工程检查异常。",
                               suggestion="查看build.log和失败现场工程；修正后重新运行。")
                ], traceback.format_exc(), incomplete=True)
            except Exception:
                LOGGER.error("写入HFSS结构化Validate报告失败。")
        if app is not None:
            try:
                save_project_compat(app, FAILED_AEDT_PROJECT_PATH)
            except Exception:
                pass
            if not OPEN_AEDT_AFTER_BUILD:
                try:
                    release_desktop_compat(app)
                except Exception:
                    pass
        return 1


def build_model():
    """按需求规定顺序创建完整HFSS几何模型。"""

    validate_embedded_data()
    app = None
    state = _new_state()
    scenario_type = str(CONFIG.get("scenario", {}).get("type", "surface_capacitor"))
    if scenario_type == "via_transition":
        return build_via_transition_model()
    try:
        clear_previous_hfss_validation_reports()
        app = create_hfss_project()
        create_hfss_design(app)
        configure_hfss_design_settings(app)
        create_design_variables(app)
        create_materials(app)

        create_dielectric_layers(app, state)
        create_surface_soldermask_openings(app, state)
        create_reference_planes(app, state)

        create_input_straight_traces(app, state)
        create_input_45deg_transitions(app, state)
        create_input_pad_leads(app, state)
        create_capacitor_pads(app, state)
        create_pad_bridges(app, state)
        create_capacitor_electrical_models(app, state)
        create_output_pad_leads(app, state)
        create_output_45deg_transitions(app, state)
        create_output_straight_traces(app, state)
        create_optional_capacitor_bodies(app, state)

        unite_positive_signal_objects(app, state)
        unite_negative_signal_objects(app, state)
        remove_signal_fragments(app)

        create_antipad_tools(app, state)
        subtract_antipads_from_planes(app, state)
        create_gnd_vias(app, state)
        create_top_ground_copper(app, state)
        create_simulation_setup(app, state)
        # Persist before auditing so the verifier can independently inspect
        # the AEDT project database when the 2022 R2 object-model cache does
        # not expose newly inserted Optimetrics sweep children.
        save_aedt_project(app)
        verify_native_optimetrics_geometry_binding(app, state)

        validate_created_objects(app, state)
        run_native_optimetrics_if_requested(app, state)
        save_aedt_project(app)
        _publish_project_copy(AEDT_PROJECT_PATH, REQUESTED_AEDT_PROJECT_PATH, "validated")
        try:
            app.modeler.fit_all()
        except Exception:
            LOGGER.warning("自动Fit All失败，不影响工程保存。")

        LOGGER.info("HFSS几何建模全部完成。")
        LOGGER.info("自动仿真：%s。", "已创建" if CONFIG.get("simulation", {}).get("enabled") else "关闭")
        if OPEN_AEDT_AFTER_BUILD:
            LOGGER.info("AEDT将保持打开，供用户检查并手动继续设置。")
        else:
            LOGGER.info("按配置释放AEDT Desktop。")
            release_desktop_compat(app)
        return 0
    except Exception:
        LOGGER.error("建模失败：\n%s", traceback.format_exc())
        if not HFSS_VALIDATE_JSON_PATH.is_file():
            try:
                write_hfss_validation_report([
                    _hfss_item(
                        "HFSS-999", "error", "HFSS建模或工程检查异常。",
                        suggestion="查看build.log和失败现场工程；修正后重新运行。",
                    )
                ], traceback.format_exc(), incomplete=True)
            except Exception:
                LOGGER.error("写入HFSS结构化Validate报告失败。")
        if app is not None:
            try:
                save_project_compat(app, FAILED_AEDT_PROJECT_PATH)
                _publish_project_copy(
                    FAILED_AEDT_PROJECT_PATH,
                    REQUESTED_FAILED_PROJECT_PATH,
                    "failed",
                )
                LOGGER.error("已尝试保存失败现场工程。")
            except Exception:
                LOGGER.error("保存失败现场工程也未成功。")
            if not OPEN_AEDT_AFTER_BUILD:
                try:
                    release_desktop_compat(app)
                except Exception:
                    pass
        return 1


def main():
    """命令行入口。"""

    parser = argparse.ArgumentParser(
        description="创建表层差分线串联电容反焊盘HFSS 3D模型"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="只检查嵌入配置和几何，不启动AEDT",
    )
    arguments = parser.parse_args()
    if arguments.validate_only:
        try:
            validate_embedded_data()
            print("独立脚本嵌入数据检查通过。")
            return 0
        except Exception:
            LOGGER.error("检查失败：\n%s", traceback.format_exc())
            return 1
    return build_model()


if __name__ == "__main__":
    raise SystemExit(main())
'''
