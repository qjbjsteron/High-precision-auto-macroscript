import asyncio
import ctypes
import gc
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import cv2
import mss
import numpy as np
from pynput.keyboard import Listener as KeyboardListener


import script_runtime
from script_runtime import (
    disable_windows_precision,
    enable_windows_precision,
    input_manager,
    precise_sleep,
    set_cancel_checker,
)


# ===================== 脚本调用配置（集中修改区） =====================
# 按键触发、顺序检测调用哪个脚本在这里设置；
# 逻辑检测模式的脚本在其专属的【逻辑检测配置】区随逻辑规则一起设置。
#
# 约定：每个 script_x.py 都提供 async def run()。
# 新增脚本步骤：
#   1) 复制 script_1.py 改名为 script_3.py，编写 async def run()
#   2) 在下方导入该模块
#   3) 把对应功能的脚本改成 script_3.run
#########################
import script_1
import script_2

# 功能 1：按键触发模式使用的脚本
KEY_TRIGGER_SCRIPT = script_1.run

# 功能 3：图像顺序检测 —— 检测器名称 -> 脚本（步骤顺序见下方 SEQUENCE_STEPS）
SEQUENCE_TRIGGER_SCRIPTS = {
    "步骤1": script_1.run,
    "步骤2": script_2.run,
}


def _lookup_script(script_map, detector_name, mode_name):
    """按检测器名称取出脚本，未绑定时报错并提示去哪里修改。"""
    script = script_map.get(detector_name)
    if script is None:
        raise KeyError(
            f"{mode_name}检测器 '{detector_name}' 未绑定脚本，"
            f"请在【脚本调用配置（集中修改区）】中补充。"
        )
    return script


# ===================== 全局配置 =====================
# 常用按键常量与动作默认值位于 script_runtime.py，脚本文件从那里导入。

# 可修改热键
STOP_HOTKEY = "x"                # 停止热键，所有模式有效
DEBUG_SNAPSHOT_HOTKEY = "p"      # 手动截图调试热键，所有模式有效
TRIGGER_HOTKEY = "c"             # 按键触发模式下的执行热键

TRIGGER_LOOP_DELAY = 0.1         # 按住循环模式下，两次脚本执行之间的间隔

# 按键触发模式
KEY_MODE_FULL = "full"
KEY_MODE_HOLD = "hold"
KEY_MODE_HOLD_LOOP = "hold_loop"

# 图片检测配置
DETECTION_INTERVAL = 0.01        # 每次检测之间的间隔，越小响应越快、CPU 占用越高
# 命中后执行脚本期间会暂停所有检测，脚本执行完再恢复，以节省 CPU。
REFERENCE_RESOLUTION = (1920, 1080)
AUTO_SCALE_REGIONS = True        # 按当前屏幕尺寸缩放原始坐标
AUTO_EXPAND_REGION = True        # 检测区域过小时自动扩展，避免小于模板
REGION_EXPAND_MARGIN = 6         # 自动扩展区域时额外增加的像素边距

SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_SCREENSHOT_DIR = SCRIPT_DIR / "debug_screenshots"

# OpenCV 对多个小区域匹配时使用单线程更稳定，避免线程过度调度。
cv2.setUseOptimized(True)
cv2.setNumThreads(1)

stop_event = threading.Event()
running_tasks = set()
main_loop = None
current_mode = None
key_run_mode = KEY_MODE_FULL
trigger_key_is_down = False
active_hold_task = None

# 检测任务固定放在一个工作线程中执行，复用 MSS 会话。
detection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-detector")
screen_capture = None
action_lock = None


def _action_cancel_requested():
    """停止热键按下，或按住模式松开触发键时，让精确延时立即取消当前动作。"""
    if stop_event.is_set():
        return True
    return (
        current_mode == "key"
        and key_run_mode in (KEY_MODE_HOLD, KEY_MODE_HOLD_LOOP)
        and not trigger_key_is_down
    )


set_cancel_checker(_action_cancel_requested)


