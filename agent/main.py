import sys

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
