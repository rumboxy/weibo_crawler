import asyncio
import json
import re
import smtplib
import logging
import os
from logging.handlers import RotatingFileHandler
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ==================== 全局配置 ====================
# 目标微博URL
TARGET_URL = "https://m.weibo.cn/u/1812511224?jumpfrom=weibocom"
# 定时爬取间隔（秒）- 1小时
INTERVAL = 3600
# 筛选时间范围（小时）
TIME_RANGE_HOURS = 5

import os 
# 邮件配置（请修改为你的实际信息）
SMTP_SERVER = "smtp.qq.com"  # 邮箱SMTP服务器
SMTP_PORT = 587   # SMTP端口（SSL）
# SEND_EMAIL = "xxxx"  # 发件人邮箱
# SEND_PASSWORD = "xxxx"  # 邮箱授权码非密码
# RECEIVE_EMAIL = "xxxx"  # 收件人邮箱
# 配置环境变量获取
SEND_EMAIL = os.getenv("SEND_EMAIL")
SEND_PASSWORD = os.getenv("SEND_PASSWORD")
RECEIVE_EMAIL = os.getenv("RECEIVE_EMAIL")
# 校验环境变量（添加）
if not all([SEND_EMAIL, SEND_PASSWORD, RECEIVE_EMAIL]):
    logger.critical("缺少邮箱配置环境变量！请检查GitHub Secrets")
    exit(1)

EMAIL_SUBJECT = f"微博爬虫-{datetime.now().strftime('%Y-%m-%d')}-1小时内新内容"

# 日志配置
LOG_DIR = "weibo_crawl_logs"
LOG_FILE = os.path.join(LOG_DIR, "weibo_crawler.log")
LOG_MAX_SIZE = 10 * 1024 * 1024  # 日志文件最大10MB
LOG_BACKUP_COUNT = 5  # 最多保留5个备份日志文件


# =================================================

# ==================== 日志初始化 ====================
def init_logger():
    """初始化日志配置"""
    # 创建日志目录
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # 定义日志格式
    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 初始化logger
    logger = logging.getLogger("WeiboCrawler")
    logger.setLevel(logging.DEBUG)  # 总级别设为DEBUG

    # 控制台处理器（输出INFO及以上级别）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)

    # 文件处理器（轮转文件，输出DEBUG及以上级别）
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_format)

    # 添加处理器
    if not logger.handlers:  # 避免重复添加
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


# 初始化全局logger
logger = init_logger()


# ==================== 核心函数 ====================
def parse_weibo_time(time_str: str, crawl_datetime: datetime) -> datetime:
    """解析微博发布时间为标准datetime对象"""
    if not time_str or time_str == "未知时间":
        logger.warning(f"无效的时间字符串：{time_str}")
        return None

    # 处理相对时间
    try:
        if "刚刚" in time_str:
            return crawl_datetime
        elif "分钟前" in time_str:
            minutes = int(re.findall(r'\d+', time_str)[0])
            return crawl_datetime - timedelta(minutes=minutes)
        elif "小时前" in time_str:
            hours = int(re.findall(r'\d+', time_str)[0])
            return crawl_datetime - timedelta(hours=hours)
        elif "今天" in time_str:
            time_part = time_str.replace("今天", "").strip()
            date_part = crawl_datetime.strftime("%Y-%m-%d")
            return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        elif "昨天" in time_str:
            time_part = time_str.replace("昨天", "").strip()
            yesterday = crawl_datetime - timedelta(days=1)
            date_part = yesterday.strftime("%Y-%m-%d")
            return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")

        # 处理绝对时间
        if len(time_str.split("-")) == 2 and ":" in time_str:
            year = crawl_datetime.year
            return datetime.strptime(f"{year}-{time_str}", "%Y-%m-%d %H:%M")
        elif len(time_str.split("-")) == 3 and ":" in time_str:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except Exception as e:
        logger.error(f"时间解析失败：{time_str} | 错误：{str(e)}")
        return None

    logger.warning(f"未匹配的时间格式：{time_str}")
    return None


async def extract_links(element):
    """提取元素内的所有超链接"""
    links = []
    try:
        a_elements = await element.query_selector_all("a")
        for a in a_elements:
            href = await a.get_attribute("href")
            link_text = await a.inner_text() if await a.inner_text() else "无文本"
            if href:
                if href.startswith("/"):
                    href = f"https://m.weibo.cn{href}"
                links.append({
                    "href": href,
                    "text": link_text.strip()
                })
        logger.debug(f"提取到{len(links)}个超链接")
    except Exception as e:
        logger.error(f"提取超链接失败：{str(e)}")
    return links


