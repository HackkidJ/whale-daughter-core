"""插件系统：把"下载一个插件就能给她加能力"做出来。

约定（插件 = 一个目录）：

    plugins/
      weather_demo/
        plugin.yaml      # 名字、版本、权限、是否启用
        __init__.py      # def register(api): api.tool(...)

安全边界：
- 插件在进程内运行 = 能读到环境变量（API Key 等），所以默认只信任自己的代码；
- `permissions` 必须声明；`filesystem` 默认拒绝，除非插件名写进 `DSD_PLUGINS_ALLOWLIST`；
- 加载失败只打日志，不影响主服务；
- 支持热加载：改插件文件后最多 5 秒生效（由 Agent 定时检查签名）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog
import yaml

from .tools import Tool, ToolRegistry

log = structlog.get_logger(__name__)

SAFE_PERMISSIONS = {"network", "qq"}
RISKY_PERMISSIONS = {"filesystem"}


class PluginError(RuntimeError):
    pass


@dataclass(slots=True)
class PluginManifest:
    name: str
    version: str = "0.0.0"
    description: str = ""
    enabled: bool = True
    permissions: list[str] = field(default_factory=list)
    author: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PluginError(f"{path.name} 不是合法的 YAML 映射")
        name = str(raw.get("name") or path.parent.name).strip()
        if not name:
            raise PluginError(f"{path} 缺少 name")
        permissions = [str(item).strip() for item in raw.get("permissions") or []]
        return cls(
            name=name,
            version=str(raw.get("version") or "0.0.0"),
            description=str(raw.get("description") or ""),
            enabled=bool(raw.get("enabled", True)),
            permissions=permissions,
            author=str(raw.get("author") or ""),
            options=dict(raw.get("options") or {}),
        )


@dataclass(slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    path: Path
    tools: list[Tool] = field(default_factory=list)
    contexts: list[Any] = field(default_factory=list)


class PluginAPI:
    """传给插件 register() 的上下文：注册工具、拿配置、打日志。"""

    def __init__(
        self, *, manifest: PluginManifest, settings: Any, options: dict[str, Any] | None = None
    ) -> None:
        self.manifest = manifest
        self.settings = settings
        self.options = options or {}
        self.logger = log.bind(plugin=manifest.name)
        self.tools: list[Tool] = []
        self.context_providers: list[Any] = []

    def context(self, provider: Any) -> None:
        """注册一段"注入到 system 提示"的动态内容（async 无参函数，返回文本）。

        适合：余额/饥饿状态、当前心情、外部实时状态。每次请求前都会调用一次，
        所以provider 里要自己做缓存（别每次都打网络）。
        """
        if not callable(provider):
            raise PluginError("context() 需要传一个可调用对象")
        self.context_providers.append(provider)

    def tool(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Any,
        is_action: bool = False,
    ) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise PluginError(f"工具名只能用字母/数字/下划线：{name}")
        self.tools.append(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                is_action=is_action,
            )
        )


class PluginManager:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.plugins: list[LoadedPlugin] = []
        self._loaded_signature: tuple = ()
        self._skipped: list[dict[str, str]] = []

    # ---------------------------------------------------------------- 扫描
    @property
    def plugins_dir(self) -> Path:
        return Path(self.settings.plugins_dir)

    @property
    def allowlist(self) -> set[str]:
        raw = self.settings.plugins_allowlist or ""
        return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}

    def _plugin_dirs(self) -> list[Path]:
        root = self.plugins_dir
        if not root.exists():
            return []
        return sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "plugin.yaml").exists() and not path.name.startswith("_")
        )

    def signature(self) -> tuple:
        """目录 mtime 指纹：文件一改，Agent 就会重建注册表。"""
        parts: list[tuple] = []
        for directory in self._plugin_dirs():
            stamps = []
            for file in sorted(directory.rglob("*")):
                if file.is_file() and file.suffix in {".py", ".yaml", ".yml", ".json"}:
                    try:
                        stamps.append((str(file.relative_to(directory)), file.stat().st_mtime_ns))
                    except OSError:
                        continue
            parts.append((directory.name, tuple(stamps)))
        return tuple(parts)

    # ---------------------------------------------------------------- 加载
    def load_all(self) -> list[LoadedPlugin]:
        loaded: list[LoadedPlugin] = []
        skipped: list[dict[str, str]] = []
        allowlist = self.allowlist

        for directory in self._plugin_dirs():
            try:
                manifest = PluginManifest.load(directory / "plugin.yaml")
            except Exception as exc:
                log.warning("插件清单读取失败", plugin=directory.name, error=str(exc))
                skipped.append({"plugin": directory.name, "reason": f"清单无效：{exc}"})
                continue

            if not manifest.enabled:
                skipped.append({"plugin": manifest.name, "reason": "manifest 里 enabled=false"})
                continue
            if allowlist and manifest.name not in allowlist:
                skipped.append(
                    {
                        "plugin": manifest.name,
                        "reason": "不在 DSD_PLUGINS_ALLOWLIST 白名单里",
                    }
                )
                continue
            unknown = set(manifest.permissions) - SAFE_PERMISSIONS - RISKY_PERMISSIONS
            if unknown:
                log.warning(
                    "插件声明了未知权限，已拒绝", plugin=manifest.name, permissions=sorted(unknown)
                )
                skipped.append({"plugin": manifest.name, "reason": f"未知权限：{sorted(unknown)}"})
                continue
            risky = set(manifest.permissions) & RISKY_PERMISSIONS
            if risky and manifest.name not in allowlist:
                skipped.append(
                    {
                        "plugin": manifest.name,
                        "reason": (
                            f"声明了高危权限 {sorted(risky)}，"
                            "需要写进 DSD_PLUGINS_ALLOWLIST 才会加载"
                        ),
                    }
                )
                continue

            module_file = directory / "__init__.py"
            if not module_file.exists():
                skipped.append({"plugin": manifest.name, "reason": "缺少 __init__.py"})
                continue

            try:
                module = self._import_module(manifest.name, module_file)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise PluginError("__init__.py 里没有 register(api) 函数")
                api = PluginAPI(
                    manifest=manifest,
                    settings=self.settings,
                    options=manifest.options,
                )
                register(api)
                loaded.append(
                    LoadedPlugin(
                        manifest=manifest,
                        path=directory,
                        tools=api.tools,
                        contexts=api.context_providers,
                    )
                )
                log.info(
                    "插件已加载", plugin=manifest.name, tools=[tool.name for tool in api.tools]
                )
            except Exception as exc:
                log.warning("插件加载失败", plugin=manifest.name, error=str(exc))
                skipped.append({"plugin": manifest.name, "reason": f"加载失败：{exc}"})

        self.plugins = loaded
        self._skipped = skipped
        self._loaded_signature = self.signature()
        return loaded

    def _import_module(self, name: str, module_file: Path):
        module_name = (
            "dsdaughter_plugin_" + hashlib.sha1(str(module_file).encode()).hexdigest()[:12]
        )
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(module_name, module_file)
        if spec is None or spec.loader is None:
            raise PluginError("无法创建模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    # ---------------------------------------------------------------- 注册
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []
        for plugin in self.plugins:
            for tool in plugin.tools:
                try:
                    registry.register(tool)
                    registered.append(tool.name)
                except ValueError:
                    log.warning(
                        "插件工具与已有工具重名，已跳过",
                        plugin=plugin.manifest.name,
                        tool=tool.name,
                    )
        return registered

    @property
    def all_tools(self) -> list[Tool]:
        return [tool for plugin in self.plugins for tool in plugin.tools]

    def context_providers(self) -> list[tuple[str, Any]]:
        return [
            (plugin.manifest.name, provider)
            for plugin in self.plugins
            for provider in plugin.contexts
        ]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.plugins_enabled),
            "dir": str(self.plugins_dir),
            "loaded": [
                {
                    "name": plugin.manifest.name,
                    "version": plugin.manifest.version,
                    "description": plugin.manifest.description,
                    "permissions": plugin.manifest.permissions,
                    "tools": [tool.name for tool in plugin.tools],
                }
                for plugin in self.plugins
            ],
            "skipped": self._skipped,
        }

    def needs_reload(self) -> bool:
        return self.signature() != self._loaded_signature


class SupportsPlugins(Protocol):
    plugins_enabled: bool
