"""
脚本 1：按键触发 / 图像检测使用的动作序列。

由 test_aut.py 调用 script_1.run()，也可以直接运行本文件单独测试。

常用写法：
    await key_tap("e", after=0.2)
    await key_hold("e", 0.3, after=0.2)
    await key_combo(Key.ctrl, "c", after=0.1)
    await mouse_click(after=0.3)
    await mouse_hold(duration=0.5, after=0.1)
"""

import asyncio

from pynput.keyboard import Key

from script_runtime import (
    E,
    H,
    key_tap,
    mouse_click,
    mouse_down,
    mouse_up,
    precise_sleep,
)


async def run():
    await key_tap(E, hold=0.01, after=0.26)

    for _ in range(108):
        await mouse_click(hold=0.01, after=0.01)

    mouse_down()
    try:
        await precise_sleep(0.41)
        await key_tap(H, hold=0.01, after=0.23)
    finally:
        mouse_up()

    await key_tap(Key.shift, hold=0.02, after=0.08)
    await key_tap(H, hold=0.01, after=0.05)

    for _ in range(5):
        for _ in range(92):
            await mouse_click(hold=0.01, after=0.01)

        mouse_down()
        try:
            await precise_sleep(0.41)
            await key_tap(H, hold=0.01, after=0.23)
        finally:
            mouse_up()

        await key_tap(Key.shift, hold=0.02, after=0.08)
        await key_tap(H, hold=0.01, after=0.05)

    for _ in range(6):
        await key_tap(E, hold=0.01, after=0.26)

        for _ in range(6):
            for _ in range(92):
                await mouse_click(hold=0.01, after=0.01)

            mouse_down()
            try:
                await precise_sleep(0.41)
                await key_tap(H, hold=0.01, after=0.23)
            finally:
                mouse_up()

            await key_tap(Key.shift, hold=0.02, after=0.08)
            await key_tap(H, hold=0.01, after=0.05)


if __name__ == "__main__":
    asyncio.run(run())
