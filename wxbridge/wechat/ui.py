"""微信客户端 UI 自动化：AT-SPI 定位 + xdotool 输入。

仅支持 X11 / Xwayland。纯 Wayland 下 xdotool 看不到窗口，
请在启动微信时加 QT_QPA_PLATFORM=xcb，或使用 X11 会话。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Iterable

from wxbridge.wechat.store import StoreError


class UIError(RuntimeError):
    def __init__(self, message, *, code="ui_error", metadata=None):
        super().__init__(message)
        self.code = code
        self.metadata = metadata


class WrongChatError(UIError):
    """目标群没有选中，继续发送会发错群。"""


def chat_label_matches(label: str, chat_name: str) -> bool:
    lines = label.strip().splitlines()
    return bool(lines and lines[0].strip() == chat_name.strip())


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(args: list[str], input_bytes: bytes | None = None, timeout: float = 8.0):
    try:
        return subprocess.run(
            args,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UIError("界面命令执行失败", code="command_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command_unavailable",
                      metadata={"command": args[0], "operation": args[1] if len(args) > 1 else ""}) from exc


class Clipboard:
    def __init__(self) -> None:
        if _which("xclip"):
            self.tool = "xclip"
        elif _which("xsel"):
            self.tool = "xsel"
        elif _which("wl-copy") and _which("wl-paste"):
            self.tool = "wl"
        else:
            self.tool = ""

    def available(self) -> bool:
        return bool(self.tool)

    def set_text(self, text: str) -> None:
        data = text.encode("utf-8")
        if self.tool == "xclip":
            proc = _run(["xclip", "-selection", "clipboard", "-i"], data)
        elif self.tool == "xsel":
            proc = _run(["xsel", "--clipboard", "--input"], data)
        elif self.tool == "wl":
            proc = _run(["wl-copy"], data)
        else:
            raise UIError("缺少剪贴板工具。请安装：sudo apt install xclip")
        if proc.returncode != 0:
            raise UIError(f"写剪贴板失败：{proc.stderr.decode('utf-8', 'replace')[:200]}")

    def get_text(self) -> str:
        try:
            if self.tool == "xclip":
                proc = _run(["xclip", "-selection", "clipboard", "-o"])
            elif self.tool == "xsel":
                proc = _run(["xsel", "--clipboard", "--output"])
            elif self.tool == "wl":
                proc = _run(["wl-paste", "--no-newline"])
            else:
                return ""
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", "replace")
        except UIError:
            pass
        return ""


class X11:
    def __init__(self, ui_cfg: dict[str, Any]):
        self.cfg = ui_cfg
        self.available = bool(_which("xdotool"))

    def _xdotool(self, *args: str, timeout: float = 8.0):
        if not self.available:
            raise UIError("缺少 xdotool。请安装：sudo apt install xdotool")
        process = _run(["xdotool", *args], timeout=timeout)
        if process.returncode:
            raise UIError("xdotool 执行失败", code="command_failed",
                          metadata={"command": "xdotool", "operation": args[0], "returncode": process.returncode})
        return process

    def _is_native_window(self, window: str) -> bool:
        try:
            process = self._xdotool("getwindowpid", window)
            pid = int(process.stdout.decode().strip())
            expected = self.cfg.get("native_executable") or "/opt/wechat/wechat"
            return os.path.realpath(f"/proc/{pid}/exe") == os.path.realpath(expected)
        except (UIError, OSError, ValueError):
            return False

    def find_window(self) -> str | None:
        """按窗口类名/标题找微信主窗口。"""
        candidates: list[str] = []
        cls = self.cfg.get("window_class") or "wechat"
        for args in (
            ["search", "--onlyvisible", "--class", cls],
            ["search", "--onlyvisible", "--classname", cls],
        ):
            try:
                proc = self._xdotool(*args)
                if proc.returncode == 0:
                    candidates.extend(proc.stdout.decode().split())
            except UIError:
                pass
        if not candidates:
            for name in self.cfg.get("window_names") or ["微信", "WeChat"]:
                try:
                    proc = self._xdotool("search", "--onlyvisible", "--name", name)
                    if proc.returncode == 0:
                        candidates.extend(proc.stdout.decode().split())
                except UIError:
                    pass
        # 去重，优先最大窗口
        seen: list[str] = []
        for win in candidates:
            if win not in seen and self._is_native_window(win):
                seen.append(win)
        best = None
        best_area = 0
        for win in seen:
            try:
                geo = self._xdotool("getwindowgeometry", "--shell", win)
                if geo.returncode != 0:
                    continue
                vals = {}
                for line in geo.stdout.decode().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        vals[k] = v
                area = int(vals.get("WIDTH", 0)) * int(vals.get("HEIGHT", 0))
                if area > best_area:
                    best_area = area
                    best = win
            except (UIError, ValueError):
                continue
        return best or (seen[0] if seen else None)

    def activate(self) -> str:
        win = self.find_window()
        if not win:
            raise UIError(
                "找不到微信窗口。请确认微信在运行、当前 DISPLAY 正确，"
                "且不是纯 Wayland（可用 QT_QPA_PLATFORM=xcb 启动微信）。"
            )
        self._xdotool("windowactivate", win)
        self._xdotool("windowraise", win)
        time.sleep(0.2)
        active = self._xdotool("getactivewindow").stdout.decode().strip()
        if active != win:
            raise UIError("原生主窗口未激活，取消操作", code="activation_failed")
        return win

    def key(self, combo: str) -> None:
        self._xdotool("key", "--clearmodifiers", combo)

    def click(self, x: int, y: int) -> None:
        self.move_pointer(x, y)
        self._xdotool("click", "1")

    def move_pointer(self, x: int, y: int) -> None:
        # --sync 在鼠标本来就位于目标点时会等待不存在的移动事件，直至超时。
        self._xdotool("mousemove", str(x), str(y))
        values = dict(line.split("=", 1) for line in self._xdotool("getmouselocation", "--shell").stdout.decode().splitlines() if "=" in line)
        if values.get("X") != str(x) or values.get("Y") != str(y):
            raise UIError("鼠标未到达目标位置，取消点击", code="pointer_mismatch")

    def pointer_client(self) -> str | None:
        """XQueryPointer 穿过窗口管理器边框，找鼠标下实际客户端；不读标题。"""
        import ctypes as c
        lib = c.CDLL("libX11.so.6")
        lib.XOpenDisplay.argtypes, lib.XOpenDisplay.restype = [c.c_char_p], c.c_void_p
        lib.XDefaultRootWindow.argtypes, lib.XDefaultRootWindow.restype = [c.c_void_p], c.c_ulong
        lib.XQueryPointer.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                     c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_int),
                                     c.POINTER(c.c_int), c.POINTER(c.c_uint)]
        lib.XQueryPointer.restype = c.c_int
        lib.XCloseDisplay.argtypes = [c.c_void_p]
        display = lib.XOpenDisplay(None)
        if not display:
            raise UIError("无法读取 X11 鼠标命中窗口", code="pointer_unknown")
        owner = None
        try:
            window = lib.XDefaultRootWindow(display)
            for _ in range(32):
                root, child = c.c_ulong(), c.c_ulong()
                rx, ry, wx, wy, mask = c.c_int(), c.c_int(), c.c_int(), c.c_int(), c.c_uint()
                if not lib.XQueryPointer(display, window, c.byref(root), c.byref(child), c.byref(rx),
                                         c.byref(ry), c.byref(wx), c.byref(wy), c.byref(mask)):
                    raise UIError("X11 鼠标不在当前屏幕", code="pointer_unknown")
                try:
                    pid = int(self._xdotool("getwindowpid", str(window)).stdout.decode().strip())
                    if pid > 0:
                        owner = str(window)
                except (UIError, ValueError):
                    pass  # 根窗口/窗口管理器边框无客户端 PID，继续向内查。
                if not child.value:
                    return owner
                window = child.value
            raise UIError("X11 命中窗口层级异常", code="pointer_unknown")
        finally:
            lib.XCloseDisplay(display)

    def checked_click(self, bounds: dict) -> dict:
        progress = {"submitted": False, "stage": "activate"}
        try:
            main = self.activate()
            progress["stage"] = "move_pointer"
            self.move_pointer(bounds["x"] + bounds["width"] // 2, bounds["y"] + bounds["height"] // 2)
            progress["stage"] = "hit_check"
            hit = self.pointer_client()
            if hit != main or not self._is_native_window(hit):
                raise UIError("按钮位置被其他窗口遮挡，取消点击", code="button_obstructed")
            progress.update(stage="click", submitted=None)
            self._xdotool("click", "1")
            return {"hit_window": hit, "hit_native": True, "submitted": True}
        except UIError as exc:
            exc.metadata = {**(exc.metadata or {}), **progress}
            raise

    def type_text(self, text: str) -> None:
        # xdotool 会为不在当前键盘映射里的字符临时绑定 keysym，可输入中文/emoji。
        # 个别应用对动态 keysym 不友好，失败时由调用方回退到剪贴板。
        self._xdotool("type", "--clearmodifiers", "--delay", "12", "--", text)


class A11y:
    """AT-SPI 访问层。导入失败时 available=False，由 Sender 回退到 xdotool。"""

    def __init__(self, ui_cfg: dict[str, Any]):
        self.cfg = ui_cfg
        self.Atspi = None
        self.error = ""
        try:
            import gi  # type: ignore

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # type: ignore

            Atspi.init()
            Atspi.set_timeout(1000, 2000)
            self.Atspi = Atspi
        except Exception as exc:
            self.error = str(exc)

    @property
    def available(self) -> bool:
        return self.Atspi is not None

    # ---------- 查找 ----------
    def find_app(self):
        Atspi = self.Atspi
        if Atspi is None:
            return None
        names = [n.lower() for n in (self.cfg.get("app_names") or ["wechat"])]
        desktop = Atspi.get_desktop(0)
        fallback = None
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                name = (app.get_name() or "").lower()
            except Exception:
                continue
            if name in names or "wechat" in name or "微信" in name:
                return app
            if fallback is None and "wechat" in name:
                fallback = app
        return fallback

    def require_app(self):
        app = self.find_app()
        if app is None:
            raise UIError(
                "AT-SPI 找不到微信应用。请确认：\n"
                "  1) 微信正在当前 DISPLAY 上运行；\n"
                "  2) 已启用无障碍：gsettings set org.gnome.desktop.interface toolkit-accessibility true；\n"
                "  3) 启动微信前设置 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1；\n"
                "  4) 可先运行 python3 -m wxbridge a11y dump --scope desktop 检查。"
            )
        return app

    def _bounds(self, node) -> dict[str, int] | None:
        try:
            comp = node.get_component_iface()
            if comp is None:
                return None
            rect = comp.get_extents(self.Atspi.CoordType.SCREEN)
            if rect.width > 0 and rect.height > 0:
                return {
                    "x": int(rect.x),
                    "y": int(rect.y),
                    "width": int(rect.width),
                    "height": int(rect.height),
                }
        except Exception:
            return None
        return None

    def walk(self, root, max_depth: int = 15, max_nodes: int = 8000):
        count = 0
        stack = [(root, 0)]
        while stack and count < max_nodes:
            node, depth = stack.pop()
            count += 1
            try:
                role = node.get_role_name() or ""
                name = node.get_name() or ""
            except Exception:
                continue
            yield node, role, name, self._bounds(node)
            if depth >= max_depth:
                continue
            try:
                children = [node.get_child_at_index(i) for i in range(node.get_child_count())]
            except Exception:
                continue
            for child in reversed(children):
                if child is not None:
                    stack.append((child, depth + 1))

    def _is_list(self, role: str) -> bool:
        return role in ("list", "table", "tree", "list box")

    def find_list(self, app, names: Iterable[str]):
        wanted = [n.lower() for n in names]
        lists = []
        for node, role, name, bounds in self.walk(app):
            if self._is_list(role):
                lists.append((node, role, name, bounds))
        for node, _role, name, _bounds in lists:
            if (name or "").lower() in wanted:
                return node
        for node, _role, name, _bounds in lists:
            low = (name or "").lower()
            if any(w and w in low for w in wanted):
                return node
        # 回退：按位置/子节点数量猜
        best = None
        best_score = -1
        for node, _role, _name, bounds in lists:
            try:
                n = node.get_child_count()
            except Exception:
                n = 0
            score = n
            if bounds:
                if any(w in ("chats", "聊天", "会话") for w in wanted) and bounds["x"] < 500:
                    score += 100
                if any(w in ("messages", "消息") for w in wanted) and bounds["width"] > 300:
                    score += 100
            if score > best_score:
                best_score = score
                best = node
        return best

    def find_chat_item(self, chat_name: str):
        """在当前可见的列表/搜索结果里找群聊项。"""
        app = self.require_app()
        target = chat_name.strip()
        candidates = []
        for node, role, name, bounds in self.walk(app, max_depth=14):
            if not name or not chat_label_matches(name, target):
                continue
            if role in ("list item", "listitem", "table row", "table cell", "cell", "label", "text", "push button"):
                # 更偏向真正的列表项
                priority = 0 if role in ("list item", "listitem", "table row", "table cell", "cell") else 1
                candidates.append((priority, len(name), node, name, bounds))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2]

    def first_editable(self, app, bottom: bool = False):
        items = []
        for node, role, _name, bounds in self.walk(app):
            if role not in ("text", "entry", "text box", "paragraph", "document text"):
                continue
            if not bounds or bounds["width"] < 60 or bounds["height"] < 12:
                continue
            editable = False
            try:
                state = node.get_state_set()
                editable = state.contains(self.Atspi.StateType.EDITABLE) or state.contains(
                    self.Atspi.StateType.FOCUSABLE
                )
            except Exception:
                editable = role in ("entry", "text box")
            if editable or role in ("entry", "text box"):
                items.append((bounds["y"], node, bounds))
        if not items:
            return None, None
        items.sort(key=lambda x: x[0], reverse=bottom)
        _y, node, bounds = items[0]
        return node, bounds

    # ---------- 操作 ----------
    def click_node(self, node, bounds=None) -> bool:
        bounds = bounds or self._bounds(node)
        if bounds:
            x = bounds["x"] + bounds["width"] // 2
            y = bounds["y"] + bounds["height"] // 2
            try:
                X11(self.cfg).click(x, y)
                return True
            except UIError:
                pass
        try:
            action = node.get_action_iface()
            if action and action.get_n_actions() > 0:
                action.do_action(0)
                return True
        except Exception:
            pass
        return False

    def submit_button(self, button) -> str:
        """动作返回不确定时不再补按回车或二次点击，交给数据库确认。"""
        try:
            action = button.get_action_iface()
            count = action.get_n_actions() if action else 0
            names = [self.Atspi.Action.get_action_name(action, i) or "" for i in range(count)]
        except Exception as exc:
            raise UIError("读取发送按钮动作失败") from exc
        self.last_submit_metadata = {"advertised_actions": names}
        click_actions = {"click", "press", "activate", "点击", "按下", "激活"}
        index = next((i for i, name in enumerate(names) if name.strip().casefold() in click_actions), None)
        if index is not None:
            try:
                if not action.do_action(index):
                    raise UIError("发送按钮动作返回失败")
            except Exception as exc:
                raise UIError("发送按钮动作状态不明") from exc
            self.last_submit_metadata["selected_action"] = names[index]
            return "atspi-action"
        bounds = self._bounds(button)
        if not bounds:
            raise UIError("发送按钮没有动作或可用坐标")
        self.last_submit_metadata.update(X11(self.cfg).checked_click(bounds))
        return "x11-click"

    def focus_metadata(self) -> dict:
        x11 = X11(self.cfg)
        try:
            active = x11._xdotool("getactivewindow").stdout.decode().strip()
            return {"active_native": x11._is_native_window(active),
                    "active_window": active, "main_window": x11.find_window()}
        except UIError:
            return {"active_native": None}

    def clear_authorized_draft(self, expected_chat: str, expected_chars: int) -> dict:
        """仅供这次明确授权的维护脚本调用；长度/群不符即保留，不发送。"""
        if self.chat_is_selected(expected_chat) is not True:
            raise WrongChatError("清空草稿前无法确认目标群")
        node, _ = self.composer()
        draft = self.read_text(node)
        if not draft:
            return {"before_chars": 0, "after_chars": 0}
        if len(draft) != expected_chars:
            raise UIError("现有草稿长度已改变，保留并停止维护")
        X11(self.cfg).activate()
        self.focus_editable(node)
        if not self._atspi_write(node, ""):
            raise UIError("清空草稿失败，未发送")
        after = self.read_text(node)
        if after:
            raise UIError("草稿未清空，未发送")
        return {"before_chars": len(draft), "after_chars": len(after)}

    def focus_editable(self, node, bounds=None) -> bool:
        try:
            comp = node.get_component_iface()
            if comp and comp.grab_focus():
                return True
        except Exception:
            pass
        return self.click_node(node, bounds)

    # ---------- 文本输入（优先不依赖剪贴板） ----------
    def read_text(self, node) -> str:
        try:
            ti = node.get_text_iface()
        except Exception:
            ti = None
        if ti is not None:
            try:
                # GI 同时暴露 Accessible.get_text() 和 Text.get_text(start,end)，
                # 实例调用会撞名，须明确调用 Text 接口；不能用控件名称代替草稿。
                count = self.Atspi.Text.get_character_count(ti)
                return self.Atspi.Text.get_text(ti, 0, count) or "" if count else ""
            except Exception as exc:
                raise UIError("无法从 Text 接口读回文字，取消操作", code="text_read_failed") from exc
        try:
            return node.get_name() or ""
        except Exception:
            return ""

    def _atspi_write(self, node, text: str) -> bool:
        try:
            et = node.get_editable_text_iface()
        except Exception:
            et = None
        if et is None:
            return False
        # 1) setTextContents
        try:
            et.set_text_contents(text)
            return True
        except Exception:
            pass
        # 2) 清空后 insertText；length 按 UTF-8 字节数传
        for length in (len(text.encode("utf-8")), len(text)):
            try:
                ti = node.get_text_iface()
                count = self.Atspi.Text.get_character_count(ti) if ti is not None else 0
                if count:
                    et.delete_text(0, count)
                et.insert_text(0, text, length)
                return True
            except Exception:
                continue
        return False

    def type_into(self, node, text: str, method: str = "auto") -> str:
        """把文本写进输入控件，返回实际使用的方法名。"""
        x11 = X11(self.cfg)
        clip = Clipboard()
        order = ["atspi", "xtype", "clipboard"] if method == "auto" else [method]
        errors = []
        for name in order:
            try:
                if name == "atspi":
                    if not self._atspi_write(node, text):
                        raise UIError("控件没有 EditableText 接口")
                    time.sleep(0.15)
                    got = self.read_text(node)
                    if got.rstrip("\r\n") != text.rstrip("\r\n"):
                        raise UIError("写入后读回内容不一致")
                    return "atspi"
                if name == "xtype":
                    x11.key(self.cfg.get("select_all_shortcut", "ctrl+a"))
                    x11.type_text(text)
                    return "xtype"
                if name == "clipboard":
                    if not clip.available():
                        raise UIError("没有剪贴板工具")
                    clip.set_text(text)
                    x11.key(self.cfg.get("paste_shortcut", "ctrl+v"))
                    return "clipboard"
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise UIError("所有输入方式都失败：" + "；".join(errors))

    def type_focused(self, text: str) -> str:
        """没有可用的 a11y 节点时，往当前焦点控件输入。"""
        x11 = X11(self.cfg)
        method = self.cfg.get("input_method", "auto")
        errors = []
        if method in ("auto", "xtype"):
            try:
                x11.type_text(text)
                return "xtype"
            except Exception as exc:
                errors.append(f"xtype: {exc}")
        try:
            clip = Clipboard()
            if not clip.available():
                raise UIError("没有剪贴板工具")
            clip.set_text(text)
            x11.key(self.cfg.get("paste_shortcut", "ctrl+v"))
            return "clipboard"
        except Exception as exc:
            errors.append(f"clipboard: {exc}")
        raise UIError("当前焦点输入失败：" + "；".join(errors))

    def input_test(self, marker: str = "wxbridge-input-test") -> dict[str, Any]:
        """在输入框里逐个测试输入方式（不会按回车发送）。"""
        app = self.require_app()
        x11 = X11(self.cfg)
        x11.activate()
        node, _bounds = self.composer()
        self.focus_editable(node)
        results: dict[str, Any] = {}
        for method in ("atspi", "xtype", "clipboard"):
            sample = f"{marker}-{method}"
            try:
                x11.key(self.cfg.get("select_all_shortcut", "ctrl+a"))
                x11.key("BackSpace")
            except Exception:
                pass
            try:
                used = self.type_into(node, sample, method)
                time.sleep(0.2)
                readback = self.read_text(node)
                results[method] = {
                    "ok": sample in readback,
                    "used": used,
                    "readback": readback[:160],
                }
            except Exception as exc:
                results[method] = {"ok": False, "error": str(exc)}
            try:
                x11.key(self.cfg.get("select_all_shortcut", "ctrl+a"))
                x11.key("BackSpace")
            except Exception:
                pass
        return results

    def select_chat(self, chat_name: str) -> None:
        app = self.require_app()
        x11 = X11(self.cfg)
        try:
            x11.activate()
        except UIError:
            pass
        item = self.find_chat_item(chat_name)
        if item is not None:
            self.click_node(item)
            time.sleep(self.cfg.get("chat_open_wait", 1.0))
            return

        # 不在当前列表：用搜索。先按 Ctrl+F 聚焦搜索框，避免误点输入框。
        x11 = X11(self.cfg)
        x11.activate()
        x11.key(self.cfg.get("search_shortcut", "ctrl+f"))
        time.sleep(0.25)
        x11.key(self.cfg.get("select_all_shortcut", "ctrl+a"))
        self.type_focused(chat_name)
        time.sleep(0.9)

        item = self.find_chat_item(chat_name)
        if item is not None:
            self.click_node(item)
        else:
            x11.key("Return")
        time.sleep(self.cfg.get("chat_open_wait", 1.0))

    def composer(self):
        editor, _send = self.composer_controls()
        return editor, self._bounds(editor)

    def composer_controls(self):
        """用可编辑状态及相邻发送按钮确认输入框，不把搜索框/消息当作编辑器。"""
        app = self.require_app()
        editors, buttons = [], []
        for node, role, name, bounds in self.walk(app, max_depth=35):
            if not bounds:
                continue
            if role in ("text", "entry", "text box", "paragraph"):
                try:
                    state = node.get_state_set()
                    if (state.contains(self.Atspi.StateType.EDITABLE)
                            and state.contains(self.Atspi.StateType.VISIBLE)):
                        editors.append((node, bounds))
                except Exception:
                    continue
            if role in ("button", "push button", "push-button") and name in (
                "Send", "Send(S)", "发送", "发送(S)", "发送（S）"
            ):
                buttons.append((node, bounds))
        pairs = []
        for editor, e in editors:
            for button, b in buttons:
                gap = b['y'] - (e['y'] + e['height'])
                if (-8 <= gap <= 120 and b['x'] < e['x'] + e['width']
                        and b['x'] + b['width'] > e['x']):
                    pairs.append((editor, button))
        if len(pairs) != 1:
            raise UIError("无法唯一确认聊天编辑器及发送按钮，已取消操作")
        return pairs[0]

    def read_chat_list_items(self, max_items: int = 80) -> list[dict[str, Any]]:
        """读取聊天列表项（含目标群的未读/预览/时间文本）。"""
        app = self.require_app()
        node = self.find_list(app, self.cfg.get("chat_list_names") or ["Chats"])
        if node is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            count = node.get_child_count()
        except Exception:
            return []
        for i in range(max(0, count - max_items), count):
            try:
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                raw = child.get_name() or ""
                if raw.strip():
                    out.append(
                        {
                            "index": i,
                            "raw": raw,
                            "role": child.get_role_name(),
                            "bounds": self._bounds(child),
                        }
                    )
            except Exception:
                continue
        return out

    def read_message_items(self, max_items: int = 80) -> list[dict[str, Any]]:
        """读取当前打开聊天的消息列表（只含可见项）。"""
        app = self.require_app()
        node = self.find_list(app, self.cfg.get("messages_list_names") or ["Messages"])
        if node is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            count = node.get_child_count()
        except Exception:
            return []
        start = max(0, count - max_items)
        for i in range(start, count):
            try:
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                raw = child.get_name() or ""
                if not raw.strip():
                    continue
                out.append(
                    {
                        "index": i,
                        "raw": raw,
                        "role": child.get_role_name(),
                        "bounds": self._bounds(child),
                    }
                )
            except Exception:
                continue
        return out

    def chat_is_selected(self, chat_name: str) -> bool | None:
        """尽力判断目标群是否处于选中状态。

        True/False = 找到了目标项且能判断；None = 找不到或接口不支持。
        """
        app = self.require_app()
        node = self.find_list(app, self.cfg.get("chat_list_names") or ["Chats"])
        if node is None:
            return None
        matches = []
        try:
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                name = child.get_name() or ""
                if not chat_label_matches(name, chat_name):
                    continue
                matches.append(child)
        except Exception:
            return None
        if len(matches) != 1:
            return None  # 同名群或无法唯一匹配，不猜测发送目标
        try:
            state = matches[0].get_state_set()
            return bool(state.contains(self.Atspi.StateType.SELECTED))
        except Exception:
            return None

    def prepare_text(self, text: str) -> str:
        """只输入、读回核对；不发送，且不覆盖用户已有的不同草稿。"""
        x11 = X11(self.cfg)
        x11.activate()
        node, bounds = self.composer()
        existing = self.read_text(node).rstrip("\r\n")
        if existing and existing != text.rstrip("\r\n"):
            raise UIError("输入框已有不同草稿，取消操作", code="draft_conflict")
        self.focus_editable(node, bounds)
        time.sleep(0.15)
        x11.key(self.cfg.get("select_all_shortcut", "ctrl+a"))
        used = self.type_into(node, text, self.cfg.get("input_method", "auto"))
        time.sleep(0.2)
        if self.read_text(node).rstrip("\r\n") != text.rstrip("\r\n"):
            raise UIError("输入后读回文字不一致，已取消发送")
        return used

    def send_text(self, text: str, expected_chat: str | None = None) -> dict:
        try:
            used = self.prepare_text(text)
        except UIError as exc:
            exc.metadata = {**(exc.metadata or {}), "stage": "prepare", "submitted": False}
            raise
        if expected_chat and self.chat_is_selected(expected_chat) is not True:
            raise WrongChatError("发送前无法再次确认目标群，保留草稿并取消发送")
        node, button = self.composer_controls()
        if self.read_text(node).rstrip("\r\n") != text.rstrip("\r\n"):
            raise UIError("发送前草稿发生变化，取消发送")
        time.sleep(0.3)
        before_focus = self.focus_metadata()
        action = self.submit_button(button)
        time.sleep(self.cfg.get("post_send_wait", 0.8))
        try:
            remaining = self.read_text(node)
        except UIError as exc:
            exc.metadata = {"input_method": used, "action": action,
                            "before_chars": len(text), "after_chars": None,
                            "focus_before": before_focus, "focus_after": self.focus_metadata(),
                            **getattr(self, "last_submit_metadata", {})}
            raise
        return {"input_method": used, "action": action,
                "before_chars": len(text), "after_chars": len(remaining),
                "focus_before": before_focus, "focus_after": self.focus_metadata(),
                **getattr(self, "last_submit_metadata", {})}

    # ---------- 诊断 ----------
    def dump(self, scope: str = "chats", max_depth: int = 8) -> dict[str, Any]:
        if not self.available:
            return {"error": f"AT-SPI 不可用：{self.error}"}
        app = self.find_app()
        if app is None:
            return {"error": "未找到微信应用", "apps": self._desktop_apps()}
        if scope == "desktop":
            root = self.Atspi.get_desktop(0)
            return self._dump_tree(root, max_depth)
        if scope == "chats":
            node = self.find_list(app, self.cfg.get("chat_list_names") or ["Chats"])
            if node is None:
                return {"error": "未找到聊天列表", "tree": self._dump_tree(app, 6)}
            return self._dump_list(node)
        if scope == "messages":
            node = self.find_list(app, self.cfg.get("messages_list_names") or ["Messages"])
            if node is None:
                return {"error": "未找到消息列表", "tree": self._dump_tree(app, 6)}
            return self._dump_list(node)
        if scope == "editables":
            items = []
            for node, role, name, bounds in self.walk(app):
                if role in ("text", "entry", "text box", "paragraph", "document text") and bounds:
                    items.append({"role": role, "name": name, "bounds": bounds})
            return {"items": items}
        return self._dump_tree(app, max_depth)

    def _desktop_apps(self) -> list[str]:
        out = []
        try:
            desktop = self.Atspi.get_desktop(0)
            for i in range(desktop.get_child_count()):
                out.append(desktop.get_child_at_index(i).get_name())
        except Exception:
            pass
        return out

    def _dump_list(self, node) -> dict[str, Any]:
        items = []
        try:
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                items.append(
                    {
                        "index": i,
                        "role": child.get_role_name(),
                        "name": child.get_name() or "",
                        "bounds": self._bounds(child),
                    }
                )
        except Exception as exc:
            return {"error": str(exc), "items": items}
        return {"list_name": node.get_name(), "items": items}

    def _dump_tree(self, root, max_depth: int) -> dict[str, Any]:
        items = []
        for node, role, name, bounds in self.walk(root, max_depth=max_depth, max_nodes=3000):
            items.append({"role": role, "name": name, "bounds": bounds})
        return {"items": items}


class Sender:
    """选择 AT-SPI 优先、xdotool 兜底的发送器。"""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.ui_cfg = cfg["ui"]
        self.x11 = X11(self.ui_cfg)
        self.a11y = A11y(self.ui_cfg)
        self.clipboard = Clipboard()
        backend = self.ui_cfg.get("backend", "auto")
        if backend == "atspi":
            self.backend = "atspi"
        elif backend == "xdotool":
            self.backend = "xdotool"
        else:
            self.backend = "atspi" if self.a11y.available else "xdotool"
        self.dry_run = bool(self.ui_cfg.get("dry_run"))

    def status(self) -> dict[str, Any]:
        win = None
        win_error = ""
        if self.x11.available:
            try:
                win = self.x11.find_window()
            except UIError as exc:
                win_error = str(exc)
        return {
            "backend": self.backend,
            "input_method": self.ui_cfg.get("input_method", "auto"),
            "xdotool": self.x11.available,
            "atspi": self.a11y.available,
            "atspi_error": self.a11y.error,
            "clipboard": self.clipboard.tool or None,
            "window_id": win,
            "window_error": win_error,
            "dry_run": self.dry_run,
            "display": __import__("os").environ.get("DISPLAY", ""),
            "wayland": __import__("os").environ.get("WAYLAND_DISPLAY", ""),
        }

    def _send_xdotool(self, chat: str, text: str) -> dict:
        x11 = self.x11
        x11.activate()
        time.sleep(0.2)
        x11.key(self.ui_cfg.get("search_shortcut", "ctrl+f"))
        time.sleep(0.3)
        x11.key(self.ui_cfg.get("select_all_shortcut", "ctrl+a"))
        self._type_or_paste(chat)
        time.sleep(0.8)
        x11.key("Return")  # 打开搜索结果
        time.sleep(self.ui_cfg.get("chat_open_wait", 1.0))
        if self.ui_cfg.get("verify_chat_selected", True):
            if not self.a11y.available or self.a11y.chat_is_selected(chat) is not True:
                raise WrongChatError("无法确认搜索结果就是目标群，已取消输入和发送")
        if not self.a11y.available:
            raise UIError("无法检查草稿，取消发送")
        used = self.a11y.prepare_text(text)
        if self.a11y.chat_is_selected(chat) is not True:
            raise WrongChatError("提交前目标群已改变")
        node, _ = self.a11y.composer()
        before_focus = self.a11y.focus_metadata()
        time.sleep(0.3)
        x11.key(self.ui_cfg.get("send_key", "Return"))
        time.sleep(self.ui_cfg.get("post_send_wait", 0.8))
        return {"input_method": used, "action": "x11-key", "before_chars": len(text),
                "after_chars": len(self.a11y.read_text(node)),
                "focus_before": before_focus, "focus_after": self.a11y.focus_metadata()}

    def _type_or_paste(self, text: str) -> str:
        """无 a11y 节点时的输入：先试 xdotool type，再回退剪贴板。"""
        method = self.ui_cfg.get("input_method", "auto")
        if method in ("auto", "xtype"):
            try:
                self.x11.type_text(text)
                return "xtype"
            except Exception:
                pass
        self.clipboard.set_text(text)
        self.x11.key(self.ui_cfg.get("paste_shortcut", "ctrl+v"))
        return "clipboard"

    def send(self, chat: str, text: str) -> dict | None:
        if self.dry_run:
            print(f"[dry-run] 目标已指定，回复字符数={len(text)}；未发送", flush=True)
            return
        if self.backend == "atspi":
            try:
                self.a11y.select_chat(chat)
                if self.ui_cfg.get("verify_chat_selected", True):
                    selected = self.a11y.chat_is_selected(chat)
                    if selected is not True:
                        raise WrongChatError("无法确认目标群处于选中状态")
                return self.a11y.send_text(text, expected_chat=chat)
            except WrongChatError as exc:
                raise UIError(
                    f"{exc}；已取消发送，避免发到错误的群。"
                    "请检查群名是否唯一，或先手动打开目标群再试。", code="wrong_chat"
                ) from exc
            except Exception as exc:
                if self.ui_cfg.get("verify_chat_selected", True):
                    raise UIError("AT-SPI 发送状态不确定，取消自动重试；请检查界面",
                                  code=getattr(exc, "code", "ui_unknown"),
                                  metadata=getattr(exc, "metadata", None)) from exc
                print(f"[ui] AT-SPI 发送失败，回退 xdotool：{exc}", flush=True)
        return self._send_xdotool(chat, text)

    def input_test(self) -> dict[str, Any]:
        if not self.a11y.available:
            return {"error": f"AT-SPI 不可用：{self.a11y.error}"}
        return self.a11y.input_test()

    def dump(self, scope: str = "chats", max_depth: int = 8) -> dict[str, Any]:
        return self.a11y.dump(scope, max_depth)
