import os
import sys

# 获取当前main.py所在路径并设置上级目录为工作目录
current_file_path = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file_path)
parent_dir = os.path.dirname(current_dir)
os.chdir(parent_dir)
# print(f"设置工作目录为: {parent_dir}")

# 将当前目录添加到路径
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from utils import logger
except ImportError:
    # 如果logger不存在，创建一个简单的logger
    import logging

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
    )
    logger = logging


def agent():
    try:
        from maa.agent.agent_server import AgentServer
        from maa.toolkit import Toolkit

        import custom
        from utils import logger

        Toolkit.init_option("./")

        socket_id = sys.argv[-1]

        AgentServer.start_up(socket_id)
        logger.info("AgentServer 启动")
        AgentServer.join()
        AgentServer.shut_down()
        logger.info("AgentServer 关闭")
    except Exception as e:
        logger.exception("agent 运行过程中发生异常")
        raise


def main():
    agent()


if __name__ == "__main__":
    main()