def send_email_with_attachment(file_path: str, crawl_count: int):
    """发送带JSON附件的邮件"""
    try:
        # 构建邮件主体
        msg = MIMEMultipart()
        msg["From"] = SEND_EMAIL
        msg["To"] = RECEIVE_EMAIL
        msg["Subject"] = f"{EMAIL_SUBJECT}（共{int(crawl_count)}条）"

        # 邮件正文
        body = f"""
        <h3>微博爬虫通知</h3>
        <p>本次爬取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p>筛选范围：{TIME_RANGE_HOURS}小时内发布的微博</p>
        <p>爬取到有效内容数量：{crawl_count}条</p>
        <p>附件为爬取结果的JSON文件，请查收。</p>
        """
        msg.attach(MIMEText(body, "html", "utf-8"))

        # 添加JSON附件
        with open(file_path, "rb") as f:
            attachment = MIMEApplication(f.read())
            attachment.add_header("Content-Disposition", "attachment", filename=os.path.basename(file_path))
            msg.attach(attachment)

        # 发送邮件
        import ssl
        server = smtplib.SMTP(SMTP_SERVER, 587)
        try:
            # 启用TLS加密
            server.starttls(context=ssl.create_default_context())
            # 方案2：指定SSL协议版本（可选，适配老旧服务器）
            # server.starttls(context=ssl.SSLContext(ssl.PROTOCOL_TLSv1_2))

            # 登录邮箱
            server.login(SEND_EMAIL, SEND_PASSWORD)
            # 发送邮件
            server.sendmail(SEND_EMAIL, RECEIVE_EMAIL, msg.as_string())
            logger.info(f"邮件发送成功！附件：{file_path} | 收件人：{RECEIVE_EMAIL}")
        except Exception as e:
            logger.error(f"TLS 587端口连接失败，尝试SSL 465端口 | 错误：{str(e)}")
            # 降级方案：使用SSL 465端口 + 指定协议版本
            server.quit()
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)  # 强制使用TLSv1.2
            with smtplib.SMTP_SSL(SMTP_SERVER, 465, context=context) as ssl_server:
                # 方案3：测试用 - 关闭SSL验证（仅临时排查问题）
                # ssl_server.verify_mode = ssl.CERT_NONE

                ssl_server.login(SEND_EMAIL, SEND_PASSWORD)
                ssl_server.sendmail(SEND_EMAIL, RECEIVE_EMAIL, msg.as_string())
            logger.info(f"SSL 465端口发送成功！附件：{file_path} | 收件人：{RECEIVE_EMAIL}")
        finally:
            server.quit()
    except Exception as e:
        logger.error(f"邮件发送失败：{str(e)}", exc_info=True)


