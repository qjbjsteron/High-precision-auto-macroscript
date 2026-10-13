"""
脚本共享运行库。

集中存放高精度计时、键鼠输入封装与 Windows 精度设置。各个
script_x.py 脚本文件直接从这里导入需要的函数即可，无需关心
test_aut.py 的调度逻辑。
"""

import asyncio
import ctypes
import sys
import time

from pynput.keyboard import Controller as KeyboardController
from pynput.mouse import Button, Controller as MouseController


# ===================== 全局配置 =====================
# 常用按键常量，脚本里可直接使用。
E = "e"
H = "h"
Q = "q"
One = "1"
Two = "2"
Three = "3"
Four = "4"

# 动作默认值
DEFAULT_TAP_DURATION = 0.01
WINDOWS_HIGH_PRIORITY = True
WINDOWS_TIMER_PERIOD_MS = 1


keyboard_controller = KeyboardController()
mouse_controller = MouseController()


# ===================== Windows 精度设置 =====================
_windows_timer_period_enabled = False


def enable_windows_precision():
    """启用 Windows 高优先级和最小定时器周期。可重复调用。"""
    global _windows_timer_period_enabled
    if sys.platform != "win32" or _windows_timer_period_enabled:
        return

    try:
        ctypes.windll.winmm.timeBeginPeriod(WINDOWS_TIMER_PERIOD_MS)
        _windows_timer_period_enabled = True
    except Exception:
        pass

    if WINDOWS_HIGH_PRIORITY:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000080)
        except Exception:
            pass


def disable_windows_precision():
    global _windows_timer_period_enabled
    if sys.platform != "win32" or not _windows_timer_period_enabled:
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(WINDOWS_TIMER_PERIOD_MS)
    except Exception:
        pass
    _windows_timer_period_enabled = False


enable_windows_precision()


# ===================== 高精度计时 =====================
# test_aut.py 通过 set_cancel_checker 注册取消判断；脚本单独运行时
# 未注册检查器，只响应 asyncio 任务取消。
_cancel_checker = None


def set_cancel_checker(checker):
    """注册一个无参回调，返回 True 时精确延时立即抛出 CancelledError。"""
    global _cancel_checker
    _cancel_checker = checker


async def precise_sleep(duration):
    """支持取消的忙等延时，精度可达亚毫秒级。"""
    if duration <= 0:
        return

    deadline = time.perf_counter_ns() + int(duration * 1_000_000_000)
    task = asyncio.current_task()
    checker = _cancel_checker

    while True:
        if time.perf_counter_ns() >= deadline:
            return

        if task is not None and task.cancelling():
            raise asyncio.CancelledError

        if checker is not None and checker():
            raise asyncio.CancelledError


# ===================== 键鼠输入封装 =====================
class InputManager:
    """记录输入按下状态，保证动作取消或异常时能够释放所有按键。"""

    def __init__(self):
        self._pressed_keys = {}
        self._pressed_buttons = {}

    def press_key(self, key):
        count = self._pressed_keys.get(key, 0)
        if count == 0:
            keyboard_controller.press(key)
        self._pressed_keys[key] = count + 1

    def release_key(self, key):
        count = self._pressed_keys.get(key, 0)
        if count <= 0:
            return
        if count == 1:
            keyboard_controller.release(key)
            self._pressed_keys.pop(key, None)
        else:
            self._pressed_keys[key] = count - 1

    def press_mouse(self, button=Button.left):
        count = self._pressed_buttons.get(button, 0)
        if count == 0:
            mouse_controller.press(button)
        self._pressed_buttons[button] = count + 1

    def release_mouse(self, button=Button.left):
        count = self._pressed_buttons.get(button, 0)
        if count <= 0:
            return
        if count == 1:
            mouse_controller.release(button)
            self._pressed_buttons.pop(button, None)
        else:
            self._pressed_buttons[button] = count - 1

    def release_all(self):
        for key in list(self._pressed_keys):
            try:
                keyboard_controller.release(key)
            except Exception:
                pass
        for button in list(self._pressed_buttons):
            try:
                mouse_controller.release(button)
            except Exception:
                pass
        self._pressed_keys.clear()
        self._pressed_buttons.clear()

    async def key_tap(self, key, hold=DEFAULT_TAP_DURATION, after=0.0):
        self.press_key(key)
        try:
            await precise_sleep(hold)
        finally:
            self.release_key(key)
        await precise_sleep(after)

    async def key_hold(self, key, duration, after=0.0):
        await self.key_tap(key, hold=duration, after=after)

    async def key_combo(self, *keys, hold=DEFAULT_TAP_DURATION, after=0.0):
        pressed = []
        try:
            for key in keys:
                self.press_key(key)
                pressed.append(key)
            await precise_sleep(hold)
        finally:
            for key in reversed(pressed):
                self.release_key(key)
        await precise_sleep(after)

    async def mouse_click(self, button=Button.left, hold=DEFAULT_TAP_DURATION, after=0.0):
        await self.mouse_hold(button, duration=hold, after=after)

    async def mouse_hold(self, button=Button.left, duration=0.01, after=0.0):
        self.press_mouse(button)
        try:
            await precise_sleep(duration)
        finally:
            self.release_mouse(button)
        await precise_sleep(after)


input_manager = InputManager()


# 脚本中可直接使用的简写函数。
async def key_tap(key, hold=DEFAULT_TAP_DURATION, after=0.0):
    await input_manager.key_tap(key, hold=hold, after=after)


async def key_hold(key, duration, after=0.0):
    await input_manager.key_hold(key, duration, after=after)


async def key_combo(*keys, hold=DEFAULT_TAP_DURATION, after=0.0):
    await input_manager.key_combo(*keys, hold=hold, after=after)


async def mouse_click(button=Button.left, hold=DEFAULT_TAP_DURATION, after=0.0):
    await input_manager.mouse_click(button=button, hold=hold, after=after)


async def mouse_hold(button=Button.left, duration=0.01, after=0.0):
    await input_manager.mouse_hold(button=button, duration=duration, after=after)


def key_down(key):
    input_manager.press_key(key)


def key_up(key):
    input_manager.release_key(key)


def mouse_down(button=Button.left):
    input_manager.press_mouse(button)


def mouse_up(button=Button.left):
    input_manager.release_mouse(button)
