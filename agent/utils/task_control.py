"""在自定义动作的连续输入之间响应 MaaFramework 停止请求。"""

import time


def wait_running(tasker, seconds):
    """等待既有动作间隔，收到停止请求时提前返回。"""
    deadline = time.monotonic() + seconds
    while not tasker.stopping:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.1))
    return False


def click_running(tasker, x, y):
    return not tasker.stopping and tasker.controller.post_click(x, y).wait().succeeded


def double_click_running(tasker, x, y, delay):
    return click_running(tasker, x, y) and wait_running(tasker, delay) and click_running(tasker, x, y)