async def crawl_weibo():
    """爬取微博核心逻辑（含2小时筛选+邮件发送）"""
    crawl_datetime = datetime.now()
    time_threshold = crawl_datetime - timedelta(hours=TIME_RANGE_HOURS)
    json_file_path = ""

    async with async_playwright() as p:
        browser = None
        try:
            # 启动浏览器
            browser = await p.chromium.launch(
                headless=True,
                args=[
                      "--no-sandbox",  # 必须，Ubuntu环境需要
                      "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage",
                      "--disable-gpu",  # 新增，适配无头环境
                      "--single-process"  # 新增，减少资源占用
                    # "--no-sandbox",
                    # "--disable-blink-features=AutomationControlled",
                    # "--disable-dev-shm-usage"
                ]
            )
            logger.debug("浏览器启动成功")

            # 创建上下文
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai"
            )
            page = await context.new_page()

            # 禁用图片加载
            await page.route("**/*", lambda
                route: route.abort() if route.request.resource_type == "image" else route.continue_())
            logger.debug("图片加载已禁用")

            # 访问目标页面
            await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            await page.wait_for_selector(".card-wrap", timeout=10000)
            logger.info(f"页面加载完成：{TARGET_URL}")

            # 提取微博数据
            weibo_items = await page.query_selector_all(".card-wrap")
            logger.debug(f"找到{len(weibo_items)}个微博卡片")
            crawl_result = []

            for idx, item in enumerate(weibo_items):
                try:
                    # 提取原始时间
                    time_elem = await item.query_selector(".time")
                    raw_post_time = await time_elem.inner_text() if time_elem else "未知时间"

                    # 解析时间并筛选
                    post_datetime = parse_weibo_time(raw_post_time, crawl_datetime)
                    if not post_datetime or post_datetime < time_threshold:
                        logger.debug(f"微博{idx + 1}：超出{TIME_RANGE_HOURS}小时范围，跳过 | 发布时间：{raw_post_time}")
                        continue

                    # 提取核心信息
                    content_elem = await item.query_selector(".weibo-text")
                    content = await content_elem.inner_text() if content_elem else "无内容"

                    like_elem = await item.query_selector(".like > span")
                    like_count = await like_elem.inner_text() if like_elem else "0"

                    comment_elem = await item.query_selector(".comment > span")
                    comment_count = await comment_elem.inner_text() if comment_elem else "0"

                    forward_elem = await item.query_selector(".forward > span")
                    forward_count = await forward_elem.inner_text() if forward_elem else "0"

                    # 提取微博链接
                    weibo_link_elem = await item.query_selector(".card-btm-bar > a:first-child")
                    weibo_link = ""
                    if weibo_link_elem:
                        weibo_link = await weibo_link_elem.get_attribute("href")
                        if weibo_link and weibo_link.startswith("/"):
                            weibo_link = f"https://m.weibo.cn{weibo_link}"

                    # 提取内容链接
                    content_links = await extract_links(content_elem) if content_elem else []

                    # 添加到结果
                    weibo_data = {
                        "raw_post_time": raw_post_time,
                        "post_datetime": post_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                        "content": content.strip(),
                        "like_count": like_count,
                        "comment_count": comment_count,
                        "forward_count": forward_count,
                        "weibo_link": weibo_link,
                        "content_links": content_links,
                        "crawl_time": crawl_datetime.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    crawl_result.append(weibo_data)
                    logger.debug(f"微博{idx + 1}：提取成功 | 内容预览：{content[:50]}...")

                except Exception as e:
                    logger.error(f"处理微博{idx + 1}失败：{str(e)}", exc_info=True)
                    continue

            # 处理爬取结果
            logger.info(f"本次爬取完成 | 筛选时间：{TIME_RANGE_HOURS}小时内 | 有效数据：{len(crawl_result)}条")

            if crawl_result:
                # 保存JSON文件
                json_file_path = f"weibo_data_{crawl_datetime.strftime('%Y%m%d_%H%M%S')}.json"
                with open(json_file_path, "w", encoding="utf-8") as f:
                    json.dump(crawl_result, f, ensure_ascii=False, indent=4)
                logger.info(f"数据已保存至：{json_file_path}")

                # 打印详情（使用logger.info）
                for idx, weibo in enumerate(crawl_result, 1):
                    logger.info(f"\n【微博 {idx}】")
                    logger.info(f"发布时间：{weibo['raw_post_time']}（标准化：{weibo['post_datetime']}）")
                    logger.info(f"内容：{weibo['content']}")
                    logger.info(
                        f"互动：点赞{weibo['like_count']} | 评论{weibo['comment_count']} | 转发{weibo['forward_count']}")
                    logger.info(f"链接：{weibo['weibo_link']}")

                # 发送邮件
                send_email_with_attachment(json_file_path, len(crawl_result))
            else:
                logger.warning("暂无2小时内的新微博内容")

        except Exception as e:
            logger.error(f"爬取主流程失败：{str(e)}", exc_info=True)
        finally:
            if browser:
                await browser.close()
                logger.debug("浏览器已关闭")


async def main():
    """主函数：定时执行"""
    logger.info("=" * 60)
    logger.info(f"微博爬虫启动 | 每{INTERVAL / 3600}小时执行一次")
    logger.info(f"筛选规则：仅提取{TIME_RANGE_HOURS}小时内的内容")
    logger.info(f"邮件接收地址：{RECEIVE_EMAIL}")
    logger.info(f"首次执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    await crawl_weibo()
    # while True:
    #     await crawl_weibo()
    #     logger.info(f"\n等待{INTERVAL / 3600}小时后再次执行...")
    #     await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("爬虫被用户手动终止")
    except Exception as e:
        logger.critical(f"爬虫主进程崩溃：{str(e)}", exc_info=True)