def enable_dpi_awareness():
    """让截图坐标与系统鼠标坐标位于同一 DPI 坐标系。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()


enable_windows_precision()


def get_virtual_screen_size():
    """返回包括多显示器在内的虚拟桌面尺寸。"""
    with mss.MSS() as sct:
        monitor = sct.monitors[0]
        return int(monitor["width"]), int(monitor["height"])


ACTUAL_SCREEN_RESOLUTION = get_virtual_screen_size()


# ===================== 任务管理 =====================
async def run_action(action):
    """串行执行动作，并在任何退出路径上释放残留的键鼠输入。"""
    global action_lock
    if action_lock is None:
        action_lock = asyncio.Lock()

    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()

    try:
        async with action_lock:
            try:
                await action
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                print(f"[错误] 执行操作失败: {ex}")
            finally:
                input_manager.release_all()
    finally:
        if gc_was_enabled:
            gc.enable()


def create_action_task(action_coro):
    task = asyncio.create_task(run_action(action_coro))
    running_tasks.add(task)
    task.add_done_callback(running_tasks.discard)
    return task


def cancel_running_actions():
    for task in list(running_tasks):
        if not task.done():
            task.cancel()


async def wait_for_running_actions():
    """等待所有正在执行的脚本任务结束。

    检测循环命中并触发脚本后会调用本函数：脚本执行期间不做任何检测，
    执行完毕后自动恢复，从而在低检测间隔下节省 CPU。
    """
    while True:
        tasks = [task for task in running_tasks if not task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def cancel_running_actions_and_wait():
    tasks = [task for task in running_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ===================== 屏幕捕获与调试截图 =====================
class ScreenCapture:
    """每个工作线程复用一个 MSS 实例，避免每帧重复创建捕获会话。"""

    def __init__(self):
        self._local = threading.local()

    def _get_session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = mss.MSS()
            self._local.session = session
        return session

    def grab_bgra(self, region):
        x, y, w, h = region
        monitor = {"top": y, "left": x, "width": w, "height": h}
        shot = self._get_session().grab(monitor)
        return np.asarray(shot)

    def grab_gray(self, region):
        frame = self.grab_bgra(region)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)


screen_capture = ScreenCapture()


def _snapshot_region(detector):
    image = screen_capture.grab_bgra(detector.region)
    DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    x, y, w, h = detector.region
    region_str = f"{x}-{y}-{w}x{h}"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in detector.name)
    filepath = DEBUG_SCREENSHOT_DIR / f"snapshot_{safe_name}_{region_str}_{timestamp}.png"
    success, encoded = cv2.imencode(".png", image[:, :, :3])
    if not success:
        raise OSError(f"无法保存截图: {filepath}")
    filepath.write_bytes(encoded.tobytes())
    return filepath


def _collect_all_detectors():
    detectors = []
    seen = set()
    for item in LOGIC_CONFIG.values():
        detector = item["detector"]
        if id(detector) not in seen:
            seen.add(id(detector))
            detectors.append(detector)
    for step in SEQUENCE_STEPS:
        detector = step["detector"]
        if id(detector) not in seen:
            seen.add(id(detector))
            detectors.append(detector)
    return detectors


async def capture_all_regions():
    detectors = _collect_all_detectors()
    if not detectors:
        print("[调试截图] 未找到任何检测器，跳过。")
        return

    print(f"[调试截图] 共 {len(detectors)} 个检测器，开始截图...")
    for detector in detectors:
        try:
            filepath = _snapshot_region(detector)
            print(f"[调试截图] {detector.name} (区域 {detector.region}) -> {filepath}")
        except Exception as ex:
            print(f"[调试截图] 检测器 {detector.name} 截图失败: {ex}")
    print("[调试截图] 全部完成。")


# ===================== 操作脚本 =====================
# 每个动作脚本独立存放在 script_x.py 中，入口统一为 async def run()。
# 具体哪个功能调用哪个脚本，见文件顶部【脚本调用配置（集中修改区）】。


# ===================== 图像检测 =====================
def scale_region(region):
    if not AUTO_SCALE_REGIONS:
        return tuple(region)

    ref_w, ref_h = REFERENCE_RESOLUTION
    actual_w, actual_h = ACTUAL_SCREEN_RESOLUTION
    if ref_w <= 0 or ref_h <= 0:
        return tuple(region)

    scale_x = actual_w / ref_w
    scale_y = actual_h / ref_h
    x, y, w, h = region
    return (
        round(x * scale_x),
        round(y * scale_y),
        max(1, round(w * scale_x)),
        max(1, round(h * scale_y)),
    )


def prepare_region(region, minimum_size):
    x, y, w, h = scale_region(region)
    min_w, min_h = minimum_size

    if AUTO_EXPAND_REGION and (w < min_w or h < min_h):
        extra_w = max(0, min_w - w) + REGION_EXPAND_MARGIN * 2
        extra_h = max(0, min_h - h) + REGION_EXPAND_MARGIN * 2
        x -= extra_w // 2
        y -= extra_h // 2
        w += extra_w
        h += extra_h

    screen_w, screen_h = ACTUAL_SCREEN_RESOLUTION
    w = min(w, screen_w)
    h = min(h, screen_h)
    x = max(0, min(x, screen_w - w))
    y = max(0, min(y, screen_h - h))
    return x, y, w, h


class Detector:
    def __init__(self, name, template_path, region, threshold, scales=None):
        self.name = name
        self.source_region = tuple(region)
        self.threshold = float(threshold)
        self.scales = tuple(scales or (1.0,))
        if not self.scales or any(scale <= 0 for scale in self.scales):
            raise ValueError(f"检测器 '{name}' 的 scales 必须包含正数。")

        template_file = Path(template_path)
        if not template_file.is_absolute():
            template_file = SCRIPT_DIR / template_file
        template_data = np.fromfile(str(template_file), dtype=np.uint8)
        template = cv2.imdecode(template_data, cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise FileNotFoundError(f"无法读取模板图片: {template_file}")
        self.template = template
        self._template_cache = {}

        ref_w, ref_h = REFERENCE_RESOLUTION
        actual_w, actual_h = ACTUAL_SCREEN_RESOLUTION
        if AUTO_SCALE_REGIONS and ref_w > 0 and ref_h > 0:
            self._base_scale_x = actual_w / ref_w
            self._base_scale_y = actual_h / ref_h
        else:
            self._base_scale_x = 1.0
            self._base_scale_y = 1.0

        max_scale = max(self.scales)
        template_h, template_w = template.shape[:2]
        minimum_size = (
            max(1, round(template_w * self._base_scale_x * max_scale)),
            max(1, round(template_h * self._base_scale_y * max_scale)),
        )
        scaled_x, scaled_y, scaled_w, scaled_h = scale_region(self.source_region)
        screen_w, screen_h = ACTUAL_SCREEN_RESOLUTION
        if (
            scaled_x < 0
            or scaled_y < 0
            or scaled_x + scaled_w > screen_w
            or scaled_y + scaled_h > screen_h
        ):
            print(
                f"[配置警告] 检测器 '{name}' 的原始区域 {self.source_region} "
                f"超出参考屏幕范围，初始化时会自动裁剪到屏幕内。"
            )
        self.region = prepare_region(self.source_region, minimum_size)
        self.last_score = 0.0

    def _get_template(self, scale):
        if scale in self._template_cache:
            return self._template_cache[scale]

        template_h, template_w = self.template.shape[:2]
        width = max(1, round(template_w * self._base_scale_x * scale))
        height = max(1, round(template_h * self._base_scale_y * scale))
        shrinking = width < template_w or height < template_h
        interpolation = cv2.INTER_AREA if shrinking else cv2.INTER_CUBIC
        resized = cv2.resize(self.template, (width, height), interpolation=interpolation)
        self._template_cache[scale] = resized
        return resized

    def check(self):
        image = screen_capture.grab_gray(self.region)
        image_h, image_w = image.shape[:2]
        best_score = 0.0

        for scale in self.scales:
            template = self._get_template(scale)
            template_h, template_w = template.shape[:2]
            if image_h < template_h or image_w < template_w:
                continue

            result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
            _, max_score, _, _ = cv2.minMaxLoc(result)
            best_score = max(best_score, max_score)
            if best_score >= self.threshold:
                break

        self.last_score = best_score
        return best_score >= self.threshold


def check_detectors(detectors):
    results = []
    for detector in detectors:
        try:
            results.append(detector.check())
        except Exception as ex:
            print(f"[检测错误] {detector.name}: {ex}")
            results.append(False)
    return results


# ===================== 检测配置 =====================
# 原坐标按 REFERENCE_RESOLUTION 记录；脚本运行时会按当前屏幕自动缩放。
# 顺序检测：这里只配置“检测什么”；“匹配后调用哪个脚本”由顶部【脚本调用配置】决定。
SEQUENCE_STEPS = [
    {
        "detector": Detector("步骤1", "test1.png", (1564, 958, 30, 20), 0.8),
        "cooldown": 2.0,
    },
    {
        "detector": Detector("步骤2", "test2.png", (1076, 538, 40, 30), 0.75),
        "cooldown": 1.5,
    },
]


# ===================== 逻辑检测配置（集中修改区） =====================
# 逻辑检测的日常修改通常只需要改下面两个字典，不必修改逻辑循环代码。
# 常见修改位置：
#   - 改某个检测器命中后的流程：修改 LOGIC_CONFIG 中它的 actions。
#   - 新增一组开关组合：在 LOGIC_ACTION_MODULES 中新增一个模块。
#
# 第一层：逻辑动作模块。
# 每个模块是一组可复用的开启/关闭操作。enable / disable 填检测器名称，
# 数量不限；列表可以省略或设为空。
LOGIC_ACTION_MODULES = {
    "切换到2": {
        "disable": ["1"],
        "enable": ["2"],
    },
    "切换到3": {
        "disable": ["2"],
        "enable": ["3"],
    },
    "切换到1": {
        "disable": ["3"],
        "enable": ["1"],
    },
    # 示例：一个模块也可以同时开关多个检测器。
    # "关闭1并打开2和3": {
    #     "disable": ["1"],
    #     "enable": ["2", "3"],
    # },
}


# 第二层：检测器与触发逻辑。
# 字典的键就是检测器名称，也是逻辑动作模块引用的名称。
# actions 按列表顺序执行，可以填写多个动作模块；空白列表表示命中后不切换检测器。
# script=None 表示命中后不执行脚本，只执行 actions。
LOGIC_CONFIG = {
    "1": {
        "detector": Detector("1", "test1.png", (1564, 958, 30, 20), 0.8),
        "cooldown": 2.0,
        "initial": True,
        "script": script_1.run,
        "actions": ["切换到2"],
    },
    "2": {
        "detector": Detector("2", "test2.png", (1076, 538, 40, 30), 0.75),
        "cooldown": 2.0,
        "initial": False,
        "script": script_2.run,
        "actions": ["切换到3"],
    },
    "3": {
        "detector": Detector("3", "test1.png", (1564, 958, 30, 20), 0.8),
        "cooldown": 2.0,
        "initial": False,
        "script": None,
        "actions": ["切换到1"],
    },
}


def _validate_logic_config():
    """检查逻辑动作模块、检测器引用和初始状态是否合法。"""
    known_detectors = set(LOGIC_CONFIG)

    for module_name, module in LOGIC_ACTION_MODULES.items():
        if not isinstance(module, dict):
            raise TypeError(f"逻辑动作模块 '{module_name}' 必须是字典。")
        for key in ("enable", "disable"):
            targets = module.get(key, [])
            if not isinstance(targets, (list, tuple)):
                raise TypeError(
                    f"逻辑动作模块 '{module_name}' 的 {key} 必须是列表。"
                )
            for target in targets:
                if target not in known_detectors:
                    raise ValueError(
                        f"逻辑动作模块 '{module_name}' 的 {key} "
                        f"引用了不存在的检测器 '{target}'。"
                    )

    for detector_name, item in LOGIC_CONFIG.items():
        actions = item.get("actions", [])
        if not isinstance(actions, (list, tuple)):
            raise TypeError(f"检测器 '{detector_name}' 的 actions 必须是列表。")
        for module_name in actions:
            if module_name not in LOGIC_ACTION_MODULES:
                raise ValueError(
                    f"检测器 '{detector_name}' 的 actions 引用了不存在的"
                    f"逻辑动作模块 '{module_name}'。"
                )

    if LOGIC_CONFIG and not any(item.get("initial") for item in LOGIC_CONFIG.values()):
        raise ValueError("逻辑检测配置：至少要有一个检测器的 initial=True。")


_validate_logic_config()


def _apply_logic_action_modules(module_names, states):
    """依次执行逻辑动作模块，返回本次明确关闭、开启的检测器名称。"""
    disabled_targets = []
    enabled_targets = []
    for module_name in module_names:
        module = LOGIC_ACTION_MODULES[module_name]
        for target in module.get("disable", []):
            states[target]["enabled"] = False
            disabled_targets.append(target)
        for target in module.get("enable", []):
            states[target]["enabled"] = True
            enabled_targets.append(target)
    return disabled_targets, enabled_targets


def _bind_scripts(steps, script_map, mode_name):
    """把集中配置里的脚本绑定到各检测步骤，动作统一存入 'action' 字段。"""
    for step in steps:
        step["action"] = _lookup_script(script_map, step["detector"].name, mode_name)
    return steps


_bind_scripts(SEQUENCE_STEPS, SEQUENCE_TRIGGER_SCRIPTS, "顺序图像")


def describe_detector(detector):
    if detector.region != detector.source_region:
        return f"原区域{detector.source_region} -> 实际{detector.region}"
    return f"区域{detector.region}"


# ===================== 三种运行模式 =====================
async def logic_image_loop():
    if not LOGIC_CONFIG:
        print("[逻辑检测模式] 未配置检测器，退出。")
        return

    print("[逻辑检测模式] 已加载检测器：")
    for detector_name, item in LOGIC_CONFIG.items():
        detector = item["detector"]
        initial = "开启" if item.get("initial") else "关闭"
        actions = " -> ".join(item.get("actions", [])) or "无"
        print(
            f"  - {detector_name}: {describe_detector(detector)}, "
            f"阈值{detector.threshold}, 冷却{item['cooldown']}s, "
            f"初始{initial}, 动作[{actions}]"
        )

    loop = asyncio.get_running_loop()
    states = {
        detector_name: {
            "enabled": bool(item.get("initial")),
            "last_trigger": 0.0,
        }
        for detector_name, item in LOGIC_CONFIG.items()
    }
    empty_active_notified = False

    while not stop_event.is_set():
        active_entries = [
            (detector_name, item)
            for detector_name, item in LOGIC_CONFIG.items()
            if states[detector_name]["enabled"]
        ]
        if not active_entries:
            if not empty_active_notified:
                print(
                    "[逻辑检测模式] 当前没有开启的检测器，"
                    f"等待停止热键 '{STOP_HOTKEY}'。"
                )
                empty_active_notified = True
            await asyncio.sleep(DETECTION_INTERVAL)
            continue
        empty_active_notified = False

        detectors = [item["detector"] for _, item in active_entries]
        results = await loop.run_in_executor(
            detection_executor, check_detectors, detectors
        )
        if stop_event.is_set():
            break

        now = time.monotonic()
        matched_entries = [
            (detector_name, item)
            for (detector_name, item), matched in zip(active_entries, results)
            if matched
            and now - states[detector_name]["last_trigger"] >= item["cooldown"]
        ]
        if not matched_entries:
            await asyncio.sleep(DETECTION_INTERVAL)
            continue

        detector_name, item = matched_entries[0]
        states[detector_name]["last_trigger"] = now
        if len(matched_entries) > 1:
            names = ", ".join(name for name, _ in matched_entries)
            print(
                f"[逻辑检测模式] 检测器 {names} 同时命中，"
                f"按配置顺序先处理 {detector_name}。"
            )

        script = item.get("script")
        if script is None:
            print(f"[逻辑检测模式] {detector_name} 命中，未配置脚本，直接执行逻辑动作。")
        else:
            print(f"[逻辑检测模式] {detector_name} 命中，执行脚本。")
            create_action_task(script())
            print("[逻辑检测模式] 脚本执行中，暂停所有检测...")
            await wait_for_running_actions()
            if stop_event.is_set():
                break
            print("[逻辑检测模式] 脚本执行完毕，恢复检测。")

        module_names = item.get("actions", [])
        disabled_targets, enabled_targets = _apply_logic_action_modules(
            module_names, states
        )
        if module_names:
            print(
                f"[逻辑检测模式] 已执行动作模块："
                f"{' -> '.join(module_names)}"
            )
            print(
                f"[逻辑检测模式] 本次动作："
                f"关闭[{', '.join(disabled_targets) or '无'}]，"
                f"开启[{', '.join(enabled_targets) or '无'}]。"
            )
        else:
            print("[逻辑检测模式] 未配置动作模块，检测器状态保持不变。")

        enabled_names = [
            name for name in LOGIC_CONFIG
            if states[name]["enabled"]
        ]
        disabled_names = [
            name for name in LOGIC_CONFIG
            if not states[name]["enabled"]
        ]
        print(
            f"[逻辑检测模式] 检测器状态已切换："
            f"开启[{', '.join(enabled_names) or '无'}]，"
            f"关闭[{', '.join(disabled_names) or '无'}]。"
        )

    print("[停止] 逻辑检测循环已退出。")


async def sequential_image_loop():
    if not SEQUENCE_STEPS:
        print("[顺序图像模式] 未配置检测步骤，退出。")
        return

    print("[顺序图像模式] 步骤序列：")
    for i, step in enumerate(SEQUENCE_STEPS, 1):
        detector = step["detector"]
        print(f"  {i}. {detector.name}: {describe_detector(detector)}, 阈值{detector.threshold}")

    loop = asyncio.get_running_loop()
    last_triggers = [0.0] * len(SEQUENCE_STEPS)

    while not stop_event.is_set():
        for i, step in enumerate(SEQUENCE_STEPS):
            if stop_event.is_set():
                break

            detector = step["detector"]
            print(f"[顺序模式] 等待检测: {detector.name} ...")
            while not stop_event.is_set():
                matched = (await loop.run_in_executor(
                    detection_executor, check_detectors, [detector]
                ))[0]
                now = time.monotonic()
                if matched and now - last_triggers[i] >= step["cooldown"]:
                    print(f"[顺序模式] {detector.name} 匹配，执行脚本...")
                    action_task = create_action_task(step["action"]())
                    # 等待脚本执行完毕；此期间不做检测，跑完后再恢复。
                    try:
                        await action_task
                    except asyncio.CancelledError:
                        if stop_event.is_set():
                            break
                        raise
                    last_triggers[i] = time.monotonic()
                    print(f"[顺序模式] {detector.name} 脚本执行完毕。")
                    break
                await asyncio.sleep(DETECTION_INTERVAL)

        if not stop_event.is_set():
            print("[顺序模式] 全部步骤完成，重新开始。")

    print("[停止] 顺序检测循环已退出。")


async def wait_for_stop():
    while not stop_event.is_set():
        await asyncio.sleep(0.05)


async def run_application(mode):
    if mode == "key":
        print(f"按下 '{TRIGGER_HOTKEY}' 键将执行脚本操作。")
        await wait_for_stop()
    elif mode == "logic":
        await logic_image_loop()
    elif mode == "image_sequential":
        await sequential_image_loop()


# ===================== 热键监听 =====================
_last_hotkey_times = {}


def _hotkey_allowed(name, cooldown=0.25):
    now = time.monotonic()
    last_time = _last_hotkey_times.get(name, 0.0)
    if now - last_time < cooldown:
        return False
    _last_hotkey_times[name] = now
    return True


def _schedule_debug_capture():
    if not stop_event.is_set():
        asyncio.create_task(capture_all_regions())


async def _run_trigger_action_loop():
    while not stop_event.is_set():
        try:
            await KEY_TRIGGER_SCRIPT()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            print(f"[按键模式] 循环执行脚本失败: {ex}")
        await precise_sleep(TRIGGER_LOOP_DELAY)


def _clear_active_hold_task(task):
    global active_hold_task
    if active_hold_task is task:
        active_hold_task = None


def _track_active_hold_task(task):
    global active_hold_task
    active_hold_task = task
    task.add_done_callback(_clear_active_hold_task)


def _start_trigger_action():
    if stop_event.is_set():
        return

    if key_run_mode == KEY_MODE_FULL:
        create_action_task(KEY_TRIGGER_SCRIPT())
        return

    # 松开事件可能先于这个回调被事件循环处理，此时不应再启动按住任务。
    if not trigger_key_is_down:
        return

    if active_hold_task is not None and not active_hold_task.done():
        return

    if key_run_mode == KEY_MODE_HOLD:
        _track_active_hold_task(create_action_task(KEY_TRIGGER_SCRIPT()))
    elif key_run_mode == KEY_MODE_HOLD_LOOP:
        _track_active_hold_task(create_action_task(_run_trigger_action_loop()))


def _stop_trigger_action():
    global active_hold_task
    if active_hold_task is not None and not active_hold_task.done():
        active_hold_task.cancel()
        active_hold_task = None


def on_press(key):
    global trigger_key_is_down
    try:
        char = key.char.lower() if hasattr(key, "char") and key.char else None

        if char == STOP_HOTKEY:
            if stop_event.is_set():
                return
            print(f"[停止] 检测到停止热键 '{STOP_HOTKEY}'，正在停止...")
            stop_event.set()
            if main_loop is not None and main_loop.is_running():
                main_loop.call_soon_threadsafe(cancel_running_actions)
            return

        if char == DEBUG_SNAPSHOT_HOTKEY:
            if not _hotkey_allowed("debug_snapshot"):
                return
            print(f"[调试截图] 检测到热键 '{DEBUG_SNAPSHOT_HOTKEY}'，开始对检测区域截图...")
            if main_loop is not None and main_loop.is_running():
                main_loop.call_soon_threadsafe(_schedule_debug_capture)
            return

        if char == TRIGGER_HOTKEY:
            if trigger_key_is_down:
                return
            trigger_key_is_down = True
            if current_mode != "key" or stop_event.is_set():
                return
            print(f"[按键模式] 检测到按下 {TRIGGER_HOTKEY}，开始执行操作。")
            if main_loop is not None and main_loop.is_running():
                main_loop.call_soon_threadsafe(_start_trigger_action)
    except Exception as ex:
        print(f"[错误] 键盘监听异常: {ex}")


def on_release(key):
    global trigger_key_is_down
    try:
        char = key.char.lower() if hasattr(key, "char") and key.char else None
        if char != TRIGGER_HOTKEY:
            return

        trigger_key_is_down = False
        if current_mode != "key":
            return
        if key_run_mode not in (KEY_MODE_HOLD, KEY_MODE_HOLD_LOOP):
            return
        if stop_event.is_set():
            return

        print(f"[按键模式] 检测到松开 {TRIGGER_HOTKEY}，停止当前执行。")
        if main_loop is not None and main_loop.is_running():
            main_loop.call_soon_threadsafe(_stop_trigger_action)
    except Exception as ex:
        print(f"[错误] 键盘监听异常: {ex}")


def select_mode():
    print("请选择脚本执行模式：")
    print("1. 按键触发")
    print("2. 逻辑检测")
    print("3. 图像顺序检测")
    while True:
        choice = input("请输入 1、2 或 3: ").strip()
        if choice == "1":
            return "key"
        if choice == "2":
            return "logic"
        if choice == "3":
            return "image_sequential"
        print("输入无效，请重新输入。")


def select_key_run_mode():
    print("请选择按键触发方式：")
    print(f"1. 按下 {TRIGGER_HOTKEY}，执行完整脚本")
    print(f"2. 按住 {TRIGGER_HOTKEY} 执行，松开停止")
    print(f"3. 按住 {TRIGGER_HOTKEY} 循环执行，松开停止")
    while True:
        choice = input("请输入 1、2 或 3: ").strip()
        if choice == "1":
            return KEY_MODE_FULL
        if choice == "2":
            return KEY_MODE_HOLD
        if choice == "3":
            return KEY_MODE_HOLD_LOOP
        print("输入无效，请重新输入。")


def main():
    global current_mode, main_loop, key_run_mode
    global trigger_key_is_down, active_hold_task

    mode = select_mode()
    key_run_mode = select_key_run_mode() if mode == "key" else None
    current_mode = mode
    trigger_key_is_down = False
    active_hold_task = None
    stop_event.clear()
    _last_hotkey_times.clear()

    print("提示：")
    print(f"  - 停止热键: '{STOP_HOTKEY}'")
    print(f"  - 调试截图热键: '{DEBUG_SNAPSHOT_HOTKEY}'（保存到 {DEBUG_SCREENSHOT_DIR}）")
    if mode == "key":
        print(f"  - 执行热键: '{TRIGGER_HOTKEY}'")
        if key_run_mode == KEY_MODE_FULL:
            print("  - 触发方式: 按下执行完整脚本，松开不影响执行")
        elif key_run_mode == KEY_MODE_HOLD:
            print("  - 触发方式: 按住执行，松开停止")
        else:
            print("  - 触发方式: 按住循环执行，松开停止")

    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    try:
        with KeyboardListener(on_press=on_press, on_release=on_release):
            try:
                main_loop.run_until_complete(run_application(mode))
            except KeyboardInterrupt:
                print("\n[系统] 退出...")
            finally:
                stop_event.set()
                cancel_running_actions()
                main_loop.run_until_complete(cancel_running_actions_and_wait())
                input_manager.release_all()
                main_loop.close()
    finally:
        detection_executor.shutdown(wait=True)
        disable_windows_precision()


if __name__ == "__main__":
    main()
